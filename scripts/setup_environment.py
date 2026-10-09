#!/usr/bin/env python3
"""AndroidForge — Toolchain Setup.

Detects project toolchain requirements, applies conservative compatibility
fixes, and writes a JSON summary for GitHub Actions.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print(
        "ERROR: PyYAML not installed. Run: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = REPO_ROOT / "config" / "toolchain-rules.yaml"
DEFAULT_ANDROID_SDK = "/usr/local/lib/android/sdk"


def load_rules(path: Path | None = None) -> dict[str, Any]:
    rules_path = path or RULES_PATH

    try:
        data = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(
            f"Cannot read toolchain rules '{rules_path}': {exc}"
        ) from exc
    except yaml.YAMLError as exc:
        raise ValueError(
            f"Invalid YAML in '{rules_path}': {exc}"
        ) from exc

    if data is None:
        return {}

    if not isinstance(data, dict):
        raise ValueError("Toolchain rules must contain a YAML mapping.")

    return data


def major(version: str | int | None) -> int:
    match = re.search(r"\d+", str(version or ""))
    return int(match.group()) if match else 0


def minor(version: str | int | None) -> int:
    parts = re.findall(r"\d+", str(version or ""))
    return int(parts[1]) if len(parts) > 1 else 0


def _version_rule(
    version: str | None,
    mapping: dict,
) -> str | None:
    """Return the most specific matching version rule."""
    if not version:
        return None

    matches = []

    for pattern, value in mapping.items():
        pattern = str(pattern)

        if pattern.endswith(".x"):
            prefix = pattern[:-2]
            matched = (
                version == prefix
                or version.startswith(prefix + ".")
            )
        else:
            matched = (
                version == pattern
                or version.startswith(pattern + ".")
            )

        if matched:
            matches.append((len(pattern), str(value)))

    if not matches:
        return None

    return max(matches, key=lambda item: item[0])[1]


def pick_jdk_for_agp(
    agp: str | None,
    rules: dict[str, Any],
) -> str:
    if not agp:
        return "17"

    configured = _version_rule(
        str(agp),
        rules.get("agp_to_jdk", {}) or {},
    )

    if configured:
        return configured

    version = major(agp)

    if version >= 8:
        return "17"
    if version >= 7:
        return "11"

    return "8"


def pick_jdk_for_gradle(
    gradle: str | None,
    rules: dict[str, Any],
) -> str:
    if gradle:
        configured = _version_rule(
            str(gradle),
            rules.get("gradle_to_jdk", {}) or {},
        )

        if configured:
            return configured

    version = major(gradle)

    if version >= 8:
        return "17"
    if version == 7:
        return "11"
    if 0 < version <= 6:
        return "8"

    return "17"


def pick_gradle_for_agp(
    agp: str | None,
    rules: dict[str, Any],
) -> str | None:
    if not agp:
        return None

    mapping = rules.get("agp_to_gradle", {}) or {}
    agp = str(agp)

    if agp in mapping:
        return str(mapping[agp])

    parts = agp.split(".")

    for length in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:length])

        if prefix in mapping:
            return str(mapping[prefix])

    same_major = [
        str(value)
        for key, value in mapping.items()
        if major(key) == major(agp)
    ]

    if len(set(same_major)) == 1 and same_major:
        return same_major[0]

    return None


def pick_flutter_version(
    detect: dict,
    rules: dict,
) -> str:
    flutter = detect.get("flutter") or {}
    constraint = flutter.get("flutter_version_constraint")

    if constraint:
        match = re.search(r"(\d+\.\d+\.\d+)", str(constraint))

        if match:
            return match.group(1)

    return str(
        (rules.get("flutter") or {}).get(
            "default_version", "3.24.0"
        )
    )


def pick_ndk_version(
    detect: dict,
    rules: dict,
) -> str:
    version = (detect.get("versions") or {}).get("ndk_version")

    if version:
        return str(version)

    return str(
        (rules.get("ndk") or {}).get(
            "default_version", "26.1.10909125"
        )
    )


def determine_legacy_fixes(
    detect: dict,
    rules: dict,
) -> list[dict[str, str]]:
    fixes = []
    wrapper = detect.get("wrapper") or {}
    gradle_version = wrapper.get("version")
    indicators = detect.get("indicator_files") or {}

    if wrapper.get("missing_jar") or not wrapper.get("present"):
        fixes.append({
            "name": "patch_gradle_wrapper",
            "reason": "Gradle Wrapper is missing or incomplete",
        })

    if wrapper.get("uses_http"):
        fixes.append({
            "name": "migrate_https",
            "reason": "Wrapper distribution URL uses HTTP",
        })

    if gradle_version and major(gradle_version) < 4:
        fixes.append({
            "name": "disable_gradle_daemon",
            "reason": f"Legacy Gradle version: {gradle_version}",
        })

    if (
        "local.properties" not in indicators
        and detect.get("project_type") in ("gradle", "flutter")
    ):
        fixes.append({
            "name": "inject_local_properties",
            "reason": "local.properties is missing",
        })

    return fixes


def _ensure_gradle_properties(
    path: Path,
    label: str,
    applied: list[str],
) -> None:
    """Preserve existing settings while adding missing compatibility flags."""
    try:
        original = (
            path.read_text(encoding="utf-8")
            if path.exists()
            else ""
        )
    except OSError as exc:
        applied.append(f"Could not read {label}: {exc}")
        return

    content = original
    additions = []

    jvm_match = re.search(
        r"(?m)^(\s*org\.gradle\.jvmargs\s*=\s*)(.*)$",
        content,
    )

    if not jvm_match:
        additions.append(
            "org.gradle.jvmargs=-Xmx4g "
            "-XX:+UseG1GC -Dfile.encoding=UTF-8"
        )
    else:
        existing = jvm_match.group(2).strip()
        heap = re.search(
            r"-Xmx(\d+)([kmg])?",
            existing,
            re.IGNORECASE,
        )

        if not heap:
            updated = existing + " -Xmx4g"
            content = (
                content[:jvm_match.start(2)]
                + updated
                + content[jvm_match.end(2):]
            )
        else:
            size = int(heap.group(1))
            unit = (heap.group(2) or "m").lower()

            if unit == "k":
                size_mb = size / 1024
            elif unit == "g":
                size_mb = size * 1024
            else:
                size_mb = size

            if size_mb < 2048:
                updated = (
                    existing[:heap.start()]
                    + "-Xmx4g"
                    + existing[heap.end():]
                )
                content = (
                    content[:jvm_match.start(2)]
                    + updated
                    + content[jvm_match.end(2):]
                )

    defaults = [
        ("android.useAndroidX", "android.useAndroidX=true"),
        ("android.enableJetifier", "android.enableJetifier=true"),
        ("org.gradle.daemon", "org.gradle.daemon=false"),
    ]

    for key, line in defaults:
        if not re.search(
            rf"(?m)^\s*{re.escape(key)}\s*=",
            content,
        ):
            additions.append(line)

    if additions:
        if content and not content.endswith("\n"):
            content += "\n"

        content += (
            "# AndroidForge: compatibility defaults\n"
            + "".join(line + "\n" for line in additions)
        )

    if content != original:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            applied.append(f"Updated {label}")
        except OSError as exc:
            applied.append(f"Could not update {label}: {exc}")


def _inject_repositories(
    root: Path,
    applied: list[str],
    is_flutter: bool,
) -> None:
    candidates = []

    if is_flutter:
        candidates.extend([
            root / "android" / "build.gradle",
            root / "android" / "build.gradle.kts",
        ])

    candidates.extend([
        root / "build.gradle",
        root / "build.gradle.kts",
    ])

    groovy = [
        ("google()", "google()"),
        ("mavenCentral()", "mavenCentral()"),
        ("jitpack.io", "maven { url 'https://jitpack.io' }"),
    ]

    kotlin = [
        ("google()", "google()"),
        ("mavenCentral()", "mavenCentral()"),
        (
            "jitpack.io",
            'maven { url = uri("https://jitpack.io") }',
        ),
    ]

    for path in candidates:
        if not path.is_file():
            continue

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            applied.append(f"Could not read {path}: {exc}")
            continue

        match = re.search(r"\brepositories\s*\{", content)

        if not match:
            continue

        start = match.end()
        depth = 1
        index = start
        quote = None
        escaped = False

        while index < len(content) and depth:
            char = content[index]

            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            elif char in ("'", '"'):
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1

            index += 1

        if depth:
            applied.append(
                f"Skipped repository injection in {path}: unmatched braces"
            )
            continue

        close_index = index - 1
        block = content[start:close_index]
        lines = kotlin if path.name.endswith(".kts") else groovy

        additions = [
            line for marker, line in lines
            if marker not in block
        ]

        if not additions:
            continue

        insertion = (
            "\n        // AndroidForge: common Maven repositories\n"
            + "".join("        " + line + "\n" for line in additions)
            + "    "
        )

        updated = (
            content[:close_index]
            + insertion
            + content[close_index:]
        )

        try:
            path.write_text(updated, encoding="utf-8")
            applied.append(f"Injected repositories into {path}")
        except OSError as exc:
            applied.append(f"Could not update {path}: {exc}")


def apply_fixes(
    detect: dict,
    fixes: list[dict[str, str]],
    android_sdk_root: str,
) -> list[str]:
    applied = []
    root = Path(detect["project_root"]).expanduser().resolve()

    wrapper_properties = (
        root / "gradle" / "wrapper" / "gradle-wrapper.properties"
    )

    for fix in fixes:
        name = fix["name"]

        if name == "migrate_https" and wrapper_properties.is_file():
            try:
                content = wrapper_properties.read_text(encoding="utf-8")
                updated = re.sub(
                    r"(?m)^(\s*distributionUrl\s*=\s*)http://",
                    r"\1https://",
                    content,
                )

                if updated != content:
                    wrapper_properties.write_text(
                        updated, encoding="utf-8"
                    )
                    applied.append(
                        "Migrated Gradle distribution URL to HTTPS"
                    )
            except OSError as exc:
                applied.append(
                    f"Could not update wrapper properties: {exc}"
                )

        elif name == "inject_local_properties":
            path = root / "local.properties"

            try:
                content = (
                    path.read_text(encoding="utf-8")
                    if path.exists()
                    else ""
                )

                sdk_line = f"sdk.dir={android_sdk_root}"

                if re.search(r"(?m)^\s*sdk\.dir\s*=", content):
                    content = re.sub(
                        r"(?m)^\s*sdk\.dir\s*=.*$",
                        lambda _: sdk_line,
                        content,
                    )
                else:
                    if content and not content.endswith("\n"):
                        content += "\n"
                    content += sdk_line + "\n"

                path.write_text(content, encoding="utf-8")
                applied.append(
                    "Ensured Android SDK path in local.properties"
                )
            except OSError as exc:
                applied.append(
                    f"Could not update local.properties: {exc}"
                )

        elif name == "patch_gradle_wrapper":
            applied.append(
                "Wrapper regeneration required; no wrapper JAR was fabricated"
            )

        elif name == "disable_gradle_daemon":
            _ensure_gradle_properties(
                root / "gradle.properties",
                "gradle.properties",
                applied,
            )

    _ensure_gradle_properties(
        root / "gradle.properties",
        "root gradle.properties",
        applied,
    )

    android_dir = root / "android"

    if android_dir.is_dir():
        _ensure_gradle_properties(
            android_dir / "gradle.properties",
            "android/gradle.properties",
            applied,
        )

    _inject_repositories(
        root,
        applied,
        is_flutter=bool(
            (detect.get("flutter") or {}).get("is_flutter")
        ),
    )

    return applied


def read_detection(value: str) -> dict[str, Any]:
    candidate = Path(value)

    try:
        if candidate.is_file():
            raw = candidate.read_text(encoding="utf-8")
        else:
            raw = value
    except OSError:
        raw = value

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"--detect must be a JSON file path or JSON string: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError("--detect JSON must be an object")

    return data


def write_github_output(result: dict[str, Any]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")

    if not output_path:
        return

    lines = [
        f"jdk_version={result['jdk_version']}",
        f"gradle_version={result['gradle_version']}",
        f"needs_flutter={str(result['needs_flutter']).lower()}",
        f"needs_ndk={str(result['needs_ndk']).lower()}",
        f"needs_gradle={str(result['needs_gradle']).lower()}",
        f"use_wrapper={str(result['use_gradle_wrapper']).lower()}",
    ]

    for key in (
        "agp_version",
        "kotlin_version",
        "ndk_version",
        "flutter_version",
    ):
        if result.get(key) is not None:
            lines.append(f"{key}={result[key]}")

    delimiter = "ANDROIDFORGE_" + uuid.uuid4().hex
    json_text = json.dumps(result, separators=(",", ":"))

    while delimiter in json_text:
        delimiter = "ANDROIDFORGE_" + uuid.uuid4().hex

    lines.extend([
        f"json<<{delimiter}",
        json_text,
        delimiter,
    ])

    try:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise ValueError(
            f"Cannot write GitHub Actions output: {exc}"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AndroidForge toolchain setup"
    )

    parser.add_argument("--root", required=True, help="Project root path")
    parser.add_argument(
        "--detect",
        required=True,
        help="Detection JSON string or file path",
    )
    parser.add_argument(
        "--rules",
        default=None,
        help="Path to toolchain-rules.yaml",
    )
    parser.add_argument(
        "--android-sdk",
        default=os.environ.get(
            "ANDROID_SDK_ROOT",
            DEFAULT_ANDROID_SDK,
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write JSON summary to this file",
    )

    args = parser.parse_args()

    try:
        rules = load_rules(
            Path(args.rules).expanduser() if args.rules else None
        )

        detect = read_detection(args.detect)
        root = Path(args.root).expanduser().resolve()

        if not root.is_dir():
            raise ValueError(
                f"Project root is not a directory: {root}"
            )

        detect["project_root"] = str(root)

        versions = detect.get("versions") or {}
        wrapper = detect.get("wrapper") or {}

        agp = versions.get("agp_version")
        wrapper_gradle = wrapper.get("version")
        kotlin_version = versions.get("kotlin_version")

        agp_jdk = pick_jdk_for_agp(
            str(agp) if agp else None,
            rules,
        )
        gradle_jdk = pick_jdk_for_gradle(
            str(wrapper_gradle) if wrapper_gradle else None,
            rules,
        )

        # AGP and Gradle runtime requirements both matter.
        required_jdk = max(
            major(agp_jdk),
            major(gradle_jdk),
        )

        if required_jdk >= 17:
            chosen_jdk = "17"
        elif required_jdk >= 11:
            chosen_jdk = "11"
        else:
            chosen_jdk = "8"

        # Choose a compatible Gradle version.
        # An outdated wrapper must not override AGP requirements.
        def version_tuple(
            value: str | int | None,
        ) -> tuple[int, int, int]:
            parts = [
                int(part)
                for part in re.findall(
                    r"\d+", str(value or "")
                )[:3]
            ]
            return tuple((parts + [0, 0, 0])[:3])

        required_gradle = pick_gradle_for_agp(
            str(agp) if agp else None,
            rules,
        )

        wrapper_script = root / (
            "gradlew.bat" if os.name == "nt" else "gradlew"
        )
        wrapper_jar = (
            root / "gradle" / "wrapper" / "gradle-wrapper.jar"
        )

        wrapper_usable = (
            bool(wrapper.get("present"))
            and bool(wrapper_gradle)
            and wrapper_script.is_file()
            and wrapper_jar.is_file()
        )

        wrapper_too_old = (
            wrapper_usable
            and bool(required_gradle)
            and version_tuple(wrapper_gradle)
            < version_tuple(required_gradle)
        )

        # Use the wrapper only when complete and not too old.
        use_wrapper = wrapper_usable and not wrapper_too_old

        if use_wrapper:
            chosen_gradle = str(wrapper_gradle)
        else:
            chosen_gradle = (
                required_gradle
                or (
                    str(wrapper_gradle)
                    if wrapper_gradle
                    else None
                )
                or "8.0"
            )

        needs_flutter = bool(
            (detect.get("flutter") or {}).get("is_flutter")
        )
        needs_ndk = bool(
            (detect.get("native") or {}).get("has_native")
        )

        chosen_flutter = (
            pick_flutter_version(detect, rules)
            if needs_flutter else None
        )
        chosen_ndk = (
            pick_ndk_version(detect, rules)
            if needs_ndk else None
        )

        fixes = determine_legacy_fixes(detect, rules)
        applied_fixes = apply_fixes(
            detect,
            fixes,
            args.android_sdk,
        )

        if needs_flutter:
            prep_commands = [["flutter", "pub", "get"]]
            build_commands = [
                ["flutter", "build", "apk", "--debug"],
                ["flutter", "build", "apk", "--release"],
            ]
        else:
            gradle_command = (
                str(wrapper_script)
                if use_wrapper
                else "gradle"
            )
            prep_commands = []
            build_commands = [
                [gradle_command, "assembleDebug"],
                [gradle_command, "assembleRelease"],
                [gradle_command, "bundleDebug"],
            ]

        project_type = detect.get("project_type")

        result = {
            "project_root": str(root),
            "jdk_version": chosen_jdk,
            "gradle_version": chosen_gradle,
            "agp_version": agp,
            "kotlin_version": kotlin_version,
            "ndk_version": chosen_ndk,
            "flutter_version": chosen_flutter,
            "use_gradle_wrapper": use_wrapper,
            "needs_flutter": needs_flutter,
            "needs_ndk": needs_ndk,
            "needs_gradle": project_type in ("gradle", "flutter"),
            "legacy_fixes_applied": applied_fixes,
            "prep_commands": prep_commands,
            "build_commands": build_commands,
        }

        output = json.dumps(result, indent=2)

        if args.output:
            output_path = Path(args.output).expanduser()
            output_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            output_path.write_text(
                output + "\n",
                encoding="utf-8",
            )
        else:
            print(output)

        write_github_output(result)
        return 0

    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
