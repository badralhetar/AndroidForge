#!/usr/bin/env python3
"""AndroidForge — Toolchain Setup.

Reads detection output and toolchain rules, chooses build tools, applies
non-destructive compatibility fixes, and writes a JSON summary.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = REPO_ROOT / "config" / "toolchain-rules.yaml"


def load_rules(path: Path | None = None) -> dict[str, Any]:
    p = path or RULES_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return data or {}


def major(v: str) -> int:
    try:
        return int(str(v).split(".")[0])
    except (ValueError, IndexError):
        return 0


def minor(v: str) -> int:
    try:
        return int(str(v).split(".")[1])
    except (ValueError, IndexError):
        return 0


def pick_jdk_for_agp(agp: str | None, rules: dict) -> str:
    """Determine JDK version required by AGP."""
    if not agp:
        return "17"
    m = major(agp)
    if m >= 8:
        return "17"
    if m in (4, 7):
        return "11"
    if m <= 3:
        return "8"
    for pattern, jdk in rules.get("agp_to_jdk", {}).items():
        if pattern.endswith(".x"):
            if str(agp).startswith(pattern[:-2]):
                return str(jdk)
        elif str(agp).startswith(pattern):
            return str(jdk)
    return "17"


def pick_jdk_for_gradle(gradle: str | None, rules: dict) -> str:
    if not gradle:
        return "17"
    m = major(gradle)
    if m >= 8:
        return "17"
    if m == 7:
        return "11"
    if m <= 6:
        return "8"
    return "17"


def pick_gradle_for_agp(agp: str | None, rules: dict) -> str | None:
    if not agp:
        return None
    mapping = rules.get("agp_to_gradle", {})
    if agp in mapping:
        return mapping[agp]
    parts = str(agp).split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in mapping:
            return mapping[prefix]
    m = major(agp)
    for k, v in mapping.items():
        if major(k) == m:
            return v
    return None


def pick_flutter_version(detect: dict, rules: dict) -> str:
    constraint = (detect.get("flutter", {}) or {}).get("flutter_version_constraint")
    if constraint:
        match = re.search(r"(\d+\.\d+\.\d+)", constraint)
        if match:
            return match.group(1)
    return str(rules.get("flutter", {}).get("default_version", "3.24.0"))


def pick_ndk_version(detect: dict, rules: dict) -> str:
    ndk_v = (detect.get("versions") or {}).get("ndk_version")
    if ndk_v:
        return str(ndk_v)
    return str(rules.get("ndk", {}).get("default_version", "26.1.10909125"))


def determine_legacy_fixes(detect: dict, rules: dict) -> list[dict]:
    fixes: list[dict] = []
    wrapper = detect.get("wrapper", {}) or {}
    gradle_v = wrapper.get("version")
    has_local = "local.properties" in detect.get("indicator_files", {})
    if wrapper.get("missing_jar") or not wrapper.get("present"):
        fixes.append({"name": "patch_gradle_wrapper", "reason": "Missing or corrupt gradle-wrapper.jar"})
    if wrapper.get("uses_http"):
        fixes.append({"name": "migrate_https", "reason": "Wrapper uses http:// distribution URL"})
    if gradle_v and major(gradle_v) < 4:
        fixes.append({"name": "disable_gradle_daemon", "reason": f"Very old Gradle: {gradle_v}"})
    if not has_local and detect.get("project_type") in ("gradle", "flutter"):
        fixes.append({"name": "inject_local_properties", "reason": "local.properties missing"})
    return fixes


def _inject_repositories(root: Path, applied: list[str], is_flutter: bool) -> None:
    """Add repositories only to safe build.gradle files, never settings.gradle(.kts).

    Settings scripts can use centralized dependency repositories and plugin
    repositories with different DSL scopes. Injecting a generic repositories
    block into them is unsafe and can corrupt otherwise valid settings files.
    """
    candidates: list[Path] = []
    if is_flutter:
        candidates.extend([root / "android" / "build.gradle", root / "android" / "build.gradle.kts"])
    candidates.extend([root / "build.gradle", root / "build.gradle.kts"])

    groovy_lines = [
        "google()",
        "mavenCentral()",
        "maven { url 'https://jitpack.io' }",
    ]
    kotlin_lines = [
        "google()",
        "mavenCentral()",
        'maven { url = uri("https://jitpack.io") }',
    ]

    for grad in candidates:
        if not grad.is_file():
            continue
        content = grad.read_text(encoding="utf-8", errors="replace")
        if "jitpack.io" in content and "mavenCentral()" in content and (
            "google()" in content or "maven.google.com" in content
        ):
            continue

        is_kts = grad.name.endswith(".kts")
        repo_lines = kotlin_lines if is_kts else groovy_lines
        match = re.search(r"\brepositories\s*\{", content)
        if match:
            start = match.end()
            depth = 1
            i = start
            quote: str | None = None
            escaped = False
            while i < len(content) and depth:
                ch = content[i]
                if quote:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == quote:
                        quote = None
                elif ch in ("'", '"'):
                    quote = ch
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                i += 1
            if depth != 0:
                applied.append(f"Skipped repository injection in {grad}: unmatched braces")
                continue
            close_idx = i - 1
            block_content = content[start:close_idx]
            additions = []
            for line, marker in zip(repo_lines, ["google()", "mavenCentral()", "jitpack.io"]):
                if marker not in block_content:
                    additions.append(line)
            if additions:
                inject_text = "\n        // AndroidForge: ensure common public Maven repos\n        " + "\n        ".join(additions) + "\n    "
                content = content[:close_idx] + inject_text + content[close_idx:]
                grad.write_text(content, encoding="utf-8")
                applied.append(f"Injected repositories into {grad.relative_to(root)}")
        else:
            # Do not create allprojects/repositories automatically here: newer
            # projects may use FAIL_ON_PROJECT_REPOS or centralized settings.
            applied.append(f"Skipped repository injection in {grad.relative_to(root)}: no repositories block")


def apply_fixes(detect: dict, fixes: list[dict], android_sdk_root: str) -> list[str]:
    """Apply compatibility fixes to the extracted project tree."""
    applied: list[str] = []
    root = Path(detect["project_root"])
    props = root / "gradle" / "wrapper" / "gradle-wrapper.properties"

    for fix in fixes:
        name = fix["name"]
        if name == "migrate_https" and props.exists():
            content = props.read_text(encoding="utf-8", errors="replace")
            new_content = re.sub(r"distributionUrl=http://", "distributionUrl=https://", content)
            if new_content != content:
                props.write_text(new_content, encoding="utf-8")
                applied.append(f"Migrated wrapper distribution URL to HTTPS in {props}")
        elif name == "inject_local_properties":
            (root / "local.properties").write_text(f"sdk.dir={android_sdk_root}\n", encoding="utf-8")
            applied.append(f"Created local.properties with sdk.dir={android_sdk_root}")
        elif name == "disable_gradle_daemon":
            gradle_props = root / "gradle.properties"
            content = gradle_props.read_text(encoding="utf-8") if gradle_props.exists() else ""
            if "org.gradle.daemon" not in content:
                with gradle_props.open("a", encoding="utf-8") as f:
                    f.write("\n# AndroidForge: disable Gradle daemon for old Gradle\norg.gradle.daemon=false\n")
                applied.append("Disabled Gradle daemon via gradle.properties")
        elif name == "patch_gradle_wrapper":
            applied.append("Flagged wrapper for regeneration via `gradle wrapper`")

    heap_line = "org.gradle.jvmargs=-Xmx4g -XX:+UseG1GC -Dfile.encoding=UTF-8"
    androidx_lines = ["android.useAndroidX=true", "android.enableJetifier=true"]

    def inject_gradle_properties(props_path: Path, label: str) -> None:
        if props_path.exists():
            content = props_path.read_text(encoding="utf-8", errors="replace")
            changes: list[str] = []
            match = re.search(r"^org\.gradle\.jvmargs\s*=\s*(.+)$", content, re.MULTILINE)
            if match:
                existing = match.group(1).strip()
                heap_match = re.search(r"-Xmx(\d+)([gm])", existing, re.IGNORECASE)
                if heap_match:
                    size = int(heap_match.group(1))
                    unit = heap_match.group(2).lower()
                    existing_mb = size * (1024 if unit == "g" else 1)
                    if existing_mb < 4096:
                        content = re.sub(
                            r"^org\.gradle\.jvmargs\s*=.*$",
                            heap_line,
                            content,
                            flags=re.MULTILINE,
                        )
                        changes.append(f"bumped jvmargs from '{existing}'")
            else:
                content += f"\n# AndroidForge: ensure enough heap for Gradle\n{heap_line}\n"
                changes.append("added jvmargs")

            if not re.search(r"^android\.useAndroidX\s*=", content, re.MULTILINE):
                content += "\n# AndroidForge: enable AndroidX\n"
                content += "".join(line + "\n" for line in androidx_lines)
                changes.append("added AndroidX flags")
            if changes:
                props_path.write_text(content, encoding="utf-8")
                applied.append(f"Updated {label}: {', '.join(changes)}")
        else:
            props_path.parent.mkdir(parents=True, exist_ok=True)
            content = (
                "# AndroidForge: ensure enough heap for Gradle\n"
                f"{heap_line}\n"
                "org.gradle.daemon=false\n"
                "# AndroidForge: enable AndroidX\n"
                + "".join(line + "\n" for line in androidx_lines)
            )
            props_path.write_text(content, encoding="utf-8")
            applied.append(f"Created {label} with JVM and AndroidX settings")

    inject_gradle_properties(root / "gradle.properties", "gradle.properties (root)")
    android_dir = root / "android"
    if android_dir.is_dir():
        inject_gradle_properties(android_dir / "gradle.properties", "android/gradle.properties")
    inject_gradle_properties(Path.home() / ".gradle" / "gradle.properties", "~/.gradle/gradle.properties")

    _inject_repositories(root, applied, is_flutter=bool((detect.get("flutter") or {}).get("is_flutter")))
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description="AndroidForge toolchain setup")
    parser.add_argument("--root", required=True, help="Project root path")
    parser.add_argument("--detect", required=True, help="Detection JSON (string or file path)")
    parser.add_argument("--rules", default=None, help="Path to toolchain-rules.yaml")
    parser.add_argument("--android-sdk", default=os.environ.get("ANDROID_SDK_ROOT", "/usr/local/lib/android/sdk"))
    parser.add_argument("--output", default=None, help="Write JSON summary to this file")
    args = parser.parse_args()

    rules = load_rules(Path(args.rules) if args.rules else None)
    detect_path = Path(args.detect)
    detect = json.loads(detect_path.read_text(encoding="utf-8") if detect_path.exists() else args.detect)

    versions = detect.get("versions", {}) or {}
    wrapper = detect.get("wrapper", {}) or {}
    agp = versions.get("agp_version")
    gradle_wrapper_v = wrapper.get("version")
    java_in_build = versions.get("java_version")
    kotlin_v = versions.get("kotlin_version")

    jdk_candidates = [pick_jdk_for_agp(agp, rules), pick_jdk_for_gradle(gradle_wrapper_v, rules)]
    if java_in_build:
        jdk_candidates.append(str(java_in_build))
    chosen_jdk = max(jdk_candidates, key=lambda v: (major(v), minor(v)))

    chosen_gradle = gradle_wrapper_v or pick_gradle_for_agp(agp, rules) or "8.0"
    use_wrapper = bool(wrapper.get("present")) or bool(wrapper.get("uses_http"))
    needs_flutter = bool((detect.get("flutter") or {}).get("is_flutter"))
    chosen_flutter = pick_flutter_version(detect, rules) if needs_flutter else None
    needs_ndk = bool((detect.get("native") or {}).get("has_native"))
    chosen_ndk = pick_ndk_version(detect, rules) if needs_ndk else None

    fixes = determine_legacy_fixes(detect, rules)
    applied_fixes = apply_fixes(detect, fixes, args.android_sdk)

    prep_commands: list[list[str]] = []
    if needs_flutter:
        prep_commands = [["flutter", "pub", "get"]]
        build_commands = [["flutter", "build", "apk", "--debug"], ["flutter", "build", "apk", "--release"]]
    else:
        gradlew = Path(detect["project_root"]) / "gradlew"
        gradle_cmd = str(gradlew) if use_wrapper and gradlew.exists() else "gradle"
        build_commands = [[gradle_cmd, "assembleDebug"], [gradle_cmd, "assembleRelease"], [gradle_cmd, "bundleDebug"]]

    result = {
        "project_root": detect["project_root"],
        "jdk_version": chosen_jdk,
        "gradle_version": chosen_gradle,
        "agp_version": agp,
        "kotlin_version": kotlin_v,
        "ndk_version": chosen_ndk,
        "flutter_version": chosen_flutter,
        "use_gradle_wrapper": use_wrapper,
        "needs_flutter": needs_flutter,
        "needs_ndk": needs_ndk,
        "needs_gradle": detect.get("project_type") in ("gradle", "flutter"),
        "legacy_fixes_applied": applied_fixes,
        "prep_commands": prep_commands,
        "build_commands": build_commands,
    }

    output = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output)

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"jdk_version={chosen_jdk}\n")
            f.write(f"gradle_version={chosen_gradle}\n")
            if agp:
                f.write(f"agp_version={agp}\n")
            if kotlin_v:
                f.write(f"kotlin_version={kotlin_v}\n")
            if chosen_ndk:
                f.write(f"ndk_version={chosen_ndk}\n")
            f.write(f"needs_flutter={'true' if needs_flutter else 'false'}\n")
            f.write(f"needs_ndk={'true' if needs_ndk else 'false'}\n")
            f.write(f"needs_gradle={'true' if result['needs_gradle'] else 'false'}\n")
            f.write(f"use_wrapper={'true' if use_wrapper else 'false'}\n")
            if chosen_flutter:
                f.write(f"flutter_version={chosen_flutter}\n")
            f.write(f"json<<EOF\n{json.dumps(result)}\nEOF\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
