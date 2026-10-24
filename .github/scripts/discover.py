#!/usr/bin/env python3
"""Inspect this repository and runner, and verify the UI protection manifest.

This is a repository-specific discovery tool: Titanium's UI is produced by
patch.sh sed edits plus branding assets, not by a conventional app/ source
directory, so the manifest is checked against the real patch.sh headers.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import sys
from pathlib import Path

from automation_lib import (
    SECTION_HEADER_RE,
    AutomationError,
    append_step_summary,
    git,
    git_out,
    load_manifest,
    now_utc,
    write_json,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--manifest", default=".github/ui-protection.yml")
    parser.add_argument("--output-dir", default="discovery-report")
    parser.add_argument(
        "--build-host",
        action="store_true",
        help="fail when disk/RAM/tool prerequisites for a full build are missing",
    )
    return parser.parse_args(argv)


def patch_headers(path: Path) -> list[dict]:
    headers: list[dict] = []
    if not path.is_file():
        return headers
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        match = SECTION_HEADER_RE.match(line.rstrip())
        if match:
            headers.append({"header": match.group(1).strip(), "line": number})
    return headers


def memory_gib() -> float | None:
    if platform.system() == "Linux":
        try:
            text = Path("/proc/meminfo").read_text(encoding="utf-8")
            match = re.search(r"^MemTotal:\s+(\d+) kB$", text, re.MULTILINE)
            if match:
                return round(int(match.group(1)) / 1024 / 1024, 1)
        except OSError:
            return None
    return None


HOST_MOUNT_ENV = "TITANIUM_WSL_HOST_MOUNT"


def wsl_virtual_disk() -> bool:
    """True when running inside WSL2, where the distro root's free space lies.

    WSL2 mounts a sparse VHDX, so statvfs reports the virtual disk size (a
    terabyte by default) rather than the space actually left on the Windows
    volume backing it. A build that trusts the figure fills the real volume and
    dies partway through `gclient sync`. Probing by reserving `min_disk` bytes
    is not a fix either: the reservation is what grows the VHDX and it is not
    given back when the probe file is deleted, so the check would consume the
    very space the build needs.
    """
    return "microsoft-standard-WSL" in platform.release()


def host_volume_free_gib(mount: str) -> float | None:
    """Free space on the Windows volume behind `mount`, or None if unproven.

    WSL reaches each host volume over 9p/drvfs, and statvfs there reports that
    volume's real free space, which is the room the VHDX can grow into. Only the
    Windows side knows which volume holds the VHDX, so the operator names the
    mount on the build host through ``HOST_MOUNT_ENV``; it is read from the
    environment rather than the manifest because a workflow file comes from the
    base branch and the host's environment cannot be set by a pull request.
    Accepting only those filesystem types is what stops a tmpfs such as
    /dev/shm from rubber-stamping the disk gate.
    """
    target = os.path.realpath(mount)
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) < 3 or fields[1] != target:
            continue
        if not re.search(r"drvfs|9p|fuse", fields[2]):
            return None
        try:
            return round(shutil.disk_usage(target).free / 1024**3, 1)
        except OSError:
            return None
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    manifest = load_manifest(repo / args.manifest)
    build = manifest.get("build", {})
    errors: list[str] = []
    warnings: list[str] = []

    actual = patch_headers(repo / "patch.sh")
    actual_headers = [entry["header"] for entry in actual]
    declared = [section["header"] for section in manifest["patch_sections"]]
    missing = [header for header in actual_headers if header not in declared]
    unused = [header for header in declared if header not in actual_headers]
    duplicates = sorted(
        {header for header in actual_headers if actual_headers.count(header) > 1}
    )
    if missing:
        errors.append(
            "patch.sh sections missing from the manifest: " + ", ".join(missing)
        )
    if unused:
        errors.append(
            "manifest sections no longer present in patch.sh: " + ", ".join(unused)
        )
    if duplicates:
        errors.append("duplicate patch.sh section headers: " + ", ".join(duplicates))
    if not actual:
        errors.append("no section headers found in patch.sh")

    for patch_file in manifest["patch_files"]:
        if not (repo / patch_file).is_file():
            errors.append(f"patch file does not exist: {patch_file}")

    root_args = (repo / "args.gn").read_text(encoding="utf-8", errors="replace")
    package_match = re.search(
        r'^chrome_public_manifest_package\s*=\s*"([^"]+)"', root_args, re.MULTILINE
    )
    vanadium_args = repo / "vanadium" / "args.gn"
    version = None
    if vanadium_args.is_file():
        match = re.search(
            r"[0-9]+(?:\.[0-9]+){3}",
            vanadium_args.read_text(encoding="utf-8", errors="replace"),
        )
        version = match.group(0) if match else None
    else:
        warnings.append(
            "vanadium/args.gn is not present (submodule not initialized); the "
            "Chromium version cannot be discovered locally."
        )

    submodules = git(
        "submodule", "status", cwd=repo, check=False
    ).stdout.strip()
    try:
        head = git_out("rev-parse", "HEAD", cwd=repo)
        tree = git_out("rev-parse", "HEAD^{tree}", cwd=repo)
        branch = git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    except AutomationError:
        head = tree = branch = ""

    usage = shutil.disk_usage(repo)
    free_gib = round(usage.free / 1024**3, 1)
    ram_gib = memory_gib()
    tools = {
        name: shutil.which(name)
        for name in ("git", "python3", "bash", "ruby", "java", "curl")
    }
    min_disk = float(build.get("min_free_disk_gib", 100))
    min_ram = float(build.get("min_ram_gib", 8))
    recommended_ram = float(build.get("recommended_ram_gib", 32))
    virtual_disk = wsl_virtual_disk()
    named_mount = os.environ.get(HOST_MOUNT_ENV, "").strip()
    host_free_gib = (
        host_volume_free_gib(named_mount) if virtual_disk and named_mount else None
    )
    disk_verifiable = not virtual_disk or host_free_gib is not None
    if host_free_gib is not None:
        # The build is bounded by the host volume, never by the VHDX virtual size.
        free_gib = host_free_gib
    disk_ok = free_gib >= min_disk and disk_verifiable
    ram_ok = ram_gib is None or ram_gib >= min_ram
    ram_recommended = ram_gib is None or ram_gib >= recommended_ram
    if virtual_disk and host_free_gib is None:
        message = (
            f"free disk is unverifiable on WSL2: {free_gib} GiB is the sparse VHDX "
            "virtual size, not the space left on the Windows volume behind it. Build "
            f"on a native Linux host, or name that volume on this host with "
            f"{HOST_MOUNT_ENV}=/mnt/<letter> so the gate can read its real free space"
        )
        if named_mount:
            message += f", which {named_mount} is not a mount of."
        (errors if args.build_host else warnings).append(message)
    elif not disk_ok:
        message = (
            f"free disk {free_gib} GiB is below the Chromium minimum {min_disk} GiB"
        )
        (errors if args.build_host else warnings).append(message)
    if not ram_ok:
        message = (
            f"RAM {ram_gib} GiB is below the Chromium minimum {min_ram} GiB"
        )
        (errors if args.build_host else warnings).append(message)
    elif not ram_recommended:
        warnings.append(
            f"RAM {ram_gib} GiB is below the recommended {recommended_ram} GiB; "
            "the build may be slow or run out of memory"
        )
    if args.build_host:
        for name in ("git", "python3", "bash", "java"):
            if not tools[name]:
                errors.append(f"required build tool is missing: {name}")
        if platform.system() != "Linux":
            errors.append("a full Chromium build requires Linux")
    elif platform.system() != "Linux":
        warnings.append(
            "this inspection host is not Linux; full Android builds must run on "
            "the configured Linux build runner."
        )

    capacity = {
        "cpu_count": os.cpu_count(),
        "ram_gib": ram_gib,
        "free_disk_gib": free_gib,
        "min_free_disk_gib": min_disk,
        "min_ram_gib": min_ram,
        "recommended_ram_gib": recommended_ram,
        "meets_disk_requirement": disk_ok,
        "disk_verifiable": disk_verifiable,
        "disk_source": (
            f"host volume {named_mount}"
            if host_free_gib is not None
            else ("sparse VHDX virtual size" if virtual_disk else "filesystem")
        ),
        "meets_ram_requirement": ram_ok,
        "meets_recommended_ram": ram_recommended,
        "build_ready": disk_ok and ram_ok and platform.system() == "Linux",
    }

    workflows = sorted(
        path.name
        for path in (repo / ".github" / "workflows").glob("*.yml")
    )
    required_workflows = {
        "inspect-project.yml",
        "upstream-sync.yml",
        "ui-protection.yml",
        "upstream-review.yml",
        "validate-build.yml",
        "release.yml",
    }
    absent_workflows = sorted(required_workflows - set(workflows))
    if absent_workflows:
        warnings.append(
            "automation workflows not present yet: " + ", ".join(absent_workflows)
        )

    report = {
        "schema_version": 1,
        "generated_at": now_utc(),
        "repository": str(repo),
        "git": {"head": head, "tree": tree, "branch": branch},
        "manifest": {
            "path": args.manifest,
            "path_rules": len(manifest["path_rules"]),
            "patch_sections": len(manifest["patch_sections"]),
            "state_branch": manifest["policy"]["state_branch"],
            "candidate_branch": manifest["policy"]["candidate_branch"],
            "review_branch": manifest["policy"]["review_branch"],
            "auto_merge": manifest["policy"].get("auto_merge", False),
        },
        "patch_sh": {
            "sections": actual,
            "missing_from_manifest": missing,
            "unused_manifest_sections": unused,
            "duplicate_sections": duplicates,
        },
        "product": {
            "package_id": package_match.group(1) if package_match else None,
            "chromium_version": version,
            "submodules": submodules.splitlines() if submodules else [],
        },
        "runner": {
            "platform": platform.platform(),
            "system": platform.system(),
            "cpu_count": os.cpu_count(),
            "ram_gib": ram_gib,
            "free_disk_gib": free_gib,
            "tools": tools,
        },
        "capacity": capacity,
        "workflows": workflows,
        "errors": errors,
        "warnings": warnings,
        "ok": not errors,
    }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "discovery.json", report)

    lines = [
        "# Repository discovery",
        "",
        f"- Commit: `{head}`",
        f"- Tree: `{tree}`",
        f"- Package id: `{report['product']['package_id'] or 'unknown'}`",
        f"- Chromium version: `{version or 'unknown'}`",
        f"- patch.sh sections: {len(actual)} (manifest: {len(declared)})",
        f"- Path rules: {len(manifest['path_rules'])}",
        f"- Host: {platform.platform()}",
        f"- CPUs: {capacity['cpu_count']}",
        f"- Free disk: {free_gib} GiB (minimum {min_disk}, "
        f"{'ok' if disk_ok else 'INSUFFICIENT'}, from {capacity['disk_source']})",
        f"- RAM: {ram_gib if ram_gib is not None else 'unknown'} GiB "
        f"(minimum {min_ram}, recommended {recommended_ram}, "
        f"{'ok' if ram_ok else 'INSUFFICIENT'})",
        f"- Chromium build ready: **{capacity['build_ready']}**",
        "",
        "## patch.sh sections",
        "",
        "| Header | Line | Protection |",
        "| --- | --- | --- |",
    ]
    declared_map = {
        section["header"]: section["protection"]
        for section in manifest["patch_sections"]
    }
    for entry in actual:
        lines.append(
            f"| {entry['header']} | {entry['line']} | "
            f"`{declared_map.get(entry['header'], 'MISSING')}` |"
        )
    if report["product"]["submodules"]:
        lines.extend(["", "## Submodules", "", "```", submodules, "```"])
    if errors:
        lines.extend(["", "## Errors", "", *[f"- {item}" for item in errors]])
    if warnings:
        lines.extend(["", "## Warnings", "", *[f"- {item}" for item in warnings]])
    lines.append("")
    summary = "\n".join(lines)
    (output_dir / "discovery.md").write_text(summary, encoding="utf-8")
    append_step_summary(summary)
    print(summary)
    if errors:
        for message in errors:
            print(f"ERROR: {message}", file=sys.stderr)
        if not disk_ok and not virtual_disk:
            print(
                f"ERROR: resize the runner to at least {min_disk} GiB free disk "
                "before starting a Chromium build; do not lower the threshold.",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
