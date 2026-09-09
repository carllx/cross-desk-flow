#!/usr/bin/env python3
"""repo_guard.py - Repository preflight and Code Context Guard validator.

Checks:
1. Context Guard (context):
   - Validates git remote URL belongs to carllx/cross-desk-flow.
   - Validates AGENTS.md declares carllx/cross-desk-flow repository identity.
   - If --mission-base is specified, validates that the current branch is an ancestor/successor of the mission SHA.
2. Code Context Guard (code-context):
   - Scans all tracked code files in the repository (excluding tests, generated files, fixtures, vendor, snapshots, and caches).
   - Enforces tiered thresholds on active handwritten code files:
     - <= 600 LOC: PASS
     - 601 - 700 LOC: WARN
     - > 700 LOC: FAIL (unless grandfathered legacy file meeting no-worse rule)
   - For grandfathered legacy files (> 700 LOC in base-ref):
     - Uses --base-ref <SHA> to compute baseline LOC.
     - Enforces No-Worse rule: final LOC <= base-ref LOC.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

EXCLUDE_DIRS = {
    ".git",
    "tests",
    "fixtures",
    "snapshots",
    "build",
    "dist",
    "vendor",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
}

EXCLUDE_EXTENSIONS = {
    ".json",
    ".md",
    ".txt",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".patch",
    ".log",
    ".lock",
}


def run_git(cmd: List[str], cwd: Optional[Path] = None) -> str:
    """Runs a git command and returns stripped stdout."""
    res = subprocess.run(
        ["git"] + cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def check_context(root_dir: Path, mission_base: Optional[str] = None) -> bool:
    """Validates cross-machine repository identity and worktree constraints."""
    print("=== [Context Guard] Checking repository identity ===")
    errors: List[str] = []

    # 1. Git remote origin check
    try:
        remote_url = run_git(["remote", "get-url", "origin"], cwd=root_dir)
        print(f"  Remote origin: {remote_url}")
        if "carllx/cross-desk-flow" not in remote_url.replace("\\", "/"):
            errors.append(f"Remote origin '{remote_url}' is not 'carllx/cross-desk-flow'")
    except Exception as exc:
        errors.append(f"Failed to query git remote origin: {exc}")

    # 2. AGENTS.md identity declaration check
    agents_file = root_dir / "AGENTS.md"
    if not agents_file.is_file():
        errors.append("AGENTS.md is missing from repository root")
    else:
        content = agents_file.read_text(encoding="utf-8")
        if "carllx/cross-desk-flow" not in content:
            errors.append("AGENTS.md does not declare 'carllx/cross-desk-flow' identity")
        else:
            print("  AGENTS.md identity declaration: carllx/cross-desk-flow OK")

    # 3. Mission base commit check
    if mission_base:
        try:
            # Verify mission_base exists in history
            merge_base = run_git(["merge-base", mission_base, "HEAD"], cwd=root_dir)
            if merge_base != mission_base:
                # Check if HEAD is descendant or ancestor
                is_ancestor = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", mission_base, "HEAD"],
                    cwd=root_dir,
                ).returncode == 0
                if not is_ancestor:
                    errors.append(f"Commit {mission_base} is not in HEAD ancestry tree")
                else:
                    print(f"  Mission base {mission_base[:8]} verified in ancestry tree")
            else:
                print(f"  Mission base {mission_base[:8]} verified in ancestry tree")
        except Exception as exc:
            errors.append(f"Failed to check mission base {mission_base}: {exc}")

    if errors:
        print("[FAIL] Context Guard violations:")
        for err in errors:
            print(f"  - {err}")
        return False

    print("[PASS] Context Guard verified successfully.\n")
    return True


def get_base_ref_loc(root_dir: Path, base_ref: str, rel_path: str) -> Optional[int]:
    """Retrieves line count of rel_path at base_ref git revision."""
    try:
        content = run_git(["show", f"{base_ref}:{rel_path}"], cwd=root_dir)
        return len(content.splitlines())
    except Exception:
        return None


def check_code_context(root_dir: Path, base_ref: Optional[str] = None) -> bool:
    """Validates LOC limits and No-Worse grandfathered legacy policy."""
    print("=== [Code Context Guard] Checking file size & complexity ===")
    if base_ref:
        print(f"  Comparison base reference: {base_ref}")

    # List all tracked files
    try:
        tracked_out = run_git(["ls-files"], cwd=root_dir)
        tracked_files = tracked_out.splitlines()
    except Exception as exc:
        print(f"[FAIL] Could not list tracked files: {exc}")
        return False

    failures: List[str] = []
    warnings: List[str] = []

    for rel_str in tracked_files:
        p = root_dir / rel_str
        if not p.is_file():
            continue

        # Check directory exclusions
        parts = Path(rel_str).parts
        if any(part in EXCLUDE_DIRS for part in parts):
            continue

        # Check extension exclusions
        if p.suffix in EXCLUDE_EXTENSIONS:
            continue

        # Count lines
        try:
            loc = len(p.read_text(encoding="utf-8").splitlines())
        except Exception:
            continue

        if loc <= 600:
            continue

        rel_path_posix = rel_str.replace("\\", "/")

        if loc <= 700:
            warnings.append(f"WARN: {rel_path_posix} ({loc} LOC) - Exceeds 600 LOC guideline")
            continue

        # File > 700 LOC: check if grandfathered legacy file with base-ref
        if base_ref:
            base_loc = get_base_ref_loc(root_dir, base_ref, rel_path_posix)
            if base_loc is not None and base_loc > 700:
                # Grandfathered legacy file: apply No-Worse rule
                if loc > base_loc:
                    failures.append(
                        f"FAIL: {rel_path_posix} ({loc} LOC) - Grandfathered legacy exceeded baseline ({base_loc} LOC, +{loc - base_loc} lines)"
                    )
                else:
                    print(
                        f"  [LEGACY NO-WORSE PASS] {rel_path_posix}: {loc} LOC (baseline: {base_loc} LOC, change: {loc - base_loc})"
                    )
                continue

        failures.append(f"FAIL: {rel_path_posix} ({loc} LOC) - Exceeds hard limit of 700 LOC")

    for warn in warnings:
        print(f"  [!] {warn}")

    if failures:
        print("\n[FAIL] Code Context Guard violations:")
        for fail in failures:
            print(f"  - {fail}")
        return False

    print("[PASS] Code Context Guard verified successfully.\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Repository and Code Context Guard.")
    parser.add_argument(
        "subcommand",
        choices=["all", "context", "code-context"],
        nargs="?",
        default="all",
        help="Subcommand to execute (default: all)",
    )
    parser.add_argument(
        "--mission-base",
        type=str,
        default=None,
        help="Expected base or historical commit SHA for context verification",
    )
    parser.add_argument(
        "--base-ref",
        type=str,
        default=None,
        help="Pre-#43 base commit SHA for legacy file No-Worse comparison",
    )

    args = parser.parse_args()
    root_dir = Path(__file__).resolve().parent.parent

    success = True
    if args.subcommand in ("all", "context"):
        if not check_context(root_dir, mission_base=args.mission_base):
            success = False

    if args.subcommand in ("all", "code-context"):
        if not check_code_context(root_dir, base_ref=args.base_ref):
            success = False

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
