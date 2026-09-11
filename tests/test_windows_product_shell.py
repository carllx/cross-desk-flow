"""Tests for Issue #42: Windows Product MVP Shell.

Verifies:
- Pure state mapping rules:
  * connected
  * waiting for Mac
  * degraded (including real Pack43 UNAVAILABLE state)
  * stopped by user
  * controller absent (Action required)
  * both failed (Action required)
- Controller client adapter boundary:
  * calls send_ipc_command only
  * zero subprocess execution
  * zero controller creation or lifecycle authority escalation
"""

import subprocess
from unittest.mock import MagicMock, patch
import pytest

from windows.product_shell import (
    DIRECTION_ACTIVE,
    DIRECTION_PROBLEM,
    DIRECTION_STOPPED,
    DIRECTION_WAITING,
    OVERALL_ACTION_REQUIRED,
    OVERALL_CONNECTED,
    OVERALL_DEGRADED,
    OVERALL_STOPPED,
    OVERALL_WAITING_FOR_MAC,
    ProductShellClient,
    map_ui_state,
)


def test_state_mapping_controller_absent():
    """When IPC connection fails (controller host absent), UI shows Action required."""
    state = map_ui_state(None)
    assert state.overall == OVERALL_ACTION_REQUIRED
    assert state.speaker == DIRECTION_STOPPED
    assert state.microphone == DIRECTION_STOPPED
    assert state.actionable_error == "Background service is not running"


def test_state_mapping_stopped_by_user():
    """Explicit STOPPED_BY_USER maps to Stopped overall and Stopped rows."""
    status = {
        "controller_state": "STOPPED",
        "desired_state": "STOPPED_BY_USER",
        "role": "windows",
        "peer_available": True,
        "speaker_path_state": "STOPPED",
        "microphone_path_state": "STOPPED",
    }
    state = map_ui_state(status)
    assert state.overall == OVERALL_STOPPED
    assert state.speaker == DIRECTION_STOPPED
    assert state.microphone == DIRECTION_STOPPED


def test_state_mapping_waiting_for_mac():
    """When peer_available is False, overall shows Waiting for Mac."""
    status = {
        "controller_state": "ACTIVE",
        "desired_state": "ENABLED",
        "role": "windows",
        "peer_available": False,
        "speaker_path_state": "IDLE",
        "microphone_path_state": "IDLE",
    }
    state = map_ui_state(status)
    assert state.overall == OVERALL_WAITING_FOR_MAC
    assert state.speaker == DIRECTION_WAITING
    assert state.microphone == DIRECTION_WAITING


def test_state_mapping_connected():
    """When both speaker and microphone are RUNNING, overall shows Connected."""
    status = {
        "controller_state": "ACTIVE",
        "desired_state": "ENABLED",
        "role": "windows",
        "peer_available": True,
        "speaker_path_state": "RUNNING",
        "microphone_path_state": "RUNNING",
    }
    state = map_ui_state(status)
    assert state.overall == OVERALL_CONNECTED
    assert state.speaker == DIRECTION_ACTIVE
    assert state.microphone == DIRECTION_ACTIVE


def test_state_mapping_degraded_pack43_unavailable():
    """Faithful mapping of real degraded Windows state: speaker RUNNING, microphone UNAVAILABLE."""
    status = {
        "controller_state": "ACTIVE",
        "desired_state": "ENABLED",
        "role": "windows",
        "peer_available": True,
        "speaker_path_state": "RUNNING",
        "microphone_path_state": "UNAVAILABLE",
        "last_actionable_microphone_error": "Standard VB-CABLE Pack43 not found or driver identity mismatch",
    }
    state = map_ui_state(status)
    assert state.overall == OVERALL_DEGRADED
    assert state.speaker == DIRECTION_ACTIVE
    assert state.microphone == DIRECTION_PROBLEM
    assert state.actionable_error == "Standard VB-CABLE Pack43 not found or driver identity mismatch"


def test_state_mapping_degraded_speaker_failed():
    """Degraded state when speaker FAILED and microphone is RUNNING."""
    status = {
        "controller_state": "ACTIVE",
        "desired_state": "ENABLED",
        "role": "windows",
        "peer_available": True,
        "speaker_path_state": "FAILED",
        "microphone_path_state": "RUNNING",
        "last_actionable_error": "Audio sink initialization failed",
    }
    state = map_ui_state(status)
    assert state.overall == OVERALL_DEGRADED
    assert state.speaker == DIRECTION_PROBLEM
    assert state.microphone == DIRECTION_ACTIVE
    assert state.actionable_error == "Audio sink initialization failed"


def test_state_mapping_both_failed_requires_action():
    """When both directions fail, overall is Action required."""
    status = {
        "controller_state": "ACTIVE",
        "desired_state": "ENABLED",
        "role": "windows",
        "peer_available": True,
        "speaker_path_state": "FAILED",
        "microphone_path_state": "UNAVAILABLE",
        "last_actionable_error": "Speaker device missing",
        "last_actionable_microphone_error": "Microphone driver missing",
    }
    state = map_ui_state(status)
    assert state.overall == OVERALL_ACTION_REQUIRED
    assert state.speaker == DIRECTION_PROBLEM
    assert state.microphone == DIRECTION_PROBLEM
    assert "Speaker device missing" in state.actionable_error
    assert "Microphone driver missing" in state.actionable_error


def test_client_status_calls_ipc_only():
    """Client get_status only calls send_ipc_command('status')."""
    client = ProductShellClient(port=50106)
    with patch("windows.product_shell.send_ipc_command") as mock_ipc, \
         patch("subprocess.Popen") as mock_popen:
        mock_ipc.return_value = {"controller_state": "ACTIVE"}
        res = client.get_status()
        assert res == {"controller_state": "ACTIVE"}
        mock_ipc.assert_called_once_with("status", port=50106, timeout=1.0)
        mock_popen.assert_not_called()


def test_client_start_calls_ipc_only_no_lifecycle_spawning():
    """Client start only calls send_ipc_command('start') and does not spawn controller."""
    client = ProductShellClient(port=50106)
    with patch("windows.product_shell.send_ipc_command") as mock_ipc, \
         patch("subprocess.Popen") as mock_popen:
        mock_ipc.return_value = {"success": True}
        res = client.start()
        assert res == {"success": True}
        mock_ipc.assert_called_once_with("start", port=50106, timeout=2.0)
        mock_popen.assert_not_called()


def test_client_stop_calls_ipc_only_no_process_killing():
    """Client stop only calls send_ipc_command('stop') and does not kill processes directly."""
    client = ProductShellClient(port=50106)
    with patch("windows.product_shell.send_ipc_command") as mock_ipc, \
         patch("subprocess.Popen") as mock_popen:
        mock_ipc.return_value = {"success": True}
        res = client.stop()
        assert res == {"success": True}
        mock_ipc.assert_called_once_with("stop", port=50106, timeout=2.0)
        mock_popen.assert_not_called()


def test_client_reconcile_calls_ipc_only():
    """Client reconcile only calls send_ipc_command('reconcile')."""
    client = ProductShellClient(port=50106)
    with patch("windows.product_shell.send_ipc_command") as mock_ipc, \
         patch("subprocess.Popen") as mock_popen:
        mock_ipc.return_value = {"reconciled": True}
        res = client.reconcile()
        assert res == {"reconciled": True}
        mock_ipc.assert_called_once_with("reconcile", port=50106, timeout=3.0)
        mock_popen.assert_not_called()


def test_network_path_classification():
    """Classifies ethernet, wifi, and unknown interfaces correctly."""
    from bridge_core.interface_classifier import InterfaceMedium
    from windows.diagnostics import classify_network_path

    mock_classifier = MagicMock()
    mock_classifier.classify_interface.return_value = InterfaceMedium.WIRED_ETHERNET
    assert classify_network_path("192.168.1.50", classifier=mock_classifier) == "Ethernet"

    mock_classifier.classify_interface.return_value = InterfaceMedium.WIFI
    assert classify_network_path("192.168.1.51", classifier=mock_classifier) == "Wi-Fi"

    mock_classifier.classify_interface.return_value = InterfaceMedium.OTHER
    assert classify_network_path("10.0.0.5", classifier=mock_classifier) == "Fallback (other)"

    assert classify_network_path(None) == "Unknown"
    assert classify_network_path("127.0.0.1") == "Unknown"


def test_build_diagnostic_report_running_sanitization():
    """build_diagnostic_report produces structured plain text without secrets/raw hardware IDs."""
    from windows.diagnostics import build_diagnostic_report

    status = {
        "controller_state": "RUNNING",
        "desired_state": "RUNNING",
        "owner_pid": 12345,
        "peer_available": True,
        "peer_address": "192.168.1.99:50105",
        "local_bind_address": "192.168.1.50",
        "speaker_path_state": "RUNNING",
        "microphone_path_state": "RUNNING",
        "mode": "PLAYBACK",
        "voice_input_active": False,
        "pack43_available": True,
        "last_actionable_error": None,
        "last_actionable_microphone_error": None,
    }

    report = build_diagnostic_report(status)
    assert "=== Cross-Desk Flow Diagnostic Report ===" in report
    assert "Controller: RUNNING (PID 12345)" in report
    assert "Peer State: Connected (192.168.1.99:50105)" in report
    assert "Auto Start:" in report
    assert "Speaker Path: RUNNING" in report
    assert "Microphone Path: RUNNING" in report
    assert "Voice Input: Automatic / Standby" in report
    assert "Pack43 Readiness: Available" in report
    assert "Last Actionable Error: None" in report

    # Verify no raw hardware GUID format {xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx} or sensitive fields
    import re
    assert not re.search(r"\{[0-9a-fA-F-]{36}\}", report)
    assert "password" not in report.lower()
    assert "token" not in report.lower()


def test_build_diagnostic_report_controller_absent():
    """build_diagnostic_report handles None status gracefully."""
    from windows.diagnostics import build_diagnostic_report

    report = build_diagnostic_report(None)
    assert "Controller: NOT RUNNING" in report
    assert "Peer State: NONE" in report
    assert "Speaker Path: STOPPED" in report
    assert "Microphone Path: STOPPED" in report
    assert "Voice Input: Standby / Off" in report
    assert "Last Actionable Error: Background service is not running" in report


def test_sanitize_diagnostic_text():
    """Deterministic verification of path, GUID, and credential redaction."""
    from windows.diagnostics import sanitize_diagnostic_text

    raw_err = (
        r"Failed loading DLL at C:\Users\alice\AppData\Local\desk-audio-bridge\lib.dll: "
        r"Device {12345678-ABCD-EF01-2345-6789ABCDEF01} refused token=secret123 and password: mypass456"
    )
    sanitized = sanitize_diagnostic_text(raw_err)
    assert r"C:\Users\alice" not in sanitized
    assert "<path redacted>" in sanitized
    assert "{12345678-ABCD-EF01-2345-6789ABCDEF01}" not in sanitized
    assert "<guid redacted>" in sanitized
    assert "secret123" not in sanitized
    assert "token=<redacted>" in sanitized
    assert "mypass456" not in sanitized
    assert "password=<redacted>" in sanitized


def test_diagnostics_ui_rows_clean_user_level():
    """UI grid display values must stay user-level (no raw PIDs or raw IPs)."""
    from windows.diagnostics import get_diagnostics_view_data

    status = {
        "controller_state": "RUNNING",
        "desired_state": "RUNNING",
        "owner_pid": 98765,
        "peer_available": True,
        "peer_address": "192.168.1.100:50105",
        "local_bind_address": "192.168.1.50",
        "speaker_path_state": "RUNNING",
        "microphone_path_state": "RUNNING",
    }
    view_data = get_diagnostics_view_data(status)
    assert view_data["service"][0] == "Running"
    assert "98765" not in view_data["service"][0]
    assert view_data["peer"][0] == "Connected"
    assert "192.168.1.100" not in view_data["peer"][0]


def test_dead_controller_recovery_invokes_lifecycle_seam():
    """When controller is absent, on_reconcile triggers lifecycle start seam, not direct subprocess."""
    import tkinter as tk
    from windows.product_shell import ProductShellApp

    root = tk.Tk()
    root.withdraw()
    try:
        mock_client = MagicMock()
        mock_client.get_status.return_value = None  # controller absent

        with patch("windows.product_shell.start_controller_via_lifecycle") as mock_start_lifecycle, \
             patch("subprocess.Popen") as mock_popen, \
             patch("subprocess.run") as mock_run:

            app = ProductShellApp(root, client=mock_client)
            assert app._last_raw_status is None

            # Trigger on_reconcile while controller is absent
            app.on_reconcile()

            # Must invoke lifecycle start seam
            mock_start_lifecycle.assert_called_once_with(timeout_sec=5.0)

            # Must not call client.reconcile() or client.start() over dead IPC
            mock_client.reconcile.assert_not_called()
            mock_client.start.assert_not_called()

            # Must never directly invoke subprocess to spawn pythonw/controller
            mock_popen.assert_not_called()
            mock_run.assert_not_called()
    finally:
        root.destroy()


def test_diagnostics_probe_caching():
    """classify_network_path and get_autostart_status use cached values without repeated expensive queries."""
    import time
    from windows.diagnostics import (
        _cached_autostart,
        _cached_network_path,
        classify_network_path,
        get_autostart_status,
    )

    mock_cls = MagicMock()
    from bridge_core.interface_classifier import InterfaceMedium
    mock_cls.classify_interface.return_value = InterfaceMedium.WIRED_ETHERNET

    # Clear test cache
    _cached_network_path.clear()

    # First call probes classifier
    res1 = classify_network_path("192.168.1.200", classifier=mock_cls)
    assert res1 == "Ethernet"
    assert mock_cls.classify_interface.call_count == 1

    # Second call within TTL returns cached value without calling classifier
    res2 = classify_network_path("192.168.1.200", classifier=mock_cls)
    assert res2 == "Ethernet"
    assert mock_cls.classify_interface.call_count == 1


def test_dead_controller_recovery_task_absent_preserves_autostart():
    """When controller is dead and Scheduled Task is absent, recovery fails safely and NEVER installs task."""
    from windows.diagnostics import start_controller_via_lifecycle

    with patch("windows.diagnostics._query_task_via_schtasks", return_value="Not installed"), \
         patch("windows.task_scheduler.install_scheduled_task") as mock_install, \
         patch("windows.task_scheduler.register_scheduled_task") as mock_register, \
         patch("subprocess.run") as mock_run:

        recovered = start_controller_via_lifecycle(timeout_sec=0.5)

        # Must return False (action required / not recovered)
        assert recovered is False

        # Must NEVER install or register Scheduled Task
        mock_install.assert_not_called()
        mock_register.assert_not_called()
        # Must not run any task
        mock_run.assert_not_called()


def test_dead_controller_recovery_task_present_triggers_schtasks():
    """When controller is dead and Scheduled Task is present, recovery runs task via schtasks and waits for IPC."""
    from windows.diagnostics import start_controller_via_lifecycle

    mock_res = MagicMock()
    mock_res.returncode = 0

    with patch("windows.diagnostics._query_task_via_schtasks", return_value="Installed"), \
         patch("subprocess.run", return_value=mock_res) as mock_run, \
         patch("windows.task_scheduler.install_scheduled_task") as mock_install, \
         patch("windows.task_scheduler.register_scheduled_task") as mock_register, \
         patch("windows.cli.send_ipc_command", return_value={"owner_pid": 1234}):

        recovered = start_controller_via_lifecycle(timeout_sec=1.0)

        assert recovered is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "/Run" in args
        assert "/TN" in args
        assert "desk-audio-bridge" in args
        mock_install.assert_not_called()
        mock_register.assert_not_called()


def test_get_deployed_sha_without_git_path(tmp_path):
    """When git CLI fails or is not in PATH, get_deployed_sha resolves SHA from repo metadata."""
    from windows.diagnostics import get_deployed_sha

    test_sha = "aabbccddeeff00112233445566778899aabbccdd"

    # Create dummy detached repo directory
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    head_file = git_dir / "HEAD"
    head_file.write_text(test_sha + "\n", encoding="utf-8")

    with patch("subprocess.run", side_effect=FileNotFoundError("git.exe not found")):
        resolved = get_deployed_sha(str(tmp_path))
        assert resolved == test_sha


def test_get_deployed_sha_worktree_indirection(tmp_path):
    """When target directory is a worktree with a .git file, get_deployed_sha follows indirection."""
    from windows.diagnostics import get_deployed_sha

    test_sha = "11223344556677889900aabbccddeeff11223344"

    # Create main repo git dir
    main_git = tmp_path / "main_repo" / ".git"
    main_git.mkdir(parents=True)
    (main_git / "refs" / "heads").mkdir(parents=True)
    (main_git / "refs" / "heads" / "feature-x").write_text(test_sha + "\n", encoding="utf-8")

    # Create worktree gitdir
    wt_gitdir = main_git / "worktrees" / "wt1"
    wt_gitdir.mkdir(parents=True)
    (wt_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (wt_gitdir / "HEAD").write_text("ref: refs/heads/feature-x\n", encoding="utf-8")

    # Create worktree directory with .git file
    wt_dir = tmp_path / "wt1"
    wt_dir.mkdir()
    (wt_dir / ".git").write_text(f"gitdir: {wt_gitdir}\n", encoding="utf-8")

    with patch("subprocess.run", side_effect=subprocess.SubprocessError("git error")):
        resolved = get_deployed_sha(str(wt_dir))
        assert resolved == test_sha


def test_autostart_query_via_schtasks_tri_state():
    """_query_task_via_schtasks correctly maps Installed, Not installed, and Unknown without pywin32."""
    from windows.diagnostics import _query_task_via_schtasks

    # Installed
    ok_res = MagicMock()
    ok_res.returncode = 0
    with patch("subprocess.run", return_value=ok_res):
        assert _query_task_via_schtasks() == "Installed"

    # Not installed
    not_found_res = MagicMock()
    not_found_res.returncode = 1
    not_found_res.stdout = ""
    not_found_res.stderr = "ERROR: The system cannot find the file specified."
    with patch("subprocess.run", return_value=not_found_res):
        assert _query_task_via_schtasks() == "Not installed"

    # Execution error / unexpected failure -> Unknown (not falsely Not installed)
    err_res = MagicMock()
    err_res.returncode = 1
    err_res.stdout = ""
    err_res.stderr = "ERROR: Access is denied."
    with patch("subprocess.run", return_value=err_res):
        assert _query_task_via_schtasks() == "Unknown"

    # Exception raised -> Unknown
    with patch("subprocess.run", side_effect=Exception("timeout")):
        assert _query_task_via_schtasks() == "Unknown"




