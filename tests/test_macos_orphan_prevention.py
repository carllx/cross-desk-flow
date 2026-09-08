"""Regression test suite for macOS GStreamer child process orphan prevention and recovery.

Verifies:
1. Graceful SIGTERM leaves zero owned children.
2. Normal Stop leaves zero owned children.
3. SIGKILL simulation leaves child + journal, next controller safely recovers it.
4. PID reuse / create-time mismatch -> process NOT touched.
5. Command-signature mismatch -> process NOT touched.
6. Unrelated GStreamer process -> process NOT touched.
7. Desired-state persistence in controller_state.json remains unchanged (no ownership pollution).
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import pytest
import psutil

from bridge_core.contract import DesiredState, PathState, DEFAULT_SPEAKER_RTP_PORT
from macos.controller import MacBridgeController, DEFAULT_STATE_FILE
from macos.process_runner import MacOwnedProcessRunner

GST_BIN = "/Library/Frameworks/GStreamer.framework/Versions/1.0/bin/gst-launch-1.0"


def get_free_port() -> int:
    """Returns an unbound local TCP/UDP port."""
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def isolated_env(tmp_path):
    """Provides isolated state and journal file paths along with free ports."""
    state_file = str(tmp_path / "controller_state.json")
    journal_file = str(tmp_path / "ownership_journal.json")
    lock_port = get_free_port()
    ipc_port = get_free_port()
    return {
        "state_file": state_file,
        "journal_file": journal_file,
        "lock_port": lock_port,
        "ipc_port": ipc_port,
    }


def test_graceful_sigterm_leaves_zero_owned_children(isolated_env):
    """Case A: Controller running in background receives SIGTERM; all owned children exit cleanly."""
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    host_code = f"""
import os, sys, time, signal
from macos.controller import MacBridgeController

ctrl = MacBridgeController(
    state_file={repr(state_file)},
    journal_file={repr(journal_file)},
    lock_port={lock_port},
    ipc_port={ipc_port},
)
if not ctrl.start_host():
    sys.exit(2)

gst_bin = {repr(GST_BIN)}
c1 = ctrl.process_runner.start_process([gst_bin, "fakesrc", "is-live=true", "!", "fakesink"])
c2 = ctrl.process_runner.start_process([gst_bin, "fakesrc", "is-live=true", "!", "fakesink"])
ctrl._speaker_child_pid = c1
ctrl._record_child_started("speaker", c1, [gst_bin, "-m", "udpsrc", "port=5004"], 5004)
ctrl._microphone_child_pid = c2
ctrl._record_child_started("microphone", c2, [gst_bin, "-m", "osxaudiosrc", "port=5006"], 5006)

print(f"READY:{{c1}},{{c2}}", flush=True)

def _sig_handler(signum, frame):
    sys.exit(0)

signal.signal(signal.SIGTERM, _sig_handler)
signal.signal(signal.SIGHUP, _sig_handler)

try:
    while True:
        time.sleep(0.5)
        ctrl.reconcile()
except (KeyboardInterrupt, SystemExit):
    pass
finally:
    ctrl.shutdown()
    print("SHUTDOWN_CLEAN", flush=True)
"""

    proc = subprocess.Popen(
        [sys.executable, "-c", host_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        ready_line = proc.stdout.readline()
        assert ready_line.startswith("READY:"), f"Host failed: {ready_line}"
        c1, c2 = [int(x) for x in ready_line.strip().split(":")[1].split(",")]

        assert psutil.pid_exists(c1)
        assert psutil.pid_exists(c2)

        # Send SIGTERM to host
        os.kill(proc.pid, signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=6.0)
        assert proc.returncode == 0

        # Children must be terminated with zero orphans
        time.sleep(0.5)
        assert not psutil.pid_exists(c1), f"Child 1 (PID {c1}) was orphaned!"
        assert not psutil.pid_exists(c2), f"Child 2 (PID {c2}) was orphaned!"

        # Ownership journal must be cleared cleanly
        assert not os.path.exists(journal_file)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_normal_stop_leaves_zero_owned_children(isolated_env):
    """Normal stop() idempotently terminates children and removes journal entries."""
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    assert ctrl.start_host()

    c1 = ctrl.process_runner.start_process([GST_BIN, "fakesrc", "is-live=true", "!", "fakesink"])
    ctrl._speaker_child_pid = c1
    ctrl._record_child_started("speaker", c1, [GST_BIN, "-m", "udpsrc", "port=5004"], 5004)

    assert psutil.pid_exists(c1)
    assert os.path.exists(journal_file)

    # Invoke stop
    ctrl.stop()

    time.sleep(0.5)
    assert not psutil.pid_exists(c1), f"Child PID {c1} should be terminated on stop()"
    assert not os.path.exists(journal_file)

    ctrl.shutdown()


def test_sigkill_simulation_and_next_controller_recovery(isolated_env):
    """Case B: Controller killed by SIGKILL leaves child; subsequent controller safely recovers it."""
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    host_code = f"""
import os, sys, time
from macos.controller import MacBridgeController

ctrl = MacBridgeController(
    state_file={repr(state_file)},
    journal_file={repr(journal_file)},
    lock_port={lock_port},
    ipc_port={ipc_port},
)
assert ctrl.start_host()

gst_bin = {repr(GST_BIN)}
c1 = ctrl.process_runner.start_process([
    gst_bin, "-m", "udpsrc", "port=5004", "!", "fakesink"
])
ctrl._speaker_child_pid = c1
ctrl._record_child_started("speaker", c1, [gst_bin, "-m", "udpsrc", "port=5004"], 5004)

print(f"CHILD:{{c1}}", flush=True)
while True:
    time.sleep(1.0)
"""

    proc = subprocess.Popen(
        [sys.executable, "-c", host_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    orphan_pid = None
    try:
        line = proc.stdout.readline()
        assert line.startswith("CHILD:"), f"Host start failed: {line}"
        orphan_pid = int(line.strip().split(":")[1])
        assert psutil.pid_exists(orphan_pid)

        # Uncatchable SIGKILL to controller host
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=3.0)

        # OS semantics: controller is dead, but child remains alive as orphan
        time.sleep(0.5)
        assert psutil.pid_exists(orphan_pid), "Orphan process should temporarily exist after SIGKILL"

        # Journal must contain recorded child
        assert os.path.exists(journal_file)

        # Now start next controller instance using the same state and journal
        ctrl2 = MacBridgeController(
            state_file=state_file,
            journal_file=journal_file,
            lock_port=lock_port,
            ipc_port=ipc_port,
        )
        # start_host should detect prior owner dead, verify ownership, and recover orphan
        assert ctrl2.start_host()

        time.sleep(0.5)
        assert not psutil.pid_exists(orphan_pid), f"Orphan PID {orphan_pid} must be recovered and terminated"
        assert not os.path.exists(journal_file)

        ctrl2.shutdown()
    finally:
        if orphan_pid and psutil.pid_exists(orphan_pid):
            try:
                os.kill(orphan_pid, signal.SIGKILL)
            except Exception:
                pass


def test_pid_reuse_create_time_mismatch_not_touched(isolated_env):
    """Safety rule: if recorded create_time does not match running process, do NOT touch it (PID reuse)."""
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    # Start a safe long-running GStreamer child
    safe_proc = subprocess.Popen(
        [GST_BIN, "-m", "udpsrc", "port=5004", "!", "fakesink"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    safe_pid = safe_proc.pid
    time.sleep(0.5)
    assert psutil.pid_exists(safe_pid)

    try:
        # Construct journal pointing to safe_pid with an intentionally false create_time (1000s in the past)
        p = psutil.Process(safe_pid)
        actual_create_time = p.create_time()

        stale_journal = {
            "version": 1,
            "owner_pid": 9999999,  # Definitely non-existent owner
            "owner_create_time": 1000.0,
            "children": {
                "speaker": {
                    "role": "speaker",
                    "pid": safe_pid,
                    "create_time": actual_create_time - 500.0,  # Deliberate mismatch
                    "port": 5004,
                    "cmd_tokens": [GST_BIN, "-m", "udpsrc", "port=5004"],
                }
            },
        }
        with open(journal_file, "w", encoding="utf-8") as f:
            json.dump(stale_journal, f)

        # Start controller; recovery should fail-closed and NOT kill safe_proc
        ctrl = MacBridgeController(
            state_file=state_file,
            journal_file=journal_file,
            lock_port=lock_port,
            ipc_port=ipc_port,
        )
        assert ctrl.start_host()

        time.sleep(0.5)
        assert psutil.pid_exists(safe_pid), "Process with create_time mismatch must NOT be touched!"
        assert safe_proc.poll() is None

        ctrl.shutdown()
    finally:
        safe_proc.kill()
        safe_proc.wait(timeout=2.0)


def test_command_signature_mismatch_not_touched(isolated_env):
    """Safety rule: if command signature does not match expected GStreamer pipeline, do NOT touch it."""
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    # Start a safe Python process (not GStreamer)
    py_proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    py_pid = py_proc.pid
    time.sleep(0.5)
    assert psutil.pid_exists(py_pid)

    try:
        p = psutil.Process(py_pid)
        stale_journal = {
            "version": 1,
            "owner_pid": 9999999,
            "owner_create_time": 1000.0,
            "children": {
                "speaker": {
                    "role": "speaker",
                    "pid": py_pid,
                    "create_time": p.create_time(),
                    "port": 5004,
                    "cmd_tokens": [GST_BIN, "-m", "udpsrc", "port=5004"],
                }
            },
        }
        with open(journal_file, "w", encoding="utf-8") as f:
            json.dump(stale_journal, f)

        ctrl = MacBridgeController(
            state_file=state_file,
            journal_file=journal_file,
            lock_port=lock_port,
            ipc_port=ipc_port,
        )
        # Fail-closed: controller must refuse to start because an unverified process occupies the journal entry
        assert not ctrl.start_host()
        assert ctrl.get_status().controller_state == "ERROR"
        assert "failed identity verification" in (ctrl.get_status().last_actionable_error or "")

        time.sleep(0.5)
        assert psutil.pid_exists(py_pid), "Non-GStreamer process must NOT be touched by recovery!"
        assert py_proc.poll() is None

        # Journal must retain the unresolved child entry
        with open(journal_file, "r", encoding="utf-8") as f:
            j_data = json.load(f)
        assert "speaker" in j_data.get("children", {})
        assert j_data["children"]["speaker"]["pid"] == py_pid

        ctrl.shutdown()
    finally:
        py_proc.kill()
        py_proc.wait(timeout=2.0)


def test_unrelated_gstreamer_process_not_touched(isolated_env):
    """External GStreamer process not recorded in journal remains completely untouched."""
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    unrelated_proc = subprocess.Popen(
        [GST_BIN, "fakesrc", "is-live=true", "!", "fakesink"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    unrelated_pid = unrelated_proc.pid
    time.sleep(0.5)
    assert psutil.pid_exists(unrelated_pid)

    try:
        ctrl = MacBridgeController(
            state_file=state_file,
            journal_file=journal_file,
            lock_port=lock_port,
            ipc_port=ipc_port,
        )
        assert ctrl.start_host()
        ctrl.stop()
        ctrl.shutdown()

        time.sleep(0.5)
        assert psutil.pid_exists(unrelated_pid), "Unrelated GStreamer process was wrongly terminated!"
        assert unrelated_proc.poll() is None
    finally:
        unrelated_proc.kill()
        unrelated_proc.wait(timeout=2.0)


def test_desired_state_persistence_remains_unchanged(isolated_env):
    """Verifies controller_state.json contains ONLY desired_state and NO ownership schema pollution."""
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    ctrl.start()

    with open(state_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Strictly verify keys in state_file
    assert set(data.keys()) == {"desired_state"}
    assert data["desired_state"] == DesiredState.ENABLED.value

    ctrl.stop()
    with open(state_file, "r", encoding="utf-8") as f:
        data2 = json.load(f)

    assert set(data2.keys()) == {"desired_state"}
    assert data2["desired_state"] == DesiredState.STOPPED_BY_USER.value

    ctrl.shutdown()


def test_verified_child_termination_failure_retains_journal_and_fails_closed(isolated_env, monkeypatch):
    """Browser Review Requirement 1: If termination of a verified child fails/times out,
    the journal record MUST be retained, controller startup MUST fail-closed, and an actionable error is exposed.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    proc = subprocess.Popen(
        [GST_BIN, "-m", "udpsrc", "port=5004", "!", "fakesink"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = proc.pid
    time.sleep(0.5)
    assert psutil.pid_exists(pid)

    try:
        p = psutil.Process(pid)
        stale_journal = {
            "version": 1,
            "owner_pid": 9999999,
            "owner_create_time": 1000.0,
            "children": {
                "speaker": {
                    "role": "speaker",
                    "pid": pid,
                    "create_time": p.create_time(),
                    "port": 5004,
                    "cmd_tokens": [GST_BIN, "-m", "udpsrc", "port=5004"],
                }
            },
        }
        with open(journal_file, "w", encoding="utf-8") as f:
            json.dump(stale_journal, f)

        ctrl = MacBridgeController(
            state_file=state_file,
            journal_file=journal_file,
            lock_port=lock_port,
            ipc_port=ipc_port,
        )

        # Simulate termination failure: _terminate_stale_child returns False
        monkeypatch.setattr(ctrl, "_terminate_stale_child", lambda c_pid, c_info: False)

        # Controller host start must fail closed
        assert not ctrl.start_host()
        assert ctrl.get_status().controller_state == "ERROR"
        assert f"Failed to terminate orphaned speaker child [PID {pid}]" in (ctrl.get_status().last_actionable_error or "")

        # Journal file must NOT be cleared; unresolved child must remain recorded
        with open(journal_file, "r", encoding="utf-8") as f:
            j_data = json.load(f)
        assert "speaker" in j_data.get("children", {})
        assert j_data["children"]["speaker"]["pid"] == pid

        ctrl.shutdown()
    finally:
        proc.kill()
        proc.wait(timeout=2.0)


def test_unverified_live_pid_retained_and_fails_closed(isolated_env):
    """Browser Review Requirement 2: If an existing process cannot be verified as our child,
    it must NOT be touched, its record MUST be retained in the journal, and startup MUST fail-closed.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    py_proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    py_pid = py_proc.pid
    time.sleep(0.5)
    assert psutil.pid_exists(py_pid)

    try:
        p = psutil.Process(py_pid)
        stale_journal = {
            "version": 1,
            "owner_pid": 9999999,
            "owner_create_time": 1000.0,
            "children": {
                "microphone": {
                    "role": "microphone",
                    "pid": py_pid,
                    "create_time": p.create_time(),
                    "port": 5006,
                    "cmd_tokens": ["nonexistent_pipeline_signature"],
                }
            },
        }
        with open(journal_file, "w", encoding="utf-8") as f:
            json.dump(stale_journal, f)

        ctrl = MacBridgeController(
            state_file=state_file,
            journal_file=journal_file,
            lock_port=lock_port,
            ipc_port=ipc_port,
        )

        assert not ctrl.start_host()
        assert ctrl.get_status().controller_state == "ERROR"
        assert "failed identity verification" in (ctrl.get_status().last_actionable_error or "")

        # Target process must NOT be killed
        assert psutil.pid_exists(py_pid)
        assert py_proc.poll() is None

        # Journal must retain unresolved entry
        with open(journal_file, "r", encoding="utf-8") as f:
            j_data = json.load(f)
        assert "microphone" in j_data.get("children", {})
        assert j_data["children"]["microphone"]["pid"] == py_pid

        ctrl.shutdown()
    finally:
        py_proc.kill()
        py_proc.wait(timeout=2.0)


def test_journal_write_failure_terminates_spawned_child_and_fails_path(isolated_env, monkeypatch):
    """Browser Review Requirement 3: If journal write fails when starting a child process,
    the newly spawned process must immediately be killed, path marked FAILED, and error exposed.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    assert ctrl.start_host()

    class FakeDiscovery:
        peer_available = True
        peer_address = "127.0.0.1"
        local_bind_address = "127.0.0.1"
        peer_speaker_port = 5004
        is_ambiguous = False
        last_enumeration_error = None
        def start(self): pass
        def stop(self): pass
        def broadcast_hello(self): pass

    ctrl.discovery_service = FakeDiscovery()

    # Simulate atomic journal write failure
    monkeypatch.setattr(ctrl, "_write_ownership_journal", lambda data: False)

    ctrl.start()

    # Speaker path must fail, not RUNNING
    status = ctrl.get_status()
    assert status.speaker_path_state == "FAILED"
    assert "ownership journal" in (status.last_actionable_error or "").lower()

    # Ensure no lingering child process
    assert ctrl._speaker_child_pid is None
    assert status.owned_children_count == 0

    ctrl.shutdown()


def test_corrupt_existing_journal_fails_closed_and_retains_file(isolated_env):
    """Browser Review Requirement 4: Corrupt/unparseable journal must fail-closed,
    refuse to start the controller host, not delete the corrupt file, and expose an actionable error.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    corrupt_content = '{"version": 1, "children": {"speaker": {INVALID_JSON'
    with open(journal_file, "w", encoding="utf-8") as f:
        f.write(corrupt_content)

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )

    # Controller must refuse to start
    assert not ctrl.start_host()
    assert ctrl.get_status().controller_state == "ERROR"
    assert "corrupt" in (ctrl.get_status().last_actionable_error or "").lower()

    # Corrupt file must be preserved for investigation
    assert os.path.exists(journal_file)
    with open(journal_file, "r", encoding="utf-8") as f:
        assert f.read() == corrupt_content

    ctrl.shutdown()


def test_successful_verified_recovery_clears_journal(isolated_env):
    """Browser Review Requirement 5: When all stale owned processes are verified and cleanly terminated,
    the journal file MUST be cleanly unlinked/emptied, allowing healthy start.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    proc = subprocess.Popen(
        [GST_BIN, "-m", "udpsrc", "port=5004", "!", "fakesink"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = proc.pid
    time.sleep(0.5)
    assert psutil.pid_exists(pid)

    p = psutil.Process(pid)
    stale_journal = {
        "version": 1,
        "owner_pid": 9999999,
        "owner_create_time": 1000.0,
        "children": {
            "speaker": {
                "role": "speaker",
                "pid": pid,
                "create_time": p.create_time(),
                "port": 5004,
                "cmd_tokens": [GST_BIN, "-m", "udpsrc", "port=5004"],
            }
        },
    }
    with open(journal_file, "w", encoding="utf-8") as f:
        json.dump(stale_journal, f)

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )

    # Recovery must succeed cleanly
    assert ctrl.start_host()
    assert ctrl.get_status().controller_state in ("ACTIVE", "DISCOVERING", "STARTING", "STOPPED", "IDLE")

    # The stale process must be dead
    time.sleep(0.5)
    assert not psutil.pid_exists(pid)

    # The journal must be cleared/unlinked
    assert not os.path.exists(journal_file)

    ctrl.shutdown()


def test_terminate_stale_child_inspection_exception_returns_false(isolated_env, monkeypatch):
    """Browser Review Requirement 1: _terminate_stale_child() inspection exception -> False, not True.
    Uncertainty must NEVER be interpreted as confirmed death.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    proc = subprocess.Popen(
        [GST_BIN, "-m", "udpsrc", "port=5004", "!", "fakesink"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = proc.pid
    time.sleep(0.5)
    assert psutil.pid_exists(pid)

    try:
        ctrl = MacBridgeController(
            state_file=state_file,
            journal_file=journal_file,
            lock_port=lock_port,
            ipc_port=ipc_port,
        )

        child_info = {
            "role": "speaker",
            "pid": pid,
            "create_time": psutil.Process(pid).create_time(),
            "port": 5004,
            "cmd_tokens": [GST_BIN, "-m", "udpsrc", "port=5004"],
        }

        # Simulate permission / inspection failure in psutil during final death check
        orig_process = psutil.Process
        def faulty_process(p_id):
            if p_id == pid:
                raise psutil.AccessDenied(pid=p_id, msg="Simulated permission error during inspection")
            return orig_process(p_id)

        monkeypatch.setattr(psutil, "Process", faulty_process)

        # Must return False, not True!
        result = ctrl._terminate_stale_child(pid, child_info)
        assert result is False, "Uncertainty / inspection exception must return False, never True"
    finally:
        proc.kill()
        proc.wait(timeout=2.0)


def test_normal_stop_process_failure_retains_journal(isolated_env, monkeypatch):
    """Browser Review Requirement 2: Normal stop_process() failure -> journal retained.
    If stop_process returns False, PID and journal record must be preserved.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    assert ctrl.start_host()

    c1 = ctrl.process_runner.start_process([GST_BIN, "-m", "udpsrc", "port=5004", "!", "fakesink"])
    ctrl._speaker_child_pid = c1
    assert ctrl._record_child_started("speaker", c1, [GST_BIN, "-m", "udpsrc", "port=5004"], 5004)

    assert psutil.pid_exists(c1)
    assert os.path.exists(journal_file)

    try:
        # Simulate stop_process failure (e.g. process refused to terminate or psutil error)
        monkeypatch.setattr(ctrl.process_runner, "stop_process", lambda p: False)

        # Attempt to stop speaker child via controller
        ok = ctrl._stop_child("speaker")
        assert ok is False

        # PID and journal must be retained
        assert ctrl._speaker_child_pid == c1
        with open(journal_file, "r", encoding="utf-8") as f:
            j_data = json.load(f)
        assert "speaker" in j_data.get("children", {})
        assert j_data["children"]["speaker"]["pid"] == c1
    finally:
        ctrl.process_runner.stop_process(c1)
        ctrl.shutdown()


def test_stop_with_one_child_refusing_termination_returns_false_and_retains_evidence(isolated_env, monkeypatch):
    """Browser Review Requirement 3: stop() with one child refusing termination -> returns False / evidence retained."""
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    assert ctrl.start_host()

    c1 = ctrl.process_runner.start_process([GST_BIN, "-m", "udpsrc", "port=5004", "!", "fakesink"])
    ctrl._speaker_child_pid = c1
    assert ctrl._record_child_started("speaker", c1, [GST_BIN, "-m", "udpsrc", "port=5004"], 5004)

    try:
        # Simulate stop_process returning False
        monkeypatch.setattr(ctrl.process_runner, "stop_process", lambda p: False)

        # Explicit stop() must return False
        ret = ctrl.stop()
        assert ret is False
        assert ctrl.get_status().controller_state == "ERROR"

        # Ownership evidence must be retained
        assert os.path.exists(journal_file)
        with open(journal_file, "r", encoding="utf-8") as f:
            j_data = json.load(f)
        assert "speaker" in j_data.get("children", {})
    finally:
        ctrl.process_runner.stop_process(c1)
        ctrl.shutdown()


def test_shutdown_host_does_not_clear_unresolved_child_journal(isolated_env, monkeypatch):
    """Browser Review Requirement 4: shutdown_host() does not clear unresolved child journal."""
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    assert ctrl.start_host()

    c1 = ctrl.process_runner.start_process([GST_BIN, "-m", "udpsrc", "port=5004", "!", "fakesink"])
    ctrl._speaker_child_pid = c1
    assert ctrl._record_child_started("speaker", c1, [GST_BIN, "-m", "udpsrc", "port=5004"], 5004)

    try:
        # Simulate stop_process failure on speaker
        monkeypatch.setattr(ctrl.process_runner, "stop_process", lambda p: False)

        ctrl.shutdown_host()

        # Journal file MUST exist and retain speaker child record
        assert os.path.exists(journal_file)
        with open(journal_file, "r", encoding="utf-8") as f:
            j_data = json.load(f)
        assert "speaker" in j_data.get("children", {})
        assert j_data["children"]["speaker"]["pid"] == c1
    finally:
        ctrl.process_runner.stop_process(c1)
        ctrl.shutdown()


def test_failed_create_time_acquisition_terminates_child_and_fails_path(isolated_env, monkeypatch):
    """Browser Review Requirement 5: Failed create_time acquisition after spawn -> child terminated and path FAILED."""
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    assert ctrl.start_host()

    class FakeDiscovery:
        peer_available = True
        peer_address = "127.0.0.1"
        local_bind_address = "127.0.0.1"
        peer_speaker_port = 5004
        is_ambiguous = False
        last_enumeration_error = None
        def start(self): pass
        def stop(self): pass
        def broadcast_hello(self): pass

    ctrl.discovery_service = FakeDiscovery()

    # Simulate inability to get create_time from psutil
    orig_process = psutil.Process
    def faulty_process(p_id):
        proc_obj = orig_process(p_id)
        if p_id != os.getpid():
            raise psutil.Error("Simulated inability to read create_time")
        return proc_obj

    monkeypatch.setattr(psutil, "Process", faulty_process)
    # Also ensure process runner metadata does not supply create_time
    monkeypatch.setattr(ctrl.process_runner, "get_child_metadata", lambda p: None)

    ctrl.start()

    status = ctrl.get_status()
    assert status.speaker_path_state == "FAILED"
    assert "journal" in (status.last_actionable_error or "").lower()

    # The child must NOT be running
    assert ctrl._speaker_child_pid is None
    assert status.owned_children_count == 0

    ctrl.shutdown()


def test_journal_clear_failure_not_reported_as_successful_recovery(isolated_env, monkeypatch):
    """Browser Review Requirement 6: Journal clear failure is not reported as successful clean recovery."""
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    proc = subprocess.Popen(
        [GST_BIN, "-m", "udpsrc", "port=5004", "!", "fakesink"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = proc.pid
    time.sleep(0.5)
    assert psutil.pid_exists(pid)

    p = psutil.Process(pid)
    stale_journal = {
        "version": 1,
        "owner_pid": 9999999,
        "owner_create_time": 1000.0,
        "children": {
            "speaker": {
                "role": "speaker",
                "pid": pid,
                "create_time": p.create_time(),
                "port": 5004,
                "cmd_tokens": [GST_BIN, "-m", "udpsrc", "port=5004"],
            }
        },
    }
    with open(journal_file, "w", encoding="utf-8") as f:
        json.dump(stale_journal, f)

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )

    # Simulate _clear_ownership_journal failing (e.g. permission error on unlinking)
    monkeypatch.setattr(ctrl, "_clear_ownership_journal", lambda: False)

    # Recovery must fail-closed!
    assert not ctrl.start_host()
    status = ctrl.get_status()
    assert status.controller_state == "ERROR"
    assert "failed to remove ownership journal file" in (status.last_actionable_error or "").lower()

    ctrl.shutdown()


def test_record_child_stopped_failure_causes_stop_child_to_return_false(isolated_env, monkeypatch):
    """Browser Review Correction 1: _stop_child must honor journal-update result.
    If journal update fails after process death is confirmed, _stop_child must return False,
    surface actionable error, and mark path as FAILED.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    assert ctrl.start_host()

    proc = subprocess.Popen(
        ["sleep", "10"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ctrl._speaker_child_pid = proc.pid

    # Simulate _record_child_stopped failing
    monkeypatch.setattr(ctrl, "_record_child_stopped", lambda role: False)

    # Process will be stopped, but journal update fails
    success = ctrl._stop_child("speaker")
    assert success is False
    assert ctrl._speaker_path_state.value == "FAILED"
    assert "ownership journal" in (ctrl._last_actionable_error or "").lower()

    ctrl.shutdown()


def test_stopped_by_user_reconcile_speaker_stop_failure_sets_error_and_preserves_failed_state(isolated_env, monkeypatch):
    """Browser Review Correction 2: Under STOPPED_BY_USER reconcile, if speaker stop fails,
    controller_state must become ERROR and speaker_path_state must be FAILED, not STOPPED.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    class FakeDiscovery:
        peer_available = False
        peer_address = None
        local_bind_address = None
        is_ambiguous = False
        last_enumeration_error = None
        def start(self): pass
        def stop(self): pass
        def broadcast_hello(self): pass

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    ctrl.discovery_service = FakeDiscovery()
    assert ctrl.start_host()

    # Simulate speaker child stop failure
    def fake_stop_child(role):
        if role == "speaker":
            ctrl._speaker_path_state = PathState.FAILED
            ctrl._last_actionable_error = "Fake speaker stop failure"
            return False
        return True

    monkeypatch.setattr(ctrl, "_stop_child", fake_stop_child)

    ctrl._desired_state = DesiredState.STOPPED_BY_USER
    ctrl.reconcile()

    status = ctrl.get_status()
    assert status.controller_state == "ERROR"
    assert status.speaker_path_state == "FAILED"
    assert status.microphone_path_state == "STOPPED"
    assert "Fake speaker stop failure" in (status.last_actionable_error or "")

    ctrl.shutdown()


def test_stopped_by_user_reconcile_mic_stop_failure_sets_error_and_preserves_failed_state(isolated_env, monkeypatch):
    """Browser Review Correction 2: Under STOPPED_BY_USER reconcile, if microphone stop fails,
    controller_state must become ERROR and microphone_path_state must be FAILED, not STOPPED.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    class FakeDiscovery:
        peer_available = False
        peer_address = None
        local_bind_address = None
        is_ambiguous = False
        last_enumeration_error = None
        def start(self): pass
        def stop(self): pass
        def broadcast_hello(self): pass

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    ctrl.discovery_service = FakeDiscovery()
    assert ctrl.start_host()

    # Simulate microphone child stop failure
    def fake_stop_child(role):
        if role == "microphone":
            ctrl._microphone_path_state = PathState.FAILED
            ctrl._last_actionable_microphone_error = "Fake mic stop failure"
            return False
        return True

    monkeypatch.setattr(ctrl, "_stop_child", fake_stop_child)

    ctrl._desired_state = DesiredState.STOPPED_BY_USER
    ctrl.reconcile()

    status = ctrl.get_status()
    assert status.controller_state == "ERROR"
    assert status.speaker_path_state == "STOPPED"
    assert status.microphone_path_state == "FAILED"
    assert "could not be confirmed stopped or unjournaled" in (status.last_actionable_error or "")

    ctrl.shutdown()


def test_stopped_by_user_reconcile_success_sets_both_stopped_and_controller_stopped(isolated_env, monkeypatch):
    """Under STOPPED_BY_USER reconcile, when both child processes stop successfully,
    controller_state and both path states must be STOPPED.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    class FakeDiscovery:
        peer_available = False
        peer_address = None
        local_bind_address = None
        is_ambiguous = False
        last_enumeration_error = None
        def start(self): pass
        def stop(self): pass
        def broadcast_hello(self): pass

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    ctrl.discovery_service = FakeDiscovery()
    assert ctrl.start_host()

    monkeypatch.setattr(ctrl, "_stop_child", lambda role: True)

    ctrl._desired_state = DesiredState.STOPPED_BY_USER
    ctrl.reconcile()

    status = ctrl.get_status()
    assert status.controller_state == "STOPPED"
    assert status.speaker_path_state == "STOPPED"
    assert status.microphone_path_state == "STOPPED"
    assert status.last_actionable_error is None

    ctrl.shutdown()


def test_record_child_stopped_journal_read_error_returns_false(isolated_env, monkeypatch):
    """Correction 1: If child death is confirmed but _load_ownership_journal returns an error
    (e.g. corrupt or unreadable journal), _record_child_stopped must return False,
    causing _stop_child to return False and prevent claiming clean STOPPED.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    assert ctrl.start_host()

    proc = subprocess.Popen(
        ["sleep", "10"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ctrl._speaker_child_pid = proc.pid

    # Simulate journal read error from corrupt/unreadable file
    monkeypatch.setattr(ctrl, "_load_ownership_journal", lambda: (None, "Corrupt journal error"))

    # Child is dead after stop_process, but journal load returns error
    assert ctrl._record_child_stopped("speaker") is False
    assert ctrl._stop_child("speaker") is False
    assert ctrl._speaker_path_state.value == "FAILED"
    assert "ownership journal" in (ctrl._last_actionable_error or "").lower()

    # Reconcile under STOPPED_BY_USER must NOT claim clean STOPPED
    ctrl._desired_state = DesiredState.STOPPED_BY_USER
    ctrl.reconcile()
    status = ctrl.get_status()
    assert status.controller_state == "ERROR"
    assert status.speaker_path_state == "FAILED"

    ctrl.shutdown()


def test_speaker_journal_start_failure_cleanup_success(isolated_env, monkeypatch):
    """Correction 2: Speaker journal-start failure with successful cleanup:
    PID must be cleared, path marked FAILED, controller ERROR, and actionable journal error exposed.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    class FakeDiscovery:
        peer_available = True
        peer_address = "127.0.0.1"
        local_bind_address = "127.0.0.1"
        is_ambiguous = False
        last_enumeration_error = None
        def start(self): pass
        def stop(self): pass
        def broadcast_hello(self): pass

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    ctrl.discovery_service = FakeDiscovery()
    assert ctrl.start_host()

    # Simulate journal start recording failure
    monkeypatch.setattr(ctrl, "_record_child_started", lambda role, pid, cmd, port: False)

    ctrl.start()

    status = ctrl.get_status()
    assert status.speaker_path_state == "FAILED"
    assert status.controller_state == "ERROR"
    assert ctrl._speaker_child_pid is None
    assert "Failed to atomically record speaker child in ownership journal" in (status.last_actionable_error or "")

    ctrl.shutdown()


def test_speaker_journal_start_failure_cleanup_failure(isolated_env, monkeypatch):
    """Correction 2: Speaker journal-start failure with cleanup failure:
    Exact PID must be retained in memory, path FAILED, controller ERROR,
    and actionable error must report BOTH journal persistence failure AND cleanup failure.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    class FakeDiscovery:
        peer_available = True
        peer_address = "127.0.0.1"
        local_bind_address = "127.0.0.1"
        is_ambiguous = False
        last_enumeration_error = None
        def start(self): pass
        def stop(self): pass
        def broadcast_hello(self): pass

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    ctrl.discovery_service = FakeDiscovery()
    assert ctrl.start_host()

    # Simulate journal start recording failure
    monkeypatch.setattr(ctrl, "_record_child_started", lambda role, pid, cmd, port: False)
    # Simulate cleanup failure: stop_process returns False (cannot confirm death)
    monkeypatch.setattr(ctrl.process_runner, "stop_process", lambda pid: False)

    ctrl.start()

    status = ctrl.get_status()
    assert status.speaker_path_state == "FAILED"
    assert status.controller_state == "ERROR"
    assert ctrl._speaker_child_pid is not None
    assert "Failed to atomically record speaker child in ownership journal" in (status.last_actionable_error or "")
    assert "cleanup could not be confirmed" in (status.last_actionable_error or "")

    # Cleanup the actual spawned process
    if ctrl._speaker_child_pid and psutil.pid_exists(ctrl._speaker_child_pid):
        try:
            os.kill(ctrl._speaker_child_pid, signal.SIGKILL)
        except Exception:
            pass

    ctrl.shutdown()


def test_microphone_journal_start_failure_cleanup_success(isolated_env, monkeypatch):
    """Correction 2: Microphone journal-start failure with successful cleanup:
    PID must be cleared, path marked FAILED, and actionable microphone error exposed.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    class FakeDiscovery:
        peer_available = True
        peer_address = "127.0.0.1"
        local_bind_address = "127.0.0.1"
        is_ambiguous = False
        last_enumeration_error = None
        def start(self): pass
        def stop(self): pass
        def broadcast_hello(self): pass

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    ctrl.discovery_service = FakeDiscovery()
    assert ctrl.start_host()

    # Enable controller
    ctrl.start()

    # Set microphone desired
    ctrl._microphone_desired = True
    monkeypatch.setattr(ctrl, "_record_child_started", lambda role, pid, cmd, port: False)

    ctrl.reconcile()

    status = ctrl.get_status()
    assert status.microphone_path_state == "FAILED"
    assert ctrl._microphone_child_pid is None
    assert "Failed to atomically record microphone child in ownership journal" in (status.last_actionable_microphone_error or "")

    ctrl.shutdown()


def test_microphone_journal_start_failure_cleanup_failure(isolated_env, monkeypatch):
    """Correction 2: Microphone journal-start failure with cleanup failure:
    Exact PID must be retained in memory, path FAILED, controller ERROR,
    and actionable microphone error must report BOTH journal persistence failure AND cleanup failure.
    """
    state_file = isolated_env["state_file"]
    journal_file = isolated_env["journal_file"]
    lock_port = isolated_env["lock_port"]
    ipc_port = isolated_env["ipc_port"]

    class FakeDiscovery:
        peer_available = True
        peer_address = "127.0.0.1"
        local_bind_address = "127.0.0.1"
        is_ambiguous = False
        last_enumeration_error = None
        def start(self): pass
        def stop(self): pass
        def broadcast_hello(self): pass

    ctrl = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    ctrl.discovery_service = FakeDiscovery()
    assert ctrl.start_host()

    ctrl.start()
    ctrl._microphone_desired = True
    # Simulate journal start recording failure
    monkeypatch.setattr(ctrl, "_record_child_started", lambda role, pid, cmd, port: False)
    # Simulate cleanup failure: stop_process returns False (cannot confirm death)
    monkeypatch.setattr(ctrl.process_runner, "stop_process", lambda pid: False)

    ctrl.reconcile()

    status = ctrl.get_status()
    assert status.microphone_path_state == "FAILED"
    assert status.controller_state == "ERROR"
    assert ctrl._microphone_child_pid is not None
    assert "Failed to atomically record microphone child in ownership journal" in (status.last_actionable_microphone_error or "")
    assert "cleanup could not be confirmed" in (status.last_actionable_microphone_error or "")

    # Cleanup the actual spawned process
    if ctrl._microphone_child_pid and psutil.pid_exists(ctrl._microphone_child_pid):
        try:
            os.kill(ctrl._microphone_child_pid, signal.SIGKILL)
        except Exception:
            pass

    ctrl.shutdown()





