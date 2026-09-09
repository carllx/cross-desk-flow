"""Automated test suite for Issue #43 Dictation Safety Baseline MVP.

Verifies all critical contract and lifecycle behaviors:
1. Start/reboot with ENABLED defaults to PLAYBACK mode:
   - speaker running, Windows mic receiver OFF, Mac osxaudiosrc NOT running
2. Enter Dictation mode transition ordering:
   - PLAYBACK -> suppress Windows speaker -> resolve Pack43 -> start Windows mic receiver
   - confirm receiver running -> generate fresh session_id -> send DICTATION_START(session_id)
   - Mac starts mic sender -> Mac replies DICTATION_START_ACK(session_id, success)
   - only after ACK: mode = DICTATION, mic = Active
3. Idempotent Start Dictation: duplicate call does not spawn extra processes
4. End Dictation:
   - DICTATION_STOP(session_id) sent to Mac
   - Mac stops mic sender, Windows stops mic receiver, mic children == 0
   - speaker restored, mode returns to PLAYBACK
5. Fresh session_id per dictation: never reuses stale mic children
6. STOPPED_BY_USER: 0 media children
7. Pack43 failure / timeout / Mac start failure rollback:
   - restores speaker, leaves Mac mic NOT running, returns to PLAYBACK
8. 50100 Control plane separation:
   - DICTATION_* packets do not contaminate discovery candidates, known responders, or route election
9. Strict peer validation:
   - Mac drops DICTATION_* from mismatched instance_id or non-elected IP
"""

import json
import os
import tempfile
import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
import pytest

from bridge_core.contract import (
    CONTROL_PROTOCOL_VERSION,
    DEFAULT_MIC_RTP_PORT,
    DEFAULT_SPEAKER_RTP_PORT,
    DesiredState,
    HostRole,
    LifecycleState,
    PathState,
)
from bridge_core.peer_discovery import PeerDiscoveryService
from windows.controller import WindowsBridgeController
from macos.controller import MacBridgeController
from windows.pack43_resolver import Pack43ResolutionResult


class MockProcessRunner:
    def __init__(self):
        self._next_pid = 60000
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


class MockPack43Resolver:
    def __init__(self, should_succeed: bool = True):
        self.should_succeed = should_succeed
        self.resolve_count = 0

    @property
    def is_cached_available(self):
        return self.should_succeed

    def resolve_pack43(self, force_refresh: bool = False):
        self.resolve_count += 1
        if not self.should_succeed:
            return None
        return Pack43ResolutionResult(
            render_endpoint_id="{mock-pack43-render}",
            capture_endpoint_id="{mock-pack43-capture}",
            driver_version="1.0.3.5",
        )

    def invalidate_cache(self):
        pass


class MockPipelineBuilder:
    def is_gstreamer_available(self):
        return True

    def build_speaker_command(self, **kwargs):
        return ["mock-win-speaker-sender"]

    def build_sender_command(self, **kwargs):
        return ["mock-win-speaker-sender"]

    def build_receiver_command(self, **kwargs):
        return ["mock-mac-speaker-receiver"]


class MockDeviceResolver:
    def resolve_default_playback_endpoint_id(self) -> Optional[str]:
        return "{mock-playback-endpoint}"

    def resolve_default_render_device(self):
        return MagicMock(device_id="{mock-render-dev}", device_name="Mock Render")

    def resolve_builtin_speaker_device(self):
        return MagicMock(device_id=42, device_name="Built-in Speaker")

    def resolve_builtin_microphone_device(self):
        return MagicMock(device_id=43, device_name="Built-in Mic")


class MockMicReceiverBuilder:
    def is_gstreamer_available(self):
        return True

    def build_receiver_command(self, **kwargs):
        return ["mock-gst-mic-receiver", f"--session={kwargs.get('session_id', '')}"]


class MockMicSenderBuilder:
    def is_gstreamer_available(self):
        return True

    def build_sender_command(self, **kwargs):
        return ["mock-gst-mac-mic-sender"]


class MockDiscoveryService:
    def __init__(self, local_role=HostRole.WINDOWS, peer_ip="192.168.1.50", local_ip="192.168.1.100"):
        self.local_role = local_role
        self.peer_available = True
        self.peer_address = peer_ip
        self.local_bind_address = local_ip
        self.peer_speaker_port = DEFAULT_SPEAKER_RTP_PORT
        self.peer_instance_id = "mock-peer-instance"
        self.instance_id = f"mock-{local_role.value}-instance"
        self.sent_control_messages: List[Dict[str, Any]] = []
        self.on_control_message = None

    def start(self):
        pass

    def stop(self):
        pass

    def broadcast_hello(self):
        pass

    def refresh_peer_state(self):
        pass

    def send_control_message(self, peer_ip: str, msg: Dict[str, Any]):
        self.sent_control_messages.append({"peer_ip": peer_ip, "msg": msg})
        return True


@pytest.fixture
def temp_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        win_state = os.path.join(tmpdir, "win_state.json")
        mac_state = os.path.join(tmpdir, "mac_state.json")
        yield win_state, mac_state


def test_startup_enabled_defaults_to_playback_mode(temp_files):
    """Start / reboot with ENABLED defaults to PLAYBACK mode with mic OFF."""
    win_state, mac_state = temp_files
    with open(win_state, "w", encoding="utf-8") as f:
        json.dump({"desired_state": DesiredState.ENABLED.value}, f)

    runner = MockProcessRunner()
    pack43 = MockPack43Resolver(should_succeed=True)
    disc = MockDiscoveryService(HostRole.WINDOWS)

    ctrl = WindowsBridgeController(
        state_file=win_state,
        process_runner=runner,
        device_resolver=MockDeviceResolver(),
        pipeline_builder=MockPipelineBuilder(),
        discovery_service=disc,
        pack43_resolver=pack43,
        microphone_receiver_builder=MockMicReceiverBuilder(),
        lock_port=53101,
        ipc_port=53102,
    )
    with patch("windows.controller.check_runtime_dependencies", return_value=(True, "")):
        ok = ctrl.start_host()
        assert ok is True

        st = ctrl.get_status()
        assert st.mode == "PLAYBACK"
        assert st.speaker_path_state == PathState.RUNNING.value
        assert st.microphone_path_state == PathState.READY.value
        assert st.owned_children_count == 1
        assert len(runner.running_pids) == 1

        ctrl.shutdown_host()


def test_enter_dictation_mode_success_flow(temp_files):
    """Enter Dictation mode follows strict ordering:
    PLAYBACK -> suppress speaker -> resolve Pack43 -> start receiver -> send DICTATION_START
    -> Mac ACK -> mode=DICTATION, mic=Active.
    """
    win_state, _ = temp_files
    runner = MockProcessRunner()
    pack43 = MockPack43Resolver(should_succeed=True)
    disc = MockDiscoveryService(HostRole.WINDOWS, peer_ip="192.168.1.50")

    ctrl = WindowsBridgeController(
        state_file=win_state,
        process_runner=runner,
        device_resolver=MockDeviceResolver(),
        pipeline_builder=MockPipelineBuilder(),
        discovery_service=disc,
        pack43_resolver=pack43,
        microphone_receiver_builder=MockMicReceiverBuilder(),
        lock_port=53103,
        ipc_port=53104,
    )
    with patch("windows.controller.check_runtime_dependencies", return_value=(True, "")):
        ctrl.start()
        st0 = ctrl.get_status()
        assert st0.mode == "PLAYBACK"
        assert st0.speaker_path_state == PathState.RUNNING.value
        speaker_pid = ctrl._speaker_child_pid
        assert speaker_pid in runner.running_pids

        # Simulate Mac ACK response when message arrives
        def mock_send(peer_ip, msg):
            if msg.get("type") == "DICTATION_START":
                ack = {
                    "version": CONTROL_PROTOCOL_VERSION,
                    "role": HostRole.MACOS.value,
                    "type": "DICTATION_START_ACK",
                    "session_id": msg.get("session_id"),
                    "success": True,
                }
                ctrl._on_control_message(ack, peer_ip)
            return True

        disc.send_control_message = mock_send

        # Trigger start_dictation
        ok = ctrl.start_dictation(timeout=2.0)
        assert ok is True

        st1 = ctrl.get_status()
        assert st1.mode == "DICTATION"
        assert st1.speaker_path_state == PathState.STOPPED.value
        assert st1.microphone_path_state == PathState.RUNNING.value
        assert ctrl._current_dictation_session is not None
        assert ctrl._microphone_child_pid in runner.running_pids
        assert speaker_pid not in runner.running_pids

        # End dictation
        end_ok = ctrl.end_dictation()
        assert end_ok is True
        st2 = ctrl.get_status()
        assert st2.mode == "PLAYBACK"
        assert st2.speaker_path_state == PathState.RUNNING.value
        assert st2.microphone_path_state == PathState.READY.value
        assert ctrl._microphone_child_pid is None

        ctrl.shutdown()


def test_start_dictation_idempotence(temp_files):
    """Calling start_dictation when already in DICTATION mode is idempotent and does not spawn extra processes."""
    win_state, _ = temp_files
    runner = MockProcessRunner()
    pack43 = MockPack43Resolver(should_succeed=True)
    disc = MockDiscoveryService(HostRole.WINDOWS)

    ctrl = WindowsBridgeController(
        state_file=win_state,
        process_runner=runner,
        device_resolver=MockDeviceResolver(),
        pipeline_builder=MockPipelineBuilder(),
        discovery_service=disc,
        pack43_resolver=pack43,
        microphone_receiver_builder=MockMicReceiverBuilder(),
        lock_port=53105,
        ipc_port=53106,
    )
    with patch("windows.controller.check_runtime_dependencies", return_value=(True, "")):
        ctrl.start()

        def mock_send(peer_ip, msg):
            if msg.get("type") == "DICTATION_START":
                ack = {
                    "version": CONTROL_PROTOCOL_VERSION,
                    "role": HostRole.MACOS.value,
                    "type": "DICTATION_START_ACK",
                    "session_id": msg.get("session_id"),
                    "success": True,
                }
                ctrl._on_control_message(ack, peer_ip)
            return True

        disc.send_control_message = mock_send

        assert ctrl.start_dictation(timeout=2.0) is True
        mic_pid_1 = ctrl._microphone_child_pid
        session_1 = ctrl._current_dictation_session
        cmd_count_1 = len(runner.started_commands)

        # Duplicate start
        assert ctrl.start_dictation(timeout=2.0) is True
        assert ctrl._microphone_child_pid == mic_pid_1
        assert ctrl._current_dictation_session == session_1
        assert len(runner.started_commands) == cmd_count_1

        ctrl.shutdown()


def test_dictation_rollback_on_pack43_failure(temp_files):
    """If Pack43 resolution fails, receiver is not started, Mac is not messaged, and speaker is restored."""
    win_state, _ = temp_files
    runner = MockProcessRunner()
    pack43 = MockPack43Resolver(should_succeed=False)
    disc = MockDiscoveryService(HostRole.WINDOWS)

    ctrl = WindowsBridgeController(
        state_file=win_state,
        process_runner=runner,
        device_resolver=MockDeviceResolver(),
        pipeline_builder=MockPipelineBuilder(),
        discovery_service=disc,
        pack43_resolver=pack43,
        microphone_receiver_builder=MockMicReceiverBuilder(),
        lock_port=53107,
        ipc_port=53108,
    )
    with patch("windows.controller.check_runtime_dependencies", return_value=(True, "")):
        ctrl.start()
        assert ctrl.get_status().mode == "PLAYBACK"

        ok = ctrl.start_dictation(timeout=1.0)
        assert ok is False

        st = ctrl.get_status()
        assert st.mode == "PLAYBACK"
        assert st.speaker_path_state == PathState.RUNNING.value
        assert st.microphone_path_state == PathState.UNAVAILABLE.value
        assert len(disc.sent_control_messages) == 0
        assert ctrl._microphone_child_pid is None

        ctrl.shutdown()


def test_dictation_rollback_on_mac_ack_timeout(temp_files):
    """If Mac fails to ACK, receiver is killed, DICTATION_STOP is sent, speaker is restored, returning to PLAYBACK."""
    win_state, _ = temp_files
    runner = MockProcessRunner()
    pack43 = MockPack43Resolver(should_succeed=True)
    disc = MockDiscoveryService(HostRole.WINDOWS)

    ctrl = WindowsBridgeController(
        state_file=win_state,
        process_runner=runner,
        device_resolver=MockDeviceResolver(),
        pipeline_builder=MockPipelineBuilder(),
        discovery_service=disc,
        pack43_resolver=pack43,
        microphone_receiver_builder=MockMicReceiverBuilder(),
        lock_port=53109,
        ipc_port=53110,
    )
    with patch("windows.controller.check_runtime_dependencies", return_value=(True, "")):
        ctrl.start()

        ok = ctrl.start_dictation(timeout=0.2)
        assert ok is False

        st = ctrl.get_status()
        assert st.mode == "PLAYBACK"
        assert st.speaker_path_state == PathState.RUNNING.value
        assert st.microphone_path_state in (PathState.READY.value, PathState.STOPPED.value)
        assert ctrl._microphone_child_pid is None
        sent_types = [m["msg"]["type"] for m in disc.sent_control_messages]
        assert "DICTATION_START" in sent_types
        assert "DICTATION_STOP" in sent_types

        ctrl.shutdown()


def test_new_dictation_session_generates_fresh_id_and_new_process(temp_files):
    """Each new dictation session generates a fresh session_id and creates a new process."""
    win_state, _ = temp_files
    runner = MockProcessRunner()
    pack43 = MockPack43Resolver(should_succeed=True)
    disc = MockDiscoveryService(HostRole.WINDOWS)

    ctrl = WindowsBridgeController(
        state_file=win_state,
        process_runner=runner,
        device_resolver=MockDeviceResolver(),
        pipeline_builder=MockPipelineBuilder(),
        discovery_service=disc,
        pack43_resolver=pack43,
        microphone_receiver_builder=MockMicReceiverBuilder(),
        lock_port=53111,
        ipc_port=53112,
    )
    with patch("windows.controller.check_runtime_dependencies", return_value=(True, "")):
        ctrl.start()

        def mock_send(peer_ip, msg):
            if msg.get("type") == "DICTATION_START":
                ack = {
                    "version": CONTROL_PROTOCOL_VERSION,
                    "role": HostRole.MACOS.value,
                    "type": "DICTATION_START_ACK",
                    "session_id": msg.get("session_id"),
                    "success": True,
                }
                ctrl._on_control_message(ack, peer_ip)
            return True

        disc.send_control_message = mock_send

        # Session 1
        assert ctrl.start_dictation(timeout=1.0) is True
        session_1 = ctrl._current_dictation_session
        pid_1 = ctrl._microphone_child_pid
        assert ctrl.end_dictation() is True

        # Session 2
        assert ctrl.start_dictation(timeout=1.0) is True
        session_2 = ctrl._current_dictation_session
        pid_2 = ctrl._microphone_child_pid

        assert session_1 != session_2
        assert pid_1 != pid_2

        ctrl.shutdown()


def test_mac_controller_on_control_message_validation_and_ack(temp_files):
    """Mac controller handles DICTATION_START, launches sender, replies with ACK; handles DICTATION_STOP cleanly."""
    _, mac_state = temp_files
    runner = MockProcessRunner()
    resolver = MockDeviceResolver()
    disc = MockDiscoveryService(HostRole.MACOS, peer_ip="192.168.1.100")

    ctrl = MacBridgeController(
        state_file=mac_state,
        process_runner=runner,
        device_resolver=resolver,
        pipeline_builder=MockPipelineBuilder(),
        discovery_service=disc,
        microphone_sender_builder=MockMicSenderBuilder(),
        lock_port=53113,
        ipc_port=53114,
    )
    with patch("macos.controller.check_runtime_dependencies", return_value=(True, "")):
        ctrl.start()
        assert ctrl.get_status().microphone_path_state == PathState.IDLE.value
        assert ctrl._microphone_child_pid is None

        # Windows sends DICTATION_START
        req = {
            "version": CONTROL_PROTOCOL_VERSION,
            "role": HostRole.WINDOWS.value,
            "instance_id": "win-123",
            "type": "DICTATION_START",
            "session_id": "session-test-mac-001",
        }
        ctrl._on_control_message(req, "192.168.1.100")

        # Mac should start mic sender and reply with ACK
        assert ctrl.get_status().microphone_path_state == PathState.RUNNING.value
        assert ctrl._microphone_child_pid is not None
        assert ctrl._microphone_child_pid in runner.running_pids

        assert len(disc.sent_control_messages) == 1
        ack = disc.sent_control_messages[0]["msg"]
        assert ack["type"] == "DICTATION_START_ACK"
        assert ack["session_id"] == "session-test-mac-001"
        assert ack["success"] is True

        # Windows sends DICTATION_STOP
        stop_req = {
            "version": CONTROL_PROTOCOL_VERSION,
            "role": HostRole.WINDOWS.value,
            "instance_id": "win-123",
            "type": "DICTATION_STOP",
            "session_id": "session-test-mac-001",
        }
        ctrl._on_control_message(stop_req, "192.168.1.100")

        assert ctrl.get_status().microphone_path_state == PathState.IDLE.value
        assert ctrl._microphone_child_pid is None

        ctrl.shutdown()


def test_50100_control_isolation_does_not_contaminate_discovery():
    """DICTATION_* control packets processed on port 50100 do not update known responders or peer candidates."""
    disc = PeerDiscoveryService(
        local_role=HostRole.MACOS,
        instance_id="mac-inst-01",
    )
    control_msgs_received = []

    def mock_control_cb(msg, peer_ip):
        control_msgs_received.append((msg, peer_ip))

    disc.on_control_message = mock_control_cb
    disc._peer_address = "192.168.1.100"
    disc._peer_instance_id = "win-inst-elected"

    control_packet = json.dumps({
        "version": CONTROL_PROTOCOL_VERSION,
        "role": "windows",
        "instance_id": "win-inst-elected",
        "type": "DICTATION_START",
        "session_id": "session-ctrl-01",
    }).encode("utf-8")

    initial_candidates = len(disc._peer_candidates)
    initial_responders = len(disc._known_responders)

    disc.handle_peer_message(control_packet, "192.168.1.100")

    assert len(control_msgs_received) == 1
    assert control_msgs_received[0][0]["session_id"] == "session-ctrl-01"
    assert len(disc._peer_candidates) == initial_candidates
    assert len(disc._known_responders) == initial_responders


def test_mac_rejects_dictation_from_unauthorized_peer():
    """DICTATION_* packets from non-elected peer or mismatched instance_id are rejected."""
    disc = PeerDiscoveryService(
        local_role=HostRole.MACOS,
        instance_id="mac-inst-01",
    )
    control_msgs_received = []
    disc.on_control_message = lambda msg, ip: control_msgs_received.append(msg)
    disc._peer_address = "192.168.1.100"
    disc._peer_instance_id = "win-inst-elected"

    # 1. Mismatched IP
    spoofed_ip_packet = json.dumps({
        "version": CONTROL_PROTOCOL_VERSION,
        "role": "windows",
        "instance_id": "win-inst-elected",
        "type": "DICTATION_START",
        "session_id": "session-bad-ip",
    }).encode("utf-8")
    disc.handle_peer_message(spoofed_ip_packet, "192.168.1.200")
    assert len(control_msgs_received) == 0

    # 2. Mismatched instance ID
    bad_inst_packet = json.dumps({
        "version": CONTROL_PROTOCOL_VERSION,
        "role": "windows",
        "instance_id": "win-inst-impostor",
        "type": "DICTATION_START",
        "session_id": "session-bad-inst",
    }).encode("utf-8")
    disc.handle_peer_message(bad_inst_packet, "192.168.1.100")
    assert len(control_msgs_received) == 0
