"""Comprehensive regression and external behavior automated tests for macOS controller.
Covers: external behavior, IPC lifecycle, singleton lock, route resolution, peer availability, ambiguity, device resolution, and shared contracts.
"""

import json
import os
import tempfile
import time
from typing import List, Optional
import pytest

from bridge_core.contract import (
    CONTROL_PROTOCOL_VERSION,
    DEFAULT_CONTROL_PORT,
    DEFAULT_LOCAL_IPC_PORT,
    DEFAULT_SINGLETON_PORT,
    DEFAULT_SPEAKER_RTP_PORT,
    ControllerStatus,
    DesiredState,
    HostRole,
    LifecycleState,
    PathState,
)
from bridge_core.peer_discovery import (
    PeerDiscoveryService,
    RouteResolver,
    is_eligible_onlink_ipv4,
)
from bridge_core.process_runner import ProcessRunner
from macos.cli import send_ipc_command
from macos.controller import MacBridgeController, SingleInstanceLock
from macos.device_resolver import MacCoreAudioDeviceResolver, ResolvedAudioDevice
from macos.speaker_receiver import SpeakerReceiverBuilder


class FakeProcessRunner(ProcessRunner):
    def __init__(self):
        self._next_pid = 2000
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
        fail: bool = False,
        fail_mic: bool = False,
    ):
        self._device = speaker if (speaker or fail) else ResolvedAudioDevice(
            device_id=88, device_uid="BuiltInSpeakerUID_Test", device_name="MacBook Air Speakers",
            is_builtin=True, output_channels=2, is_default=True, input_channels=0,
        )
        self._microphone = microphone if (microphone or fail or fail_mic) else ResolvedAudioDevice(
            device_id=89, device_uid="BuiltInMicrophoneUID_Test", device_name="MacBook Air Microphone",
            is_builtin=True, output_channels=0, is_default=True, input_channels=1,
        )
        self.fail, self.fail_mic = fail, fail_mic

    def resolve_builtin_speaker_device(self) -> Optional[ResolvedAudioDevice]:
        return None if self.fail else self._device

    def resolve_builtin_microphone_device(self) -> Optional[ResolvedAudioDevice]:
        return None if (self.fail or self.fail_mic) else self._microphone


class FakeReceiverBuilder(SpeakerReceiverBuilder):
    def __init__(self, gst_available: bool = True):
        self._available = gst_available
        self.last_built_cmd: Optional[List[str]] = None

    def is_gstreamer_available(self) -> bool:
        return self._available

    def build_receiver_command(
        self,
        local_bind_ip: str,
        local_port: int = DEFAULT_SPEAKER_RTP_PORT,
        device_id: Optional[int] = None,
    ) -> List[str]:
        cmd = [
            "fake-mac-gst",
            f"--bind={local_bind_ip}",
            f"--port={local_port}",
        ]
        if device_id is not None:
            cmd.append(f"--device={device_id}")
        self.last_built_cmd = cmd
        return cmd


class FakeDiscoveryService:
    def __init__(
        self,
        peer_available: bool = True,
        peer_address: str = "198.168.10.5",
        local_bind: str = "198.168.10.4",
        peer_port: int = DEFAULT_SPEAKER_RTP_PORT,
        is_ambiguous: bool = False,
    ):
        self.peer_available = peer_available
        self.peer_address = peer_address
        self.local_bind_address = local_bind
        self.peer_speaker_port = peer_port
        self.is_ambiguous = is_ambiguous
        self.last_enumeration_error = None
        self.started = False
        self.stopped = False
        self.broadcast_count = 0

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def broadcast_hello(self):
        self.broadcast_count += 1
class FakeSpeakerVolumeRelay:
    def __init__(self, bind_ip: str, listen_port: int = 5004, target_port: int = 5005, target_ip: str = "127.0.0.1"):
        self.bind_ip, self.listen_port, self.target_port, self.target_ip = bind_ip, listen_port, target_port, target_ip
        self.volume, self._running = 1.0, False

    @property
    def is_running(self) -> bool:
        return self._running

    def set_volume(self, vol: float) -> None:
        self.volume = max(0.0, min(1.0, float(vol)))

    def start(self) -> bool:
        self._running = True
        return True

    def stop(self) -> None:
        self._running = False


@pytest.fixture(autouse=True)
def isolate_speaker_relay_for_controller_tests(monkeypatch):
    monkeypatch.setattr("macos.voice_ducking.SpeakerVolumeRelay", FakeSpeakerVolumeRelay)


@pytest.fixture
def temp_state_file():
    with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as f:
        path = f.name
    yield path
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


# ---------------------------------------------------------
# Test Suite 1: Mac Controller External Behavior & Lifecycle
# ---------------------------------------------------------

def test_repeated_start_is_idempotent_and_creates_single_receiver(temp_state_file):
    runner = FakeProcessRunner()
    controller = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=FakeDeviceResolver(),
        pipeline_builder=FakeReceiverBuilder(),
        discovery_service=FakeDiscoveryService(peer_available=True),
        lock_port=50250,
        ipc_port=50260,
    )

    assert controller.start() is True
    status1 = controller.get_status()
    assert status1.desired_state == DesiredState.ENABLED.value
    assert status1.speaker_path_state == PathState.RUNNING.value
    assert status1.microphone_path_state == PathState.IDLE.value
    assert len(runner.started_commands) == 1

    assert controller.start() is True
    status2 = controller.get_status()
    assert status2.desired_state == DesiredState.ENABLED.value
    assert len(runner.started_commands) == 1
    assert status2.owned_children_count == 1

    controller.shutdown()


def test_repeated_stop_is_idempotent_and_cleans_owned_process(temp_state_file):
    runner = FakeProcessRunner()
    controller = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=FakeDeviceResolver(),
        pipeline_builder=FakeReceiverBuilder(),
        discovery_service=FakeDiscoveryService(peer_available=True),
        lock_port=50251,
        ipc_port=50261,
    )

    controller.start()
    assert len(runner.running_pids) == 1

    assert controller.stop() is True
    status1 = controller.get_status()
    assert status1.desired_state == DesiredState.STOPPED_BY_USER.value
    assert len(runner.running_pids) == 0

    assert controller.stop() is True
    status2 = controller.get_status()
    assert status2.desired_state == DesiredState.STOPPED_BY_USER.value

    controller.shutdown()


def test_status_is_strictly_read_only(temp_state_file):
    runner = FakeProcessRunner()
    discovery = FakeDiscoveryService(peer_available=False)
    controller = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=FakeDeviceResolver(),
        pipeline_builder=FakeReceiverBuilder(),
        discovery_service=discovery,
        lock_port=50252,
        ipc_port=50262,
    )

    s1 = controller.get_status()
    assert s1.controller_state == LifecycleState.STOPPED.value
    assert len(runner.started_commands) == 0

    for _ in range(3):
        s = controller.get_status()
        assert s.controller_state == LifecycleState.STOPPED.value

    assert len(runner.started_commands) == 0
    assert discovery.started is False
    controller.shutdown()


def test_owned_child_only_cleanup(temp_state_file):
    runner = FakeProcessRunner()
    external_pid = 9999
    runner.running_pids.add(external_pid)

    controller = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=FakeDeviceResolver(),
        pipeline_builder=FakeReceiverBuilder(),
        discovery_service=FakeDiscoveryService(peer_available=True),
        lock_port=50253,
        ipc_port=50263,
    )

    controller.start()
    owned_pid = list(runner.running_pids - {external_pid})[0]
    controller.stop()

    assert owned_pid in runner.stopped_pids
    assert external_pid in runner.running_pids
    assert external_pid not in runner.stopped_pids
    controller.shutdown()


# ---------------------------------------------------------
# Test Suite 2: Process-Level Singleton & Lock Retention
# ---------------------------------------------------------

def test_singleton_lock_held_across_repeated_starts(temp_state_file):
    lock_port = 50254
    ipc_port = 50264
    controller = MacBridgeController(
        state_file=temp_state_file,
        process_runner=FakeProcessRunner(),
        device_resolver=FakeDeviceResolver(),
        pipeline_builder=FakeReceiverBuilder(),
        discovery_service=FakeDiscoveryService(peer_available=True),
        lock_port=lock_port,
        ipc_port=ipc_port,
    )

    assert controller.start() is True

    for _ in range(3):
        assert controller.start() is True

    second_ctrl = MacBridgeController(
        state_file=temp_state_file,
        process_runner=FakeProcessRunner(),
        device_resolver=FakeDeviceResolver(),
        pipeline_builder=FakeReceiverBuilder(),
        discovery_service=FakeDiscoveryService(peer_available=True),
        lock_port=lock_port,
        ipc_port=ipc_port + 1,
    )
    assert second_ctrl.start() is False, "Second controller process must fail to acquire singleton lock!"

    controller.shutdown()
    assert second_ctrl.start() is True
    second_ctrl.shutdown()


# ---------------------------------------------------------
# Test Suite 3: End-to-End Discovery Routing & Local Bind Injection
# ---------------------------------------------------------

class MockRouteResolver(RouteResolver):
    def __init__(self, routes: dict):
        self.routes = routes

    def resolve_local_route(self, target_ip: str, port: int) -> str:
        return self.routes.get(target_ip, "0.0.0.0")


def test_end_to_end_mac_discovery_routing_and_receiver_bind(temp_state_file):
    """Verifies Windows peer greeting -> Mac discovery -> route resolution -> Mac controller -> receiver builder bind."""
    mac_wifi_ip = "192.168.10.70"
    mac_direct_ip = "198.168.10.4"
    win_direct_peer_ip = "198.168.10.5"

    routes = {
        win_direct_peer_ip: mac_direct_ip,
        "192.168.10.1": mac_wifi_ip,
    }
    resolver = MockRouteResolver(routes)

    discovery = PeerDiscoveryService(
        local_role=HostRole.MACOS,
        instance_id="mac-disc-e2e",
        control_port=50271,
        route_resolver=resolver,
    )

    runner = FakeProcessRunner()
    builder = FakeReceiverBuilder()
    dev_resolver = FakeDeviceResolver()
    controller = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=dev_resolver,
        pipeline_builder=builder,
        discovery_service=discovery,
        lock_port=50273,
        ipc_port=50274,
    )
    controller.start()

    # Pass Windows peer greeting through handle_peer_message seam
    hello_msg = {
        "version": 1,
        "role": "windows",
        "instance_id": "win-pc-direct",
        "speaker_port": 5004,
    }
    discovery.handle_peer_message(hello_msg, win_direct_peer_ip)

    # 1. Assert discovery level
    assert discovery.peer_available is True
    assert discovery.peer_address == win_direct_peer_ip
    assert discovery.local_bind_address == mac_direct_ip

    # 2. Trigger reconcile & verify status
    controller.reconcile()
    status = controller.get_status()
    assert status.peer_available is True
    assert status.peer_address == win_direct_peer_ip
    assert status.local_bind_address == mac_direct_ip
    assert status.speaker_path_state == PathState.RUNNING.value

    # 3. Assert receiver builder received internal loopback bind and resolved device ID,
    # while SpeakerVolumeRelay intercepts on the dynamic Mac bind address
    assert builder.last_built_cmd is not None
    assert f"--bind=127.0.0.1" in builder.last_built_cmd
    assert f"--port=5005" in builder.last_built_cmd
    assert f"--device=88" in builder.last_built_cmd
    assert controller.voice_ducking.relay is not None
    assert controller.voice_ducking.relay.bind_ip == mac_direct_ip
    assert controller.voice_ducking.relay.listen_port == 5004
    assert controller.voice_ducking.relay.target_port == 5005

    controller.shutdown()


# ---------------------------------------------------------
# Test Suite 4: Peer Unavailable State
# ---------------------------------------------------------

def test_peer_unavailable_enters_discovering_without_receiver(temp_state_file):
    runner = FakeProcessRunner()
    builder = FakeReceiverBuilder()
    discovery = FakeDiscoveryService(peer_available=False)

    controller = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=FakeDeviceResolver(),
        pipeline_builder=builder,
        discovery_service=discovery,
        lock_port=50275,
        ipc_port=50276,
    )
    controller.start()

    status = controller.get_status()
    assert status.controller_state == LifecycleState.DISCOVERING.value
    assert status.speaker_path_state == PathState.IDLE.value
    assert len(runner.started_commands) == 0
    assert builder.last_built_cmd is None

    controller.shutdown()


# ---------------------------------------------------------
# Test Suite 5: Multiple-Responder Ambiguity
# ---------------------------------------------------------

def test_mac_multiple_responder_ambiguity_handling(temp_state_file):
    discovery = PeerDiscoveryService(
        local_role=HostRole.MACOS,
        instance_id="mac-ambig-test",
        control_port=50277,
    )
    runner = FakeProcessRunner()
    builder = FakeReceiverBuilder()
    controller = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=FakeDeviceResolver(),
        pipeline_builder=builder,
        discovery_service=discovery,
        lock_port=50278,
        ipc_port=50279,
    )
    controller.start()

    # Responder 1
    discovery.handle_peer_message(
        {"version": 1, "role": "windows", "instance_id": "win-1", "speaker_port": 5004},
        "198.168.10.5",
    )
    controller.reconcile()
    assert controller.get_status().speaker_path_state == PathState.RUNNING.value

    # Responder 2 arrives
    discovery.handle_peer_message(
        {"version": 1, "role": "windows", "instance_id": "win-2", "speaker_port": 5004},
        "198.168.10.99",
    )
    controller.reconcile()

    status = controller.get_status()
    assert status.controller_state == LifecycleState.AMBIGUOUS_PEER.value
    assert "Multiple opposite-role responders discovered" in status.last_actionable_error
    assert status.speaker_path_state == PathState.IDLE.value
    assert len(runner.running_pids) == 0

    controller.shutdown()


# ---------------------------------------------------------
# Test Suite 6: CoreAudio Built-in Speaker Resolution Failure
# ---------------------------------------------------------

def test_speaker_resolution_failure_reports_error_and_refuses_launch(temp_state_file):
    runner = FakeProcessRunner()
    builder = FakeReceiverBuilder()
    resolver = FakeDeviceResolver(fail=True)

    controller = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=resolver,
        pipeline_builder=builder,
        discovery_service=FakeDiscoveryService(peer_available=True),
        lock_port=50280,
        ipc_port=50281,
    )
    controller.start()
    controller.reconcile()

    status = controller.get_status()
    assert status.controller_state == LifecycleState.ERROR.value
    assert status.speaker_path_state == PathState.FAILED.value
    assert "CoreAudio built-in speaker output resolution failed" in status.last_actionable_error
    assert len(runner.started_commands) == 0

    controller.shutdown()


# ---------------------------------------------------------
# Test Suite 7: Cross-Platform Contract Alignment
# ---------------------------------------------------------

def test_shared_contract_constants():
    assert CONTROL_PROTOCOL_VERSION == 1
    assert DEFAULT_CONTROL_PORT == 50100
    assert DEFAULT_SPEAKER_RTP_PORT == 5004
    assert DEFAULT_SINGLETON_PORT == 50105
    assert DEFAULT_LOCAL_IPC_PORT == 50106


# ---------------------------------------------------------
# Test Suite 8: Cross-Process IPC Control on macOS
# ---------------------------------------------------------

def test_macos_cross_process_ipc_lifecycle(temp_state_file):
    ipc_port = 50282
    lock_port = 50283

    runner = FakeProcessRunner()
    controller = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=FakeDeviceResolver(),
        pipeline_builder=FakeReceiverBuilder(),
        discovery_service=FakeDiscoveryService(peer_available=True),
        lock_port=lock_port,
        ipc_port=ipc_port,
    )
    assert controller.start() is True

    res_status = send_ipc_command("status", port=ipc_port)
    assert res_status is not None
    assert res_status["desired_state"] == DesiredState.ENABLED.value
    assert res_status["role"] == HostRole.MACOS.value
    assert res_status["owned_children_count"] == 1

    res_start = send_ipc_command("start", port=ipc_port)
    assert res_start is not None
    assert res_start["success"] is True
    assert len(runner.started_commands) == 1

    res_stop = send_ipc_command("stop", port=ipc_port)
    assert res_stop is not None
    assert res_stop["success"] is True

    res_status2 = send_ipc_command("status", port=ipc_port)
    assert res_status2["desired_state"] == DesiredState.STOPPED_BY_USER.value
    assert res_status2["owned_children_count"] == 0

    controller.shutdown()


# ---------------------------------------------------------
# Test Suite 9: Strict Status Purity & Snapshot Consistency
# ---------------------------------------------------------

class InstrumentedRouteResolver(RouteResolver):
    def __init__(self, routes: dict):
        self.routes = routes
        self.call_count = 0

    def resolve_local_route(self, target_ip: str, port: int) -> str:
        self.call_count += 1
        return self.routes.get(target_ip, "0.0.0.0")


def test_strict_status_purity_and_zero_mutation(temp_state_file):
    """Verifies that calling get_status() repeatedly causes ZERO mutations or RouteResolver calls."""
    routes = {"198.168.10.5": "198.168.10.4", "198.168.10.99": "198.168.10.4"}
    resolver = InstrumentedRouteResolver(routes)
    discovery = PeerDiscoveryService(
        local_role=HostRole.MACOS,
        instance_id="mac-purity-test",
        control_port=50290,
        route_resolver=resolver,
    )

    runner = FakeProcessRunner()
    controller = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=FakeDeviceResolver(),
        pipeline_builder=FakeReceiverBuilder(),
        discovery_service=discovery,
        lock_port=50291,
        ipc_port=50292,
    )
    controller.start()

    # Create an initial discovery state with two responders (ambiguous)
    discovery.handle_peer_message(
        {"version": 1, "role": "windows", "instance_id": "win-1", "speaker_port": 5004},
        "198.168.10.5",
    )
    discovery.handle_peer_message(
        {"version": 1, "role": "windows", "instance_id": "win-2", "speaker_port": 5004},
        "198.168.10.99",
    )
    controller.reconcile()

    # Now simulate responder 2 having expired > 15s ago, but do NOT call refresh/reconcile yet
    now = time.time()
    discovery._known_responders["win-2"] = ("198.168.10.99", now - 25.0)
    discovery._known_responders["win-1"] = ("198.168.10.5", now)

    # Capture snapshot of mutable state
    responders_before = dict(discovery._known_responders)
    is_ambig_before = discovery._is_ambiguous
    peer_inst_before = discovery._peer_instance_id
    peer_addr_before = discovery._peer_address
    bind_addr_before = discovery._local_bind_address
    last_seen_before = discovery._last_peer_seen
    route_calls_before = resolver.call_count
    desired_before = controller._desired_state
    running_pids_before = set(runner.running_pids)

    # Call get_status() multiple times
    for _ in range(10):
        st = controller.get_status()
        # Verify status observation matches un-mutated ambiguous state
        assert st.controller_state == LifecycleState.AMBIGUOUS_PEER.value
        assert st.peer_available is False
        assert st.peer_address is None
        assert st.local_bind_address is None

    # Assert ZERO mutations occurred across all internal structures
    assert discovery._known_responders == responders_before
    assert discovery._is_ambiguous == is_ambig_before
    assert discovery._peer_instance_id == peer_inst_before
    assert discovery._peer_address == peer_addr_before
    assert discovery._local_bind_address == bind_addr_before
    assert discovery._last_peer_seen == last_seen_before
    assert resolver.call_count == route_calls_before, "RouteResolver MUST NOT be called during get_status()!"
    assert controller._desired_state == desired_before
    assert set(runner.running_pids) == running_pids_before

    controller.shutdown()


def test_snapshot_consistency_and_explicit_ambiguity_recovery(temp_state_file):
    """Verifies internal snapshot consistency and that ambiguity recovery only occurs via explicit reconcile."""
    routes = {"198.168.10.5": "198.168.10.4", "198.168.10.99": "198.168.10.4"}
    resolver = InstrumentedRouteResolver(routes)
    discovery = PeerDiscoveryService(
        local_role=HostRole.MACOS,
        instance_id="mac-snap-test",
        control_port=50293,
        route_resolver=resolver,
    )

    runner = FakeProcessRunner()
    builder = FakeReceiverBuilder()
    controller = MacBridgeController(
        state_file=temp_state_file,
        process_runner=runner,
        device_resolver=FakeDeviceResolver(),
        pipeline_builder=builder,
        discovery_service=discovery,
        lock_port=50294,
        ipc_port=50295,
    )
    controller.start()

    # Introduce ambiguity
    discovery.handle_peer_message(
        {"version": 1, "role": "windows", "instance_id": "win-1", "speaker_port": 5004},
        "198.168.10.5",
    )
    discovery.handle_peer_message(
        {"version": 1, "role": "windows", "instance_id": "win-2", "speaker_port": 5004},
        "198.168.10.99",
    )
    controller.reconcile()

    # Expire win-2
    now = time.time()
    discovery._known_responders["win-2"] = ("198.168.10.99", now - 30.0)

    # Status must strictly observe unrecovered ambiguous state
    s1 = controller.get_status()
    assert s1.controller_state == LifecycleState.AMBIGUOUS_PEER.value
    assert s1.peer_available is False
    assert s1.peer_address is None
    assert s1.local_bind_address is None
    initial_route_calls = resolver.call_count

    # Explicit reconcile advances discovery state
    controller.reconcile()

    # Now ambiguity is recovered to win-1
    s2 = controller.get_status()
    assert s2.controller_state == LifecycleState.ACTIVE.value
    assert s2.peer_available is True
    assert s2.peer_address == "198.168.10.5"
    assert s2.local_bind_address == "198.168.10.4"
    assert s2.speaker_path_state == PathState.RUNNING.value
    assert resolver.call_count == initial_route_calls + 1

    # Subsequent Status calls are completely pure and do not invoke resolver again
    for _ in range(5):
        s3 = controller.get_status()
        assert s3.peer_available is True
        assert s3.peer_address == "198.168.10.5"
    assert resolver.call_count == initial_route_calls + 1

    controller.shutdown()

