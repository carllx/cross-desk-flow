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

from bridge_core.contract import DesiredState, DEFAULT_SPEAKER_RTP_PORT
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
        assert ctrl.start_host()

        time.sleep(0.5)
        assert psutil.pid_exists(py_pid), "Non-GStreamer process must NOT be touched by recovery!"
        assert py_proc.poll() is None

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
