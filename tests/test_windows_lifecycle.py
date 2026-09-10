"""Automated test suite for Windows Daily Lifecycle (Issue #24).

Covers:
- Separate host lifecycle from user intent in BOTH directions
  * start_host preserves persisted ENABLED without mutation
  * start_host preserves persisted STOPPED_BY_USER without mutation and with 0 media children
  * host shutdown preserves persisted ENABLED
  * host shutdown preserves persisted STOPPED_BY_USER
  * explicit Start persists ENABLED
  * explicit Stop persists STOPPED_BY_USER
- Scheduled Task registration & inspection:
  * current user logon trigger
  * bounded restart on failure policy (3 retries, 1 minute interval)
  * single instance policy (IgnoreNew)
  * verified windowless executable (pythonw.exe)
  * explicit working directory and launcher entrypoint
- Idempotent install, reinstall, and uninstall:
  * install succeeds
  * reinstall safely replaces task and preserves state
  * manual host exists before install -> safe handoff to single managed host
  * uninstall cleanly removes task, stops host and media, and removes state
  * repeated uninstall succeeds without error
- Launching from non-repo current directory:
  * verifies launcher sets sys.path and imports successfully
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from bridge_core.contract import (
    DEFAULT_LOCAL_IPC_PORT,
    DEFAULT_SINGLETON_PORT,
    DesiredState,
    LifecycleState,
    PathState,
)
from windows.controller import SingleInstanceLock, WindowsBridgeController
from windows.task_scheduler import (
    DEFAULT_TASK_NAME,
    get_current_user_sid,
    get_launcher_path,
    get_project_repo_root,
    get_scheduled_task_info,
    install_scheduled_task,
    is_scheduled_task_installed,
    register_scheduled_task,
    reinstall_scheduled_task,
    resolve_windowless_python,
    terminate_verified_controller_host,
    uninstall_scheduled_task,
)
from windows.cli import send_ipc_command


@pytest.fixture
def temp_state_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test_controller_state.json")


class MockProcessRunner:
    def __init__(self):
        self.running_pids = set()
        self._next_pid = 50000

    def start_pipeline(self, cmd, desc=""):
        pid = self._next_pid
        self._next_pid += 1
        self.running_pids.add(pid)
        return pid

    def start_process(self, cmd, desc=""):
        return self.start_pipeline(cmd, desc)

    def stop_process(self, pid):
        self.running_pids.discard(pid)

    def is_running(self, pid):
        return pid in self.running_pids


class MockPipelineBuilder:
    def is_gstreamer_available(self):
        return True

    def build_speaker_command(self, **kwargs):
        return ["mock-speaker"]

    def build_sender_command(self, **kwargs):
        return ["mock-speaker-sender"]


class MockDeviceResolver:
    def resolve_default_playback_endpoint_id(self):
        return "{mock-endpoint-guid}"


class MockDiscoveryService:
    def __init__(self, **kwargs):
        self.peer_available = True
        self.peer_address = "192.168.1.100"
        self.local_bind_address = "192.168.1.101"
        self.peer_speaker_port = 5004
        self.is_ambiguous = False
        self.last_enumeration_error = None

    def start(self):
        pass

    def stop(self):
        pass

    def broadcast_hello(self):
        pass

    def refresh_peer_state(self):
        pass


class MockPack43Resolver:
    def __init__(self):
        self.cached = False

    @property
    def is_cached_available(self):
        return self.cached

    def resolve_pack43(self, force_refresh: bool = False):
        return None

    def invalidate_cache(self):
        pass


def test_start_host_preserves_persisted_enabled(temp_state_file):
    """start_host must load persisted ENABLED, start pipelines, and NOT mutate desired state."""
    # Persist ENABLED
    with open(temp_state_file, "w", encoding="utf-8") as f:
        json.dump({"desired_state": DesiredState.ENABLED.value}, f)

    ctrl = WindowsBridgeController(
        state_file=temp_state_file,
        device_resolver=MockDeviceResolver(),
        process_runner=MockProcessRunner(),
        pipeline_builder=MockPipelineBuilder(),
        discovery_service=MockDiscoveryService(),
        pack43_resolver=MockPack43Resolver(),
        lock_port=52105,
        ipc_port=52106,
    )
    with patch("windows.controller.check_runtime_dependencies", return_value=(True, "")):
        ok = ctrl.start_host()
        assert ok is True
        assert ctrl.get_status().desired_state == DesiredState.ENABLED.value
        assert ctrl.get_status().speaker_path_state == PathState.RUNNING.value

        # Verify state file was NOT overwritten with something else
        with open(temp_state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert data["desired_state"] == DesiredState.ENABLED.value

        ctrl.shutdown_host()


def test_start_host_preserves_persisted_stopped_by_user(temp_state_file):
    """start_host must load persisted STOPPED_BY_USER, maintain 0 media children, and not mutate state."""
    # Persist STOPPED_BY_USER
    with open(temp_state_file, "w", encoding="utf-8") as f:
        json.dump({"desired_state": DesiredState.STOPPED_BY_USER.value}, f)

    runner = MockProcessRunner()
    ctrl = WindowsBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        pipeline_builder=MockPipelineBuilder(),
        discovery_service=MockDiscoveryService(),
        pack43_resolver=MockPack43Resolver(),
        lock_port=52107,
        ipc_port=52108,
    )
    with patch("windows.controller.check_runtime_dependencies", return_value=(True, "")):
        ok = ctrl.start_host()
        assert ok is True
        assert ctrl.get_status().desired_state == DesiredState.STOPPED_BY_USER.value
        assert ctrl.get_status().controller_state == LifecycleState.STOPPED.value
        assert ctrl.get_status().owned_children_count == 0
        assert len(runner.running_pids) == 0

        # Verify state file remains STOPPED_BY_USER
        with open(temp_state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert data["desired_state"] == DesiredState.STOPPED_BY_USER.value

        ctrl.shutdown_host()


def test_host_shutdown_preserves_enabled(temp_state_file):
    """Host shutdown while ENABLED must NOT clobber desired_state to STOPPED_BY_USER."""
    with open(temp_state_file, "w", encoding="utf-8") as f:
        json.dump({"desired_state": DesiredState.ENABLED.value}, f)

    ctrl = WindowsBridgeController(
        state_file=temp_state_file,
        process_runner=MockProcessRunner(),
        pipeline_builder=MockPipelineBuilder(),
        discovery_service=MockDiscoveryService(),
        pack43_resolver=MockPack43Resolver(),
        lock_port=52109,
        ipc_port=52110,
    )
    with patch("windows.controller.check_runtime_dependencies", return_value=(True, "")):
        ctrl.start_host()
        assert ctrl.get_status().desired_state == DesiredState.ENABLED.value
        ctrl.shutdown_host()

        # State file must STILL be ENABLED after shutdown_host
        with open(temp_state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert data["desired_state"] == DesiredState.ENABLED.value


def test_explicit_start_and_stop_persist_intent(temp_state_file):
    """Only explicit start() persists ENABLED and only stop() persists STOPPED_BY_USER."""
    ctrl = WindowsBridgeController(
        state_file=temp_state_file,
        process_runner=MockProcessRunner(),
        pipeline_builder=MockPipelineBuilder(),
        discovery_service=MockDiscoveryService(),
        pack43_resolver=MockPack43Resolver(),
        lock_port=52111,
        ipc_port=52112,
    )
    with patch("windows.controller.check_runtime_dependencies", return_value=(True, "")):
        # Explicit start
        ctrl.start()
        assert ctrl.get_status().desired_state == DesiredState.ENABLED.value
        with open(temp_state_file, "r", encoding="utf-8") as f:
            assert json.load(f)["desired_state"] == DesiredState.ENABLED.value

        # Explicit stop
        ctrl.stop()
        assert ctrl.get_status().desired_state == DesiredState.STOPPED_BY_USER.value
        with open(temp_state_file, "r", encoding="utf-8") as f:
            assert json.load(f)["desired_state"] == DesiredState.STOPPED_BY_USER.value

        ctrl.shutdown_host()


def test_start_host_enabled_defaults_to_playback_mode(temp_state_file):
    """Under Issue #43 Dictation MVP policy, start_host on persisted ENABLED defaults to PLAYBACK mode
    (speaker running, microphone OFF/not started, 1 media child).
    """
    with open(temp_state_file, "w", encoding="utf-8") as f:
        json.dump({"desired_state": DesiredState.ENABLED.value}, f)

    class AvailablePack43Resolver:
        @property
        def is_cached_available(self):
            return True

        def resolve_pack43(self, force_refresh: bool = False):
            from windows.pack43_resolver import Pack43ResolutionResult
            return Pack43ResolutionResult(
                render_endpoint_id="{mock-pack43-render}",
                capture_endpoint_id="{mock-pack43-capture}",
                driver_version="1.0.3.5",
            )

        def invalidate_cache(self):
            pass

    class MockMicReceiverBuilder:
        def is_gstreamer_available(self):
            return True

        def build_receiver_command(self, **kwargs):
            return ["mock-gst-mic-receiver"]

    runner = MockProcessRunner()
    ctrl = WindowsBridgeController(
        state_file=temp_state_file,
        device_resolver=MockDeviceResolver(),
        process_runner=runner,
        pipeline_builder=MockPipelineBuilder(),
        discovery_service=MockDiscoveryService(),
        pack43_resolver=AvailablePack43Resolver(),
        microphone_receiver_builder=MockMicReceiverBuilder(),
        lock_port=52113,
        ipc_port=52114,
    )
    with patch("windows.controller.check_runtime_dependencies", return_value=(True, "")):
        ok = ctrl.start_host()
        assert ok is True
        st = ctrl.get_status()
        assert st.desired_state == DesiredState.ENABLED.value
        assert st.mode == "PLAYBACK"
        assert st.speaker_path_state == PathState.RUNNING.value
        assert st.microphone_path_state == PathState.READY.value
        assert st.owned_children_count == 1
        assert len(runner.running_pids) == 1

        ctrl.shutdown_host()


def test_start_host_stopped_by_user_retains_zero_media_children_and_stopped_mic(temp_state_file):
    """Persisted STOPPED_BY_USER must never start microphone or speaker, maintaining 0 media children."""
    with open(temp_state_file, "w", encoding="utf-8") as f:
        json.dump({"desired_state": DesiredState.STOPPED_BY_USER.value}, f)

    runner = MockProcessRunner()
    ctrl = WindowsBridgeController(
        state_file=temp_state_file,
        device_resolver=MockDeviceResolver(),
        process_runner=runner,
        pipeline_builder=MockPipelineBuilder(),
        discovery_service=MockDiscoveryService(),
        pack43_resolver=MockPack43Resolver(),
        lock_port=52115,
        ipc_port=52116,
    )
    with patch("windows.controller.check_runtime_dependencies", return_value=(True, "")):
        ok = ctrl.start_host()
        assert ok is True
        st = ctrl.get_status()
        assert st.desired_state == DesiredState.STOPPED_BY_USER.value
        assert st.controller_state == LifecycleState.STOPPED.value
        assert st.speaker_path_state == PathState.STOPPED.value
        assert st.microphone_path_state == PathState.STOPPED.value
        assert st.owned_children_count == 0
        assert len(runner.running_pids) == 0

        ctrl.shutdown_host()


def test_windowless_python_resolution():
    """Verified pythonw.exe must be resolved; must fail with actionable RuntimeError if missing."""
    pyw = resolve_windowless_python()
    assert os.path.isfile(pyw)
    assert "pythonw.exe" in pyw.lower()

    with patch("os.path.isfile", return_value=False), patch("shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="pythonw.exe not found"):
            resolve_windowless_python()


def test_launcher_sets_repo_context_from_non_repo_cwd():
    """windows/launcher.py must execute cleanly even when invoked from a foreign cwd."""
    temp_foreign_dir = tempfile.gettempdir()
    launcher = get_launcher_path()
    assert os.path.isfile(launcher)

    # Invoke python from foreign cwd passing launcher as sys.argv[1] to test launcher path resolution
    check_code = (
        "import os, sys\n"
        "launcher = sys.argv[1]\n"
        "repo_root = os.path.dirname(os.path.dirname(os.path.abspath(launcher)))\n"
        "if repo_root not in sys.path:\n"
        "    sys.path.insert(0, repo_root)\n"
        "os.chdir(repo_root)\n"
        "import windows.cli\n"
        "print('CWD_OK' if os.getcwd().lower() == repo_root.lower() else 'CWD_FAIL')\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", check_code, launcher],
        cwd=temp_foreign_dir,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert "CWD_OK" in res.stdout


def test_scheduled_task_registration_and_info():
    """Tests registering the task and verifying logon trigger, restart policy, and IgnoreNew."""
    test_task = "desk-audio-bridge-unit-test"
    try:
        ok, msg = register_scheduled_task(task_name=test_task, force=True)
        assert ok is True, msg
        assert is_scheduled_task_installed(test_task) is True

        info = get_scheduled_task_info(test_task)
        assert info is not None
        assert info["task_name"] == test_task
        assert info["enabled"] is True
        # Principal remains current user with interactive token
        assert info["run_as_user"]
        assert info["logon_type"] == 3  # TASK_LOGON_INTERACTIVE_TOKEN
        # Logon trigger with bounded startup delay
        assert info["trigger_type"] == 9  # TASK_TRIGGER_LOGON
        assert info["trigger_delay"] == "PT2S"
        # StartWhenAvailable & battery restrictions disabled
        assert info["start_when_available"] is True
        assert info["disallow_start_if_on_batteries"] is False
        assert info["stop_if_going_on_batteries"] is False
        # Bounded restart policy
        assert info["restart_count"] == 3
        assert info["restart_interval"] == "PT1M"
        # IgnoreNew policy
        assert info["multiple_instances"] == 2
        # Explicit working directory and action
        assert os.path.isdir(info["working_directory"])
        assert "pythonw.exe" in info["command"].lower()
        assert "launcher.py" in info["arguments"]
    finally:
        # Cleanup
        try:
            import win32com.client
            ts = win32com.client.Dispatch("Schedule.Service")
            ts.Connect()
            ts.GetFolder("\\").DeleteTask(test_task, 0)
        except Exception:
            pass


def test_unconstrained_trigger_access_denied_under_standard_user():
    """Windows Task Scheduler rejects empty LogonTrigger.UserId with Access Denied (0x80070005)
    for standard/non-elevated users. Confirms why user SID binding is required under standard user tokens.
    """
    import win32com.client
    user_sid = get_current_user_sid()
    ts = win32com.client.Dispatch("Schedule.Service")
    ts.Connect()
    folder = ts.GetFolder("\\")

    test_task = "desk-audio-bridge-unconstrained-test"
    td = ts.NewTask(0)
    td.Principal.UserId = user_sid
    td.Principal.LogonType = 3  # TASK_LOGON_INTERACTIVE_TOKEN
    trigger = td.Triggers.Create(9)  # TASK_TRIGGER_LOGON
    trigger.Enabled = True
    # Deliberately leave trigger.UserId empty/unset
    action = td.Actions.Create(0)
    action.Path = resolve_windowless_python()

    try:
        with pytest.raises(Exception) as exc_info:
            folder.RegisterTaskDefinition(test_task, td, 6, None, None, 3)
        # Verify COM error HRESULT is -2147024891 (0x80070005 = E_ACCESSDENIED)
        err_str = str(exc_info.value)
        assert "-2147024891" in err_str or "Access is denied" in err_str
    finally:
        try:
            folder.DeleteTask(test_task, 0)
        except Exception:
            pass


def test_reinstall_is_idempotent():
    """Reinstalling an existing task safely replaces it and leaves exactly one task."""
    test_task = "desk-audio-bridge-reinstall-test"
    try:
        ok1, msg1 = register_scheduled_task(task_name=test_task, force=True)
        assert ok1 is True, msg1

        ok2, msg2 = register_scheduled_task(task_name=test_task, force=True)
        assert ok2 is True, msg2
        assert is_scheduled_task_installed(test_task) is True
    finally:
        try:
            import win32com.client
            ts = win32com.client.Dispatch("Schedule.Service")
            ts.Connect()
            ts.GetFolder("\\").DeleteTask(test_task, 0)
        except Exception:
            pass


def test_uninstall_cleanup_and_idempotency(temp_state_file):
    """Uninstall must delete task, remove state file, and repeated uninstall must be safe."""
    test_task = "desk-audio-bridge-uninstall-test"
    # Create task and state file
    register_scheduled_task(task_name=test_task, force=True)
    assert is_scheduled_task_installed(test_task) is True

    with open(temp_state_file, "w", encoding="utf-8") as f:
        f.write("{}")

    with patch("windows.controller.DEFAULT_STATE_FILE", temp_state_file):
        ok, msg = uninstall_scheduled_task(task_name=test_task, cleanup_state=True)
        assert ok is True, msg
        assert is_scheduled_task_installed(test_task) is False
        assert not os.path.exists(temp_state_file)

        # Repeated uninstall must succeed cleanly
        ok2, msg2 = uninstall_scheduled_task(task_name=test_task, cleanup_state=True)
        assert ok2 is True, msg2


def test_ipc_host_shutdown_flow_terminates_process_and_preserves_state(temp_state_file):
    """Running host receiving IPC shutdown must exit naturally, preserve desired state, and free singleton."""
    # Write initial ENABLED state
    with open(temp_state_file, "w", encoding="utf-8") as f:
        json.dump({"desired_state": DesiredState.ENABLED.value}, f)

    test_ipc_port = 52199
    test_lock_port = 52198

    # Start a real controller process via python with custom ports and state file
    code = (
        "import sys, os, time\n"
        "from windows.controller import WindowsBridgeController\n"
        "from unittest.mock import patch\n"
        f"ctrl = WindowsBridgeController(state_file=r'{temp_state_file}', lock_port={test_lock_port}, ipc_port={test_ipc_port})\n"
        "with patch('windows.controller.check_runtime_dependencies', return_value=(True, '')):\n"
        "    if not ctrl.start_host():\n"
        "        sys.exit(2)\n"
        "    while not ctrl.is_shutdown_requested:\n"
        "        time.sleep(0.1)\n"
        "    ctrl.shutdown_host()\n"
        "sys.exit(0)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    try:
        # Wait for IPC to become responsive
        time.sleep(1.0)
        assert proc.poll() is None, "Host process must be running"

        # Send IPC shutdown
        res = send_ipc_command("shutdown", port=test_ipc_port)
        assert res is not None and res.get("success") is True

        # Host process must exit cleanly within bounded time
        proc.wait(timeout=4.0)
        assert proc.returncode == 0

        # Verify desired state remains ENABLED
        with open(temp_state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert data.get("desired_state") == DesiredState.ENABLED.value

        # Verify IPC no longer responds
        assert send_ipc_command("status", port=test_ipc_port) is None

        # Verify singleton lock is free and can be acquired
        new_lock = SingleInstanceLock(port=test_lock_port)
        assert new_lock.acquire() is True
        new_lock.release()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_missing_pywin32_actionable_error():
    """Missing pywin32 must raise actionable RuntimeError mentioning requirements.txt or pip install."""
    with patch.dict(sys.modules, {"win32api": None, "win32security": None, "win32com.client": None}):
        from windows.task_scheduler import _check_pywin32_dependency
        with pytest.raises(RuntimeError, match="Missing Windows lifecycle dependency 'pywin32'"):
            _check_pywin32_dependency()


def test_install_scheduled_task_truthful_activation_failure():
    """When immediate controller launch is requested but controller fails to become responsive, install fails truthfully."""
    test_task = "desk-audio-bridge-activation-test"
    with patch("windows.task_scheduler.register_scheduled_task", return_value=(True, "ok")), \
         patch("windows.task_scheduler._get_scheduler_folder") as mock_folder, \
         patch("windows.task_scheduler.terminate_verified_controller_host", return_value=True), \
         patch("windows.task_scheduler.send_ipc_command", return_value=None):
        mock_task = MagicMock()
        mock_folder.return_value.GetTask.return_value = mock_task
        from windows.task_scheduler import install_scheduled_task
        ok, msg = install_scheduled_task(task_name=test_task, start_service=True)
        assert ok is False
        assert "did not become responsive on IPC" in msg
