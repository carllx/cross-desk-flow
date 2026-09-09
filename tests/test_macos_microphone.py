"""Focused tests for macOS Production Microphone Sender (Issue #21).

Verifies:
- No microphone reports UNAVAILABLE / actionable error
- Sender canonical RTP L16 PT97 command formatting
- Microphone not started automatically when peer goes online
- Explicit microphone enable is idempotent
- Explicit microphone disable is idempotent
- Duplicate prevention (already running pipeline not re-spawned)
- Peer loss cleans up microphone child
- Ambiguous peer cleans up microphone child
- Unexpected child exit detected and re-spawned / reconciled
- Owned-child-only cleanup (microphone child killed, external PID untouched)
- Speaker receiver unaffected by microphone failure
- Status reflection is strictly read-only
"""

import json
import os
import tempfile
import time
from typing import List, Optional
import pytest

from bridge_core.contract import (
    DEFAULT_MIC_RTP_PORT,
    DEFAULT_SPEAKER_RTP_PORT,
    CANONICAL_MIC_CHANNELS,
    CANONICAL_MIC_PAYLOAD_TYPE,
    CANONICAL_MIC_SAMPLE_RATE,
    DesiredState,
    HostRole,
    LifecycleState,
    PathState,
)
from macos.controller import MacBridgeController
from macos.device_resolver import MacCoreAudioDeviceResolver, ResolvedAudioDevice
from macos.microphone_sender import MicrophoneSenderBuilder
from macos.process_runner import ProcessRunner


class FakeProcessRunner(ProcessRunner):
    def __init__(self):
        self._next_pid = 3000
        self.running_pids = set()
        self.started_commands: List[List[str]] = []
        self.stopped_pids: List[int] = []

    def start_process(self, cmd: List[str]) -> int:
        pid = self._next_pid
        self._next_pid += 1
        self.running_pids.add(pid)
        self.started_commands.append(cmd)
        return pid

    def stop_process(self, pid: int) -> bool:
        if pid in self.running_pids:
            self.running_pids.remove(pid)
            self.stopped_pids.append(pid)
        return True

    def is_running(self, pid: int) -> bool:
        return pid in self.running_pids

    def get_child_metadata(self, pid: int) -> Optional[dict]:
        if pid in self.running_pids or pid in self.stopped_pids:
            return {"pid": pid, "create_time": 1000.0}
        return None


class FakeDeviceResolver(MacCoreAudioDeviceResolver):
    def __init__(
        self,
        speaker: Optional[ResolvedAudioDevice] = None,
        microphone: Optional[ResolvedAudioDevice] = None,
        fail_speaker: bool = False,
        fail_mic: bool = False,
    ):
        if speaker is None and not fail_speaker:
            self._speaker = ResolvedAudioDevice(
                device_id=82,
                device_uid="BuiltInSpeakerDevice_Test",
                device_name="MacBook Air Speakers",
                is_builtin=True,
                output_channels=2,
                is_default=True,
                input_channels=0,
            )
        else:
            self._speaker = speaker

        if microphone is None and not fail_mic:
            self._microphone = ResolvedAudioDevice(
                device_id=89,
                device_uid="BuiltInMicrophoneDevice_Test",
                device_name="MacBook Air Microphone",
                is_builtin=True,
                output_channels=0,
                is_default=True,
                input_channels=1,
            )
        else:
            self._microphone = microphone

        self.fail_speaker = fail_speaker
        self.fail_mic = fail_mic

    def resolve_builtin_speaker_device(self) -> Optional[ResolvedAudioDevice]:
        if self.fail_speaker:
            return None
        return self._speaker

    def resolve_builtin_microphone_device(self) -> Optional[ResolvedAudioDevice]:
        if self.fail_mic:
            return None
        return self._microphone


class FakeDiscoveryService:
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
def temp_state_file():
    with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as f:
        path = f.name
    yield path
    if os.path.exists(path):
        os.remove(path)


# ---------------------------------------------------------
# Test Suite 1: Microphone Availability & Controller Binding
# (CoreAudio stable identity / UID hint tests moved to test_macos_microphone_identity.py)
# ---------------------------------------------------------


def test_no_microphone_reports_unavailable_error(temp_state_file):
    """When built-in microphone resolution fails, reports UNAVAILABLE with actionable error."""
    runner = FakeProcessRunner()
    resolver = FakeDeviceResolver(fail_mic=True)
    disc = FakeDiscoveryService()
    ctrl = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=resolver,
        discovery_service=disc,
        lock_port=50310,
        ipc_port=50311,
    )
    ctrl.start()
    ok = ctrl.set_microphone_enabled(True)
    assert ok is False

    st = ctrl.get_status()
    assert st.microphone_path_state == PathState.UNAVAILABLE.value
    assert "CoreAudio built-in microphone resolution failed" in (st.last_actionable_microphone_error or "")
    # Speaker path unaffected and running
    assert st.speaker_path_state == PathState.RUNNING.value
    assert st.owned_children_count == 1
    ctrl.shutdown()


# ---------------------------------------------------------
# Test Suite 2: Sender Command Formatting
# ---------------------------------------------------------

def test_sender_canonical_rtp_command_format():
    """Validates the canonical GStreamer command tokens generated by MicrophoneSenderBuilder."""
    builder = MicrophoneSenderBuilder(gst_path="/usr/local/bin/gst-launch-1.0")
    cmd = builder.build_sender_command(
        target_host="192.168.1.50",
        target_port=5006,
        device_id=89,
        local_bind_ip="192.168.1.100",
    )

    expected = [
        "/usr/local/bin/gst-launch-1.0",
        "-m",
        "osxaudiosrc",
        "device=89",
        "!",
        "audioconvert",
        "!",
        "audioresample",
        "!",
        "audio/x-raw,format=S16BE,rate=48000,channels=1",
        "!",
        "rtpL16pay",
        "pt=97",
        "!",
        "udpsink",
        "host=192.168.1.50",
        "port=5006",
        "sync=false",
        "bind-address=192.168.1.100",
    ]
    assert cmd == expected


# ---------------------------------------------------------
# Test Suite 3: Lifecycle & Controller Integration
# ---------------------------------------------------------

def test_microphone_auto_starts_on_enabled_with_peer(temp_state_file):
    """Under pre-#22 dual-active policy, microphone automatically starts when controller starts and peer is online."""
    runner = FakeProcessRunner()
    resolver = FakeDeviceResolver()
    disc = FakeDiscoveryService()
    ctrl = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=resolver,
        discovery_service=disc,
        lock_port=50312,
        ipc_port=50313,
    )
    ctrl.start()

    st = ctrl.get_status()
    assert st.controller_state == LifecycleState.ACTIVE.value
    assert st.speaker_path_state == PathState.RUNNING.value
    assert st.microphone_path_state == PathState.RUNNING.value
    assert st.owned_children_count == 2
    assert ctrl._microphone_child_pid is not None
    ctrl.shutdown()


def test_microphone_not_started_when_stopped_by_user(temp_state_file):
    """When persisted desired state is STOPPED_BY_USER, start_host leaves zero media children."""
    runner = FakeProcessRunner()
    resolver = FakeDeviceResolver()
    disc = FakeDiscoveryService()
    # Write STOPPED_BY_USER to state file
    with open(temp_state_file, "w") as f:
        json.dump({"desired_state": DesiredState.STOPPED_BY_USER.value}, f)

    ctrl = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=resolver,
        discovery_service=disc,
        lock_port=50312,
        ipc_port=50313,
    )
    assert ctrl.start_host() is True

    st = ctrl.get_status()
    assert st.controller_state == LifecycleState.STOPPED.value
    assert st.speaker_path_state == PathState.STOPPED.value
    assert st.microphone_path_state == PathState.STOPPED.value
    assert st.owned_children_count == 0
    assert ctrl._microphone_child_pid is None
    assert ctrl._speaker_child_pid is None
    ctrl.shutdown()


def test_reboot_persisted_enabled_restores_microphone_intent(temp_state_file):
    """When persisted desired state is ENABLED, start_host automatically restores microphone desired intent and runs mic."""
    runner = FakeProcessRunner()
    resolver = FakeDeviceResolver()
    disc = FakeDiscoveryService()
    # Write ENABLED to state file as left by previous session / reboot
    with open(temp_state_file, "w") as f:
        json.dump({"desired_state": DesiredState.ENABLED.value}, f)

    ctrl = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=resolver,
        discovery_service=disc,
        lock_port=50350,
        ipc_port=50351,
    )
    assert ctrl.start_host() is True
    assert ctrl._microphone_desired is True

    st = ctrl.get_status()
    assert st.desired_state == DesiredState.ENABLED.value
    assert st.controller_state == LifecycleState.ACTIVE.value
    assert st.speaker_path_state == PathState.RUNNING.value
    assert st.microphone_path_state == PathState.RUNNING.value
    assert st.owned_children_count == 2
    assert ctrl._speaker_child_pid is not None
    assert ctrl._microphone_child_pid is not None
    ctrl.shutdown()


def test_reboot_persisted_enabled_with_mic_permission_denied_fails_closed(temp_state_file):
    """When persisted desired state is ENABLED but mic permission is Denied, microphone fails closed while speaker remains ACTIVE."""
    runner = FakeProcessRunner()
    resolver = FakeDeviceResolver()
    disc = FakeDiscoveryService()
    # Write ENABLED to state file
    with open(temp_state_file, "w") as f:
        json.dump({"desired_state": DesiredState.ENABLED.value}, f)

    # Mic permission returns 2 (Denied)
    ctrl = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=resolver,
        discovery_service=disc,
        mic_permission_probe=lambda: 2,
        lock_port=50352,
        ipc_port=50353,
    )
    assert ctrl.start_host() is True
    assert ctrl._microphone_desired is True

    st = ctrl.get_status()
    assert st.desired_state == DesiredState.ENABLED.value
    assert st.speaker_path_state == PathState.RUNNING.value
    assert st.microphone_path_state == PathState.FAILED.value
    assert "Microphone permission denied" in (st.last_actionable_microphone_error or "")
    assert st.owned_children_count == 1
    assert ctrl._speaker_child_pid is not None
    assert ctrl._microphone_child_pid is None
    ctrl.shutdown()


def test_explicit_microphone_enable_and_disable_idempotence(temp_state_file):
    """Explicitly enabling and disabling microphone is fully idempotent."""
    runner = FakeProcessRunner()
    resolver = FakeDeviceResolver()
    disc = FakeDiscoveryService()
    ctrl = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=resolver,
        discovery_service=disc,
        lock_port=50314,
        ipc_port=50315,
    )
    ctrl.start()

    # Enable microphone
    ok1 = ctrl.set_microphone_enabled(True)
    assert ok1 is True
    assert ctrl.get_status().microphone_path_state == PathState.RUNNING.value
    assert ctrl.get_status().owned_children_count == 2
    mic_pid = ctrl._microphone_child_pid
    assert mic_pid is not None

    # Repeated enable does not spawn duplicate
    ok2 = ctrl.set_microphone_enabled(True)
    assert ok2 is True
    assert ctrl._microphone_child_pid == mic_pid
    assert len(runner.started_commands) == 2  # 1 speaker + 1 mic

    # Disable microphone
    ok3 = ctrl.set_microphone_enabled(False)
    assert ok3 is True
    assert ctrl.get_status().microphone_path_state == PathState.STOPPED.value
    assert ctrl.get_status().owned_children_count == 1
    assert mic_pid in runner.stopped_pids
    assert ctrl._microphone_child_pid is None

    # Repeated disable is safe
    ok4 = ctrl.set_microphone_enabled(False)
    assert ok4 is True
    assert ctrl.get_status().microphone_path_state == PathState.STOPPED.value
    ctrl.shutdown()


def test_duplicate_microphone_sender_prevention(temp_state_file):
    """Reconcile loops do not create multiple microphone sender processes if one is already running."""
    runner = FakeProcessRunner()
    resolver = FakeDeviceResolver()
    disc = FakeDiscoveryService()
    ctrl = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=resolver,
        discovery_service=disc,
        lock_port=50316,
        ipc_port=50317,
    )
    ctrl.start()
    ctrl.set_microphone_enabled(True)

    mic_pid = ctrl._microphone_child_pid
    # Reconcile multiple times
    for _ in range(5):
        ctrl.reconcile()
        assert ctrl._microphone_child_pid == mic_pid
        assert ctrl.get_status().owned_children_count == 2

    assert len(runner.started_commands) == 2
    ctrl.shutdown()


def test_peer_loss_cleans_up_microphone_child(temp_state_file):
    """When peer is lost (peer_available=False), microphone child is stopped."""
    runner = FakeProcessRunner()
    resolver = FakeDeviceResolver()
    disc = FakeDiscoveryService()
    ctrl = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=resolver,
        discovery_service=disc,
        lock_port=50318,
        ipc_port=50319,
    )
    ctrl.start()
    ctrl.set_microphone_enabled(True)
    assert ctrl.get_status().owned_children_count == 2

    mic_pid = ctrl._microphone_child_pid
    spk_pid = ctrl._speaker_child_pid

    # Peer becomes unavailable
    disc.peer_available = False
    ctrl.reconcile()

    assert mic_pid in runner.stopped_pids
    assert spk_pid in runner.stopped_pids
    assert ctrl.get_status().owned_children_count == 0
    assert ctrl.get_status().microphone_path_state == PathState.IDLE.value
    assert ctrl.get_status().speaker_path_state == PathState.IDLE.value
    ctrl.shutdown()


def test_ambiguous_peer_cleans_up_microphone_child(temp_state_file):
    """When peer state becomes ambiguous, microphone child is stopped."""
    runner = FakeProcessRunner()
    resolver = FakeDeviceResolver()
    disc = FakeDiscoveryService()
    ctrl = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=resolver,
        discovery_service=disc,
        lock_port=50320,
        ipc_port=50321,
    )
    ctrl.start()
    ctrl.set_microphone_enabled(True)
    assert ctrl.get_status().owned_children_count == 2

    mic_pid = ctrl._microphone_child_pid
    spk_pid = ctrl._speaker_child_pid

    # Ambiguity introduced
    disc.is_ambiguous = True
    ctrl.reconcile()

    assert mic_pid in runner.stopped_pids
    assert spk_pid in runner.stopped_pids
    assert ctrl.get_status().owned_children_count == 0
    assert ctrl.get_status().controller_state == LifecycleState.AMBIGUOUS_PEER.value
    assert ctrl.get_status().microphone_path_state == PathState.IDLE.value
    ctrl.shutdown()


def test_unexpected_microphone_child_exit_detected_and_reconciled(temp_state_file):
    """Unexpected exit of microphone process is detected on reconcile and re-spawned."""
    runner = FakeProcessRunner()
    resolver = FakeDeviceResolver()
    disc = FakeDiscoveryService()
    ctrl = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=resolver,
        discovery_service=disc,
        lock_port=50322,
        ipc_port=50323,
    )
    ctrl.start()
    ctrl.set_microphone_enabled(True)
    mic_pid = ctrl._microphone_child_pid
    assert mic_pid is not None

    # Simulate child exiting unexpectedly
    runner.running_pids.remove(mic_pid)

    assert ctrl.get_status().owned_children_count == 1

    # Reconcile detects unexpected exit and restarts sender
    ctrl.reconcile()
    assert ctrl.get_status().microphone_path_state == PathState.RUNNING.value
    assert ctrl.get_status().owned_children_count == 2
    assert len(runner.started_commands) == 3
    ctrl.shutdown()


def test_owned_child_only_cleanup_for_microphone(temp_state_file):
    """Controller stop terminates owned mic and speaker children without affecting unrelated PIDs."""
    runner = FakeProcessRunner()
    external_pid = 9999
    runner.running_pids.add(external_pid)

    resolver = FakeDeviceResolver()
    disc = FakeDiscoveryService()
    ctrl = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=resolver,
        discovery_service=disc,
        lock_port=50324,
        ipc_port=50325,
    )
    ctrl.start()
    ctrl.set_microphone_enabled(True)
    assert ctrl.get_status().owned_children_count == 2

    spk_pid = ctrl._speaker_child_pid
    mic_pid = ctrl._microphone_child_pid

    ctrl.stop()
    assert spk_pid in runner.stopped_pids
    assert mic_pid in runner.stopped_pids
    assert external_pid not in runner.stopped_pids
    assert external_pid in runner.running_pids
    assert ctrl.get_status().owned_children_count == 0
    assert ctrl.get_status().microphone_path_state == PathState.STOPPED.value
    assert ctrl.get_status().speaker_path_state == PathState.STOPPED.value
    ctrl.shutdown()


def test_speaker_receiver_unaffected_by_microphone_failure(temp_state_file):
    """Failure in microphone resolution or launch must NOT disrupt or stop active speaker receiver."""
    runner = FakeProcessRunner()
    resolver = FakeDeviceResolver()
    disc = FakeDiscoveryService()
    ctrl = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=resolver,
        discovery_service=disc,
        lock_port=50326,
        ipc_port=50327,
    )
    ctrl.start()
    assert ctrl.get_status().speaker_path_state == PathState.RUNNING.value
    spk_pid = ctrl._speaker_child_pid
    # Under pre-#22 dual-active, mic started running on ctrl.start()
    assert ctrl.get_status().microphone_path_state == PathState.RUNNING.value
    # Disable first
    ctrl.set_microphone_enabled(False)
    assert ctrl.get_status().microphone_path_state == PathState.STOPPED.value

    # Now simulate mic resolution failure and re-enable
    resolver.fail_mic = True
    ok = ctrl.set_microphone_enabled(True)
    assert ok is False

    # Speaker is still RUNNING, with same PID
    assert ctrl._speaker_child_pid == spk_pid
    assert ctrl.get_status().speaker_path_state == PathState.RUNNING.value
    assert ctrl.get_status().microphone_path_state == PathState.UNAVAILABLE.value
    assert spk_pid not in runner.stopped_pids
    ctrl.shutdown()


def test_status_reflection_read_only(temp_state_file):
    """get_status() is strictly read-only and does not trigger reconciles or process operations."""
    runner = FakeProcessRunner()
    resolver = FakeDeviceResolver()
    disc = FakeDiscoveryService()
    ctrl = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=resolver,
        discovery_service=disc,
        lock_port=50328,
        ipc_port=50329,
    )
    ctrl.start()
    ctrl.set_microphone_enabled(True)

    commands_count = len(runner.started_commands)
    stopped_count = len(runner.stopped_pids)

    for _ in range(5):
        st = ctrl.get_status()
        assert st.microphone_path_state == PathState.RUNNING.value
        assert st.pack43_available is None  # Mac must keep None
        assert st.microphone_port == DEFAULT_MIC_RTP_PORT

    assert len(runner.started_commands) == commands_count
    assert len(runner.stopped_pids) == stopped_count
    ctrl.shutdown()
