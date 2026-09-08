"""Automated unit and integration tests for macOS LaunchAgent lifecycle and state preservation."""

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import pytest

from bridge_core.contract import DEFAULT_SPEAKER_RTP_PORT, DesiredState, LifecycleState, PathState
from macos.controller import MacBridgeController, SingleInstanceLock
from macos.lifecycle import (
    LAUNCH_AGENT_LABEL,
    bootout_service,
    generate_launch_agent_plist,
    handoff_existing_manual_controller,
    is_service_loaded,
    write_launch_agent_plist,
    install_launch_agent,
    reinstall_launch_agent,
    uninstall_launch_agent,
)


class DummyDiscoveryService:
    def __init__(self, peer_ip: str = "192.168.1.50", local_ip: str = "192.168.1.100"):
        self.peer_available = True
        self.peer_address = peer_ip
        self.local_bind_address = local_ip
        self.peer_speaker_port = DEFAULT_SPEAKER_RTP_PORT
        self.is_ambiguous = False
        self.last_enumeration_error = None
        self.broadcast_called = 0

    def start(self):
        pass

    def stop(self):
        pass

    def broadcast_hello(self):
        self.broadcast_called += 1

    def refresh_peer_state(self):
        pass


@pytest.fixture
def temp_state_path():
    with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as f:
        path = f.name
    yield path
    if os.path.exists(path):
        os.remove(path)


def test_plist_generation_structure():
    """Validates generated plist contains correct user LaunchAgent keys without hardcoded secrets."""
    plist_dict = generate_launch_agent_plist(python_exe="/dummy/python", repo_root="/dummy/repo")
    assert plist_dict["Label"] == LAUNCH_AGENT_LABEL
    assert plist_dict["ProgramArguments"] == ["/dummy/python", "-m", "macos.cli", "run"]
    assert plist_dict["WorkingDirectory"] == "/dummy/repo"
    assert plist_dict["RunAtLoad"] is True
    assert plist_dict["KeepAlive"] is True
    assert "ThrottleInterval" not in plist_dict
    assert "EnvironmentVariables" in plist_dict
    assert plist_dict["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"
    assert plist_dict["EnvironmentVariables"]["PYTHONPATH"] == "/dummy/repo"


def test_host_start_preserves_persisted_enabled(temp_state_path):
    """Verifies that start_host() preserves persisted ENABLED state and does not alter it."""
    with open(temp_state_path, "w", encoding="utf-8") as f:
        json.dump({"desired_state": DesiredState.ENABLED.value}, f)

    ctrl = MacBridgeController(state_file=temp_state_path, lock_port=50350, ipc_port=50351)
    ok = ctrl.start_host()
    assert ok is True

    st = ctrl.get_status()
    assert st.desired_state == DesiredState.ENABLED.value
    # Ensure state file still has ENABLED
    with open(temp_state_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        assert data["desired_state"] == DesiredState.ENABLED.value

    ctrl.shutdown_host()
    # After host shutdown, state file MUST still be ENABLED
    with open(temp_state_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        assert data["desired_state"] == DesiredState.ENABLED.value


def test_host_start_preserves_persisted_stopped_by_user(temp_state_path):
    """Verifies that start_host() preserves persisted STOPPED_BY_USER and creates zero media children."""
    with open(temp_state_path, "w", encoding="utf-8") as f:
        json.dump({"desired_state": DesiredState.STOPPED_BY_USER.value}, f)

    ctrl = MacBridgeController(state_file=temp_state_path, lock_port=50352, ipc_port=50353)
    ok = ctrl.start_host()
    assert ok is True

    st = ctrl.get_status()
    assert st.desired_state == DesiredState.STOPPED_BY_USER.value
    assert st.controller_state == LifecycleState.STOPPED.value
    assert st.owned_children_count == 0
    assert st.speaker_path_state == PathState.STOPPED.value
    assert st.microphone_path_state == PathState.STOPPED.value

    ctrl.shutdown_host()
    with open(temp_state_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        assert data["desired_state"] == DesiredState.STOPPED_BY_USER.value


def test_explicit_start_and_stop_intent_mutation(temp_state_path):
    """Verifies that explicit start() mutates to ENABLED and stop() mutates to STOPPED_BY_USER."""
    ctrl = MacBridgeController(state_file=temp_state_path, lock_port=50354, ipc_port=50355)
    
    # 1. Explicit start persists ENABLED
    ctrl.start()
    assert ctrl.get_status().desired_state == DesiredState.ENABLED.value
    with open(temp_state_path, "r", encoding="utf-8") as f:
        assert json.load(f)["desired_state"] == DesiredState.ENABLED.value

    # 2. Host shutdown leaves ENABLED persisted
    ctrl.shutdown_host()
    with open(temp_state_path, "r", encoding="utf-8") as f:
        assert json.load(f)["desired_state"] == DesiredState.ENABLED.value

    # 3. Explicit stop persists STOPPED_BY_USER
    ctrl2 = MacBridgeController(state_file=temp_state_path, lock_port=50354, ipc_port=50355)
    ctrl2.stop()
    assert ctrl2.get_status().desired_state == DesiredState.STOPPED_BY_USER.value
    with open(temp_state_path, "r", encoding="utf-8") as f:
        assert json.load(f)["desired_state"] == DesiredState.STOPPED_BY_USER.value


def test_microphone_permission_probe_actionable_error(temp_state_path):
    """Verifies that if permission probe reports Denied (2) or Restricted (1), microphone path fails with actionable error."""
    # Probe returning 2 (Denied)
    ctrl = MacBridgeController(
        state_file=temp_state_path,
        lock_port=50356,
        ipc_port=50357,
        discovery_service=DummyDiscoveryService(),
        mic_permission_probe=lambda: 2,
    )
    ctrl.start()
    ok = ctrl.set_microphone_enabled(True)
    assert ok is False

    st = ctrl.get_status()
    assert st.microphone_path_state == PathState.FAILED.value
    assert "macOS Microphone permission denied" in (st.last_actionable_microphone_error or "")
    assert "System Settings -> Privacy & Security -> Microphone" in (st.last_actionable_microphone_error or "")
    ctrl.shutdown_host()


def test_idempotent_lifecycle_mock_registration(tmp_path):
    """Tests install, reinstall, and repeated uninstall using a test plist path."""
    test_plist = str(tmp_path / "test.controller.plist")
    test_state = str(tmp_path / "test_state.json")

    with open(test_state, "w", encoding="utf-8") as f:
        json.dump({"desired_state": DesiredState.STOPPED_BY_USER.value}, f)

    # 1. Write plist directly
    written_path = write_launch_agent_plist(plist_path=test_plist)
    assert os.path.exists(written_path)
    with open(written_path, "rb") as f:
        data = plistlib.load(f)
        assert data["Label"] == LAUNCH_AGENT_LABEL

    # 2. Reinstall (overwrite) plist
    reinstall_path = write_launch_agent_plist(plist_path=test_plist)
    assert reinstall_path == written_path
    assert os.path.exists(written_path)

    # State file must remain untouched
    with open(test_state, "r", encoding="utf-8") as f:
        assert json.load(f)["desired_state"] == DesiredState.STOPPED_BY_USER.value

    # 3. Uninstall (removes plist and state)
    ok, err = uninstall_launch_agent(plist_path=test_plist, remove_state=True, state_file=test_state)
    assert ok is True
    assert err is None
    assert not os.path.exists(test_plist)
    assert not os.path.exists(test_state)

    # 4. Repeated uninstall succeeds safely
    ok_repeated, err_repeated = uninstall_launch_agent(plist_path=test_plist, remove_state=True, state_file=test_state)
    assert ok_repeated is True
    assert err_repeated is None


def test_manual_controller_handoff_enabled(temp_state_path):
    """Verifies that handoff_existing_manual_controller terminates a running manual controller while preserving ENABLED."""
    with open(temp_state_path, "w", encoding="utf-8") as f:
        json.dump({"desired_state": DesiredState.ENABLED.value}, f)

    # Launch manual controller in subprocess with isolated state file
    env = dict(os.environ)
    env["DESK_AUDIO_BRIDGE_STATE_FILE"] = temp_state_path
    proc = subprocess.Popen(
        [sys.executable, "-m", "macos.cli", "run"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    assert proc.poll() is None

    # Handoff manual controller
    ok, err = handoff_existing_manual_controller()
    assert ok is True, f"Handoff failed: {err}"
    assert proc.poll() is not None, "Manual controller process must be terminated"

    # Verify state file still has ENABLED
    with open(temp_state_path, "r", encoding="utf-8") as f:
        assert json.load(f)["desired_state"] == DesiredState.ENABLED.value


def test_manual_controller_handoff_stopped_by_user(temp_state_path):
    """Verifies that handoff_existing_manual_controller terminates a running manual controller while preserving STOPPED_BY_USER."""
    with open(temp_state_path, "w", encoding="utf-8") as f:
        json.dump({"desired_state": DesiredState.STOPPED_BY_USER.value}, f)

    env = dict(os.environ)
    env["DESK_AUDIO_BRIDGE_STATE_FILE"] = temp_state_path
    proc = subprocess.Popen(
        [sys.executable, "-m", "macos.cli", "run"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    assert proc.poll() is None

    ok, err = handoff_existing_manual_controller()
    assert ok is True, f"Handoff failed: {err}"
    assert proc.poll() is not None, "Manual controller process must be terminated"

    with open(temp_state_path, "r", encoding="utf-8") as f:
        assert json.load(f)["desired_state"] == DesiredState.STOPPED_BY_USER.value


def test_bootout_failure_during_install_reports_error():
    """Verifies that if an existing LaunchAgent fails to bootout, install reports failure."""
    with patch("macos.lifecycle.is_service_loaded", return_value=True), \
         patch("macos.lifecycle.bootout_service", return_value=False):
        ok, err = install_launch_agent()
        assert ok is False
        assert "Failed to bootout existing LaunchAgent registration" in (err or "")


def test_bootout_failure_during_uninstall_reports_error():
    """Verifies that if bootout fails during uninstall, uninstall reports failure."""
    with patch("macos.lifecycle.is_service_loaded", return_value=True), \
         patch("macos.lifecycle.bootout_service", return_value=False):
        ok, err = uninstall_launch_agent()
        assert ok is False
        assert "Failed to bootout LaunchAgent service from launchd" in (err or "")


def test_plist_deletion_failure_during_uninstall_reports_error(tmp_path):
    """Verifies that if removing plist fails, uninstall reports failure."""
    test_plist = str(tmp_path / "test.plist")
    with open(test_plist, "w") as f:
        f.write("test")

    with patch("macos.lifecycle.is_service_loaded", return_value=False), \
         patch("os.remove", side_effect=OSError("Permission denied")):
        ok, err = uninstall_launch_agent(plist_path=test_plist, remove_state=False)
        assert ok is False
        assert "Failed to remove LaunchAgent plist" in (err or "")
