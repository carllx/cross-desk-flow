"""Automated regression tests for macOS CLI status commands (human-readable and JSON)."""

import json
import subprocess
import sys


def test_macos_cli_status_human_readable():
    """Verifies that python -m macos.cli status outputs valid text and does not crash with NameError."""
    res = subprocess.run(
        [sys.executable, "-m", "macos.cli", "status"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Expected 0, got {res.returncode}. Stderr: {res.stderr}"
    stdout = res.stdout
    assert "=== desk-audio-bridge macOS Controller Status ===" in stdout
    assert "Controller State:" in stdout
    assert "Desired State:" in stdout
    assert "Host Role:" in stdout
    assert "Microphone Path State:" in stdout
    assert "Microphone Port:" in stdout


def test_macos_cli_status_json():
    """Verifies that python -m macos.cli status --json outputs valid JSON matching contract."""
    res = subprocess.run(
        [sys.executable, "-m", "macos.cli", "status", "--json"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Expected 0, got {res.returncode}. Stderr: {res.stderr}"
    data = json.loads(res.stdout)
    assert "controller_state" in data
    assert "desired_state" in data
    assert data["role"] == "macos"
    assert "speaker_path_state" in data
    assert "microphone_path_state" in data
    assert "microphone_port" in data
