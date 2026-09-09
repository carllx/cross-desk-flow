"""Deterministic negative tests for Context Guard (scripts/repo_guard.py).

Validates that wrong origin, wrong repository identity, or missing/corrupted
AGENTS.md causes fail-closed behavior (returns False and non-zero exit code).
Does NOT access or scan any external repository.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.repo_guard import check_context


def test_context_guard_wrong_origin_fails(tmp_path: Path) -> None:
    """Proves that a git repo with wrong origin URL fails closed."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/other-user/wrong-repo.git"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "AGENTS.md").write_text("Repository: carllx/cross-desk-flow\n", encoding="utf-8")

    result = check_context(tmp_path)
    assert result is False, "Context Guard must fail closed when remote origin is not carllx/cross-desk-flow"


def test_context_guard_missing_agents_file_fails(tmp_path: Path) -> None:
    """Proves that missing AGENTS.md fails closed even if origin is correct."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/carllx/cross-desk-flow.git"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    result = check_context(tmp_path)
    assert result is False, "Context Guard must fail closed when AGENTS.md is missing"


def test_context_guard_wrong_agents_identity_fails(tmp_path: Path) -> None:
    """Proves that incorrect repo identity in AGENTS.md fails closed."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/carllx/cross-desk-flow.git"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "AGENTS.md").write_text("Repository: some-other/project\n", encoding="utf-8")

    result = check_context(tmp_path)
    assert result is False, "Context Guard must fail closed when AGENTS.md lacks carllx/cross-desk-flow"


def test_context_guard_cli_wrong_context_exits_nonzero(tmp_path: Path) -> None:
    """Proves that CLI invocation exits with non-zero code on context failure."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/alien/alien-repo.git"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "AGENTS.md").write_text("Repository: alien/alien-repo\n", encoding="utf-8")

    guard_script = REPO_ROOT / "scripts" / "repo_guard.py"
    res = subprocess.run(
        [sys.executable, str(guard_script), "context"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0, "CLI repo_guard context must return non-zero exit code on wrong repo"
    assert "Remote origin" in res.stdout or "FAIL" in res.stdout
