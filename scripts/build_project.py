
#!/usr/bin/env python3
"""AndroidForge — Build Project.

Reads detection + setup JSON and runs the appropriate build command(s).

Strategy:
  1. If Flutter -> run preparation commands and Flutter build commands.
  2. Otherwise -> try configured Gradle build commands until one succeeds.
  3. Prefer the selected Gradle version when using system Gradle.
  4. Preserve wrapper execution when the wrapper is usable.
  5. Write build results to JSON and GITHUB_OUTPUT.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def run(
    cmd: list[str],
    cwd: Path,
    log_file: Path,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run a command and stream output to console and log."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    print(f"\n=== Running: {' '.join(cmd)}", flush=True)
    print(f"    cwd: {cwd}", flush=True)
    print(f"    log: {log_file}", flush=True)

    start = time.time()
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    with log_file.open("w", encoding="utf-8", errors="replace") as f:
        f.write(f"$ {' '.join(cmd)}\n")
        f.write(f"# cwd: {cwd}\n\n")
        f.flush()

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=full_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            message = f"ERROR: Could not start command: {exc}\n"
            print(message, flush=True)
            f.write(message)
            return 127, str(log_file)

        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            f.write(line)

        rc = proc.wait()

    elapsed = time.time() - start
    print(f"=== Exit {rc} after {elapsed:.1f}s", flush=True)
    return rc, str(log_file)


def version_tuple(value: str) -> tuple[int, ...]:
    """Convert a version such as 8.11.1 to a comparable tuple."""
    parts = []
    for item in value.strip().split("."):
        digits = ""
        for char in item:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def maybe_regenerate_wrapper(
    project_root: Path,
    gradle_version: str | None,
    log_dir: Path,
    gradle_executable: str | None,
) -> bool:
    """Attempt wrapper regeneration if its JAR is missing."""
    jar = project_root / "gradle" / "wrapper" / "gradle-wrapper.jar"
    if jar.is_file():
        return True

    if not gradle_version or not gradle_executable:
        print(
            "WARNING: Cannot regenerate wrapper: Gradle version "
            "or executable is unavailable.",
            flush=True,
        )
        return False

    cmd = [
        gradle_executable,
        "wrapper",
        "--gradle-version",
        gradle_version,
        "--distribution-type",
        "bin",
    ]
    rc, _ = run(cmd, project_root, log_dir / "regenerate-wrapper.log")
    return rc == 0 and jar.is_file()


def load_json_argument(value: str) -> Any:
    """Load JSON from a file path or directly from a JSON string."""
    path = Path(value)
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        pass
    return json.loads(value)


def replace_system_gradle(
    commands: list,
    gradle_executable: str,
) -> list[list[str]]:
    """Replace only commands that invoke the bare system 'gradle'."""
    updated: list[list[str]] = []

    for item in commands:
        command = list(item) if isinstance(item, (list, tuple)) else str(item).split()

        if command and command[0] == "gradle":
            command[0] = gradle_executable

        updated.append(command)

    return updated


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AndroidForge build project"
    )
    parser.add_argument("--root", required=True, help="Project root path")
    parser.add_argument("--detect", required=True, help="Detection JSON (string or path)")
    parser.add_argument("--toolchain", required=True, help="Toolchain JSON (string or path)")
    parser.add_argument(
        "--variant",
        default="auto",
        choices=["auto", "debug", "release", "bundle"],
    )
    parser.add_argument("--log-dir", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    try:
        detect = load_json_argument(args.detect)
        toolchain = load_json_argument(args.toolchain)
    except (ValueError, OSError) as exc:
        print(f"ERROR: Could not read configuration JSON: {exc}", flush=True)
        return 2

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"ERROR: Project root does not exist: {root}", flush=True)
        return 2

    log_dir = (
        Path(args.log_dir).resolve()
        if args.log_dir
        else root.parent / "androidforge-logs"
    )
    log_dir.mkdir(parents=True, exist_ok=True)

    project_type = detect.get("project_type", "unknown")
    needs_gradle = bool(toolchain.get("needs_gradle", False))
    needs_flutter = bool(toolchain.get("needs_flutter", False))
    use_wrapper = bool(toolchain.get("use_wrapper", False))

    gradle_version = str(
        toolchain.get("gradle_version") or ""
    ).strip()

    # Locate the exact Gradle distribution installed by the workflow.
    gradle_executable = None

    if gradle_version:
        candidate = Path(
            f"/opt/gradle-{gradle_version}/bin/gradle"
        )
        if candidate.is_file():
            gradle_executable = str(candidate)

    # Fall back to PATH only when the expected distribution is unavailable.
    if not gradle_executable:
        gradle_executable = shutil.which("gradle")

    if needs_gradle and not use_wrapper:
        if not gradle_executable:
            print(
                "ERROR: System Gradle was requested but no executable "
                "was found.",
                flush=True,
            )
            return 1

        print(
            f"Selected Gradle executable: {gradle_executable}",
            flush=True,
        )
        print(
            f"Configured Gradle version: "
            f"{gradle_version or 'unknown'}",
            flush=True,
        )

        # Verify the executable before starting the build.
        version_rc, _ = run(
            [gradle_executable, "--version"],
            root,
            log_dir / "gradle-version.log",
        )
        if version_rc != 0:
            print("ERROR: Gradle version check failed.", flush=True)
            return version_rc

    all_commands = toolchain.get("build_commands", [])
    prep_commands = toolchain.get("prep_commands", []) or []

    # Filter build commands for the requested variant.
    if args.variant != "auto":
        filtered = []

        for cmd in all_commands:
            command = (
                cmd if isinstance(cmd, (list, tuple))
                else str(cmd).split()
            )
            cmd_str = " ".join(command).lower()

            if args.variant == "debug" and "debug" in cmd_str:
                filtered.append(command)
            elif args.variant == "release" and "release" in cmd_str:
                filtered.append(command)
            elif args.variant == "bundle" and "bundle" in cmd_str:
                filtered.append(command)

        if filtered:
            all_commands = filtered

    # Use the selected system Gradle for bare 'gradle' commands.
    if needs_gradle and not use_wrapper and gradle_executable:
        all_commands = replace_system_gradle(
            all_commands, gradle_executable
        )
        prep_commands = replace_system_gradle(
            prep_commands, gradle_executable
        )

    # Handle a missing wrapper without silently assuming it is usable.
    if use_wrapper and project_type != "flutter":
        gradlew = root / "gradlew"

        if not gradlew.is_file():
            print(
                "WARNING: gradlew is missing; attempting regeneration.",
                flush=True,
            )
            regenerated = maybe_regenerate_wrapper(
                root,
                gradle_version or None,
                log_dir,
                gradle_executable,
            )

            if not regenerated:
                print(
                    "ERROR: Wrapper regeneration failed. "
                    "Check wrapper files and toolchain configuration.",
                    flush=True,
                )
                return 1

    build_env: dict[str, str] = {}

    # Run preparation commands. Their failure is logged as a warning.
    if prep_commands:
        print(
            f"\n=== Running {len(prep_commands)} preparation command(s) ===",
            flush=True,
        )

    for index, original_cmd in enumerate(prep_commands):
        cmd = list(original_cmd)

        if use_wrapper and cmd and cmd[0].endswith("gradlew"):
            gradlew_path = Path(cmd[0])
            if not gradlew_path.is_absolute():
                gradlew_path = root / gradlew_path

            if gradlew_path.is_file():
                try:
                    gradlew_path.chmod(
                        gradlew_path.stat().st_mode | 0o111
                    )
                except OSError:
                    pass

                cmd = ["/bin/sh", str(gradlew_path)] + cmd[1:]

        rc, _ = run(
            cmd,
            root,
            log_dir / f"prep-{index}-{int(time.time())}.log",
            build_env,
        )

        if rc != 0:
            print(
                f"WARNING: Preparation command failed ({rc}): "
                f"{' '.join(cmd)}",
                flush=True,
            )
        else:
            print(f"Preparation command succeeded: {' '.join(cmd)}", flush=True)

    # Run build commands until the first successful build.
    success = False
    last_rc = 1
    last_log = ""
    successful_command = None
    attempted_commands = []

    for index, original_cmd in enumerate(all_commands):
        cmd = list(original_cmd)

        if use_wrapper and cmd and cmd[0].endswith("gradlew"):
            gradlew_path = Path(cmd[0])
            if not gradlew_path.is_absolute():
                gradlew_path = root / gradlew_path

            if gradlew_path.is_file():
                try:
                    gradlew_path.chmod(
                        gradlew_path.stat().st_mode | 0o111
                    )
                except OSError:
                    pass

                cmd = ["/bin/sh", str(gradlew_path)] + cmd[1:]

        attempted_commands.append(cmd)

        rc, log = run(
            cmd,
            root,
            log_dir / f"build-{index}-{int(time.time())}.log",
            build_env,
        )

        last_rc = rc
        last_log = log

        if rc == 0:
            success = True
            successful_command = cmd
            break

    result = {
        "project_root": str(root),
        "project_type": project_type,
        "build_succeeded": success,
        "successful_command": successful_command,
        "exit_code": last_rc,
        "log_file": last_log,
        "gradle_version": gradle_version or None,
        "gradle_executable": gradle_executable,
        "use_wrapper": use_wrapper,
        "prep_commands_run": prep_commands,
        "build_commands_attempted": attempted_commands,
    }

    output = json.dumps(result, indent=2)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output)

    gh_output = os.environ.get("GITHUB_OUTPUT")

    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(
                f"build_succeeded={'true' if success else 'false'}\n"
            )
            f.write(f"exit_code={last_rc}\n")
            f.write(f"log_file={last_log}\n")

            if successful_command:
                f.write(
                    f"successful_command={' '.join(successful_command)}\n"
                )

            f.write("json<<EOF\n")
            f.write(json.dumps(result) + "\n")
            f.write("EOF\n")

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
