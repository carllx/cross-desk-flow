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

