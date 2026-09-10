"""Tests for Pack43 explicit demand recovery and negative cache behavior (Issue #43).

Verifies the preferred minimal design for stale negative cache recovery:
1. Stale negative recovers on explicit demand:
   - Initial probe fails -> negative cache (is_cached_available is False).
   - Device becomes available.
   - resolve_for_explicit_demand() performs fresh probe -> returns positive result.
   - Stale negative error does not persist.
2. Real missing Pack43 remains fail-closed:
   - Cached negative -> explicit demand -> fresh probe still None.
   - Dictation fails, mic child = 0, Mac sender not left running, speaker restored, actionable error retained.
3. Positive cache does not cause unnecessary re-enumeration:
   - When cache is positive, resolve_for_explicit_demand() reuses cached result without re-querying WMI.
4. Idle reconcile no hammering:
   - negative cache + no explicit demand -> multiple reconcile() calls do NOT query WMI.
5. Dictation coordinator integration with stale negative recovery:
   - start_dictation() triggers resolve_for_explicit_demand(), recovering when Pack43 becomes available.
"""

import json
import os
import tempfile
import time
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
from windows.controller import WindowsBridgeController
from windows.pack43_resolver import Pack43ResolutionResult, Pack43Resolver


class CountingPack43Resolver(Pack43Resolver):
    """Test subclass that intercepts _query_pack43 to track enumeration counts and mock returns."""

    def __init__(self, outcomes=None):
        super().__init__()
        self._outcomes = list(outcomes) if outcomes is not None else []
        self.query_count = 0

    def _query_pack43(self):
        self.query_count += 1
        if self._outcomes:
            return self._outcomes.pop(0)
        return None


class FakeProcessRunner:
    def __init__(self):
        self._next_pid = 70000
        self.running_pids = set()
        self.started_commands = []
        self.stopped_pids = []

    def start_process(self, cmd):
        pid = self._next_pid
        self._next_pid += 1
        self.running_pids.add(pid)
        self.started_commands.append(cmd)
        return pid

    def stop_process(self, pid):
        if pid in self.running_pids:
            self.running_pids.remove(pid)
            self.stopped_pids.append(pid)
        return True

    def is_running(self, pid):
        return pid in self.running_pids

    def get_child_metadata(self, pid):
        return {"pid": pid, "create_time": 1000.0} if pid in self.running_pids else None


class FakeDiscoveryService:
    def __init__(self):
        self.peer_available = True
        self.peer_address = "192.168.1.100"
        self.local_bind_address = "192.168.1.101"
        self.peer_speaker_port = DEFAULT_SPEAKER_RTP_PORT
        self.instance_id = "win-test-inst"
        self.sent_control_messages = []

    def send_control_message(self, peer_ip, msg):
        self.sent_control_messages.append({"peer_ip": peer_ip, "msg": msg})
        return True

    def start(self):
        pass

    def stop(self):
        pass


class FakePipelineBuilder:
    def is_gstreamer_available(self):
        return True

    def build_speaker_command(self, **kwargs):
        return ["mock-win-speaker-sender"]

    def build_sender_command(self, **kwargs):
        return ["mock-win-speaker-sender"]

    def build_receiver_command(self, **kwargs):
        return ["mock-mac-speaker-receiver"]


class FakeMicReceiverBuilder:
    def is_gstreamer_available(self):
        return True

    def build_receiver_command(self, local_bind_ip, local_port, device_id):
        return ["mock-gst-receiver", f"--port={local_port}", f"--device={device_id}"]


class FakeDeviceResolver:
    def resolve_default_playback_endpoint_id(self):
        return "{DEFAULT_SPEAKER}"


VALID_PACK43 = Pack43ResolutionResult(
    render_endpoint_id="{mock-pack43-render}",
    capture_endpoint_id="{mock-pack43-capture}",
    driver_version="1.0.3.5",
)


def test_resolver_stale_negative_recovers_on_explicit_demand():
    """Requirement 1: Stale negative cache must recover on resolve_for_explicit_demand()."""
    resolver = CountingPack43Resolver(outcomes=[None, VALID_PACK43])

    res1 = resolver.resolve_pack43()
    assert res1 is None
    assert resolver.query_count == 1
    assert resolver.is_cached_available is False

    res_cached = resolver.resolve_pack43()
    assert res_cached is None
    assert resolver.query_count == 1

    res_explicit = resolver.resolve_for_explicit_demand()
    assert res_explicit == VALID_PACK43
    assert resolver.query_count == 2
    assert resolver.is_cached_available is True

    res_cached_pos = resolver.resolve_pack43()
    assert res_cached_pos == VALID_PACK43
    assert resolver.query_count == 2


def test_resolver_real_missing_remains_fail_closed():
    """Requirement 2: If Pack43 remains missing on fresh probe, fail-closed is preserved."""
    resolver = CountingPack43Resolver(outcomes=[None, None])

    res1 = resolver.resolve_pack43()
    assert res1 is None
    assert resolver.query_count == 1
    assert resolver.is_cached_available is False

    res2 = resolver.resolve_for_explicit_demand()
    assert res2 is None
    assert resolver.query_count == 2
    assert resolver.is_cached_available is False


def test_resolver_positive_cache_reuses_without_re_query():
    """Requirement 3: Positive cache does NOT trigger re-enumeration on explicit demand."""
    resolver = CountingPack43Resolver(outcomes=[VALID_PACK43])

    res1 = resolver.resolve_pack43()
    assert res1 == VALID_PACK43
    assert resolver.query_count == 1
    assert resolver.is_cached_available is True

    res2 = resolver.resolve_for_explicit_demand()
    assert res2 == VALID_PACK43
    assert resolver.query_count == 1


def test_idle_reconcile_no_hammering():
    """Requirement 4: When negative cache exists, multiple reconcile() calls do NOT hammer WMI."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        temp_state = f.name

    try:
        resolver = CountingPack43Resolver(outcomes=[None])
        runner = FakeProcessRunner()
        disc = FakeDiscoveryService()
        ctrl = WindowsBridgeController(
            state_file=temp_state,
            process_runner=runner,
            device_resolver=FakeDeviceResolver(),
            discovery_service=disc,
            pack43_resolver=resolver,
            lock_port=54101,
            ipc_port=54102,
        )

        with patch("windows.controller.check_runtime_dependencies", return_value=(True, "")):
            ctrl.start()
            resolver.resolve_pack43()
            assert resolver.query_count == 1
            assert resolver.is_cached_available is False

            for _ in range(5):
                ctrl.reconcile()

            assert resolver.query_count == 1

            ctrl.shutdown()
    finally:
        if os.path.exists(temp_state):
            os.unlink(temp_state)


def test_dictation_coordinator_stale_negative_recovery():
    """End-to-end requirement: Start Dictation must recover from stale negative cache."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        temp_state = f.name

    try:
        resolver = CountingPack43Resolver(outcomes=[None, VALID_PACK43])
        runner = FakeProcessRunner()
        disc = FakeDiscoveryService()

        ctrl = WindowsBridgeController(
            state_file=temp_state,
            process_runner=runner,
            device_resolver=FakeDeviceResolver(),
            pipeline_builder=FakePipelineBuilder(),
            discovery_service=disc,
            pack43_resolver=resolver,
            microphone_receiver_builder=FakeMicReceiverBuilder(),
            lock_port=54103,
            ipc_port=54104,
        )

        with patch("windows.controller.check_runtime_dependencies", return_value=(True, "")):
            ctrl.start()

            resolver.resolve_pack43()
            assert resolver.is_cached_available is False
            assert resolver.query_count == 1

            def mock_send(peer_ip, msg):
                disc.sent_control_messages.append({"peer_ip": peer_ip, "msg": msg})
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

            ok = ctrl.start_dictation(timeout=2.0)
            assert ok is True

            assert resolver.query_count == 2
            assert resolver.is_cached_available is True

            st = ctrl.get_status()
            assert st.mode == "DICTATION"
            assert st.microphone_path_state == PathState.RUNNING.value
            assert ctrl._microphone_child_pid is not None
            assert ctrl._last_actionable_microphone_error != "Standard VB-CABLE Pack43 not found or driver identity mismatch"

            ctrl.shutdown()
    finally:
        if os.path.exists(temp_state):
            os.unlink(temp_state)


def test_dictation_coordinator_real_missing_pack43_fails_closed():
    """End-to-end requirement: Start Dictation fails closed when Pack43 genuinely missing."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        temp_state = f.name

    try:
        resolver = CountingPack43Resolver(outcomes=[None, None])
        runner = FakeProcessRunner()
        disc = FakeDiscoveryService()

        ctrl = WindowsBridgeController(
            state_file=temp_state,
            process_runner=runner,
            device_resolver=FakeDeviceResolver(),
            pipeline_builder=FakePipelineBuilder(),
            discovery_service=disc,
            pack43_resolver=resolver,
            microphone_receiver_builder=FakeMicReceiverBuilder(),
            lock_port=54105,
            ipc_port=54106,
        )

        with patch("windows.controller.check_runtime_dependencies", return_value=(True, "")):
            ctrl.start()

            resolver.resolve_pack43()
            assert resolver.is_cached_available is False

            ok = ctrl.start_dictation(timeout=1.0)
            assert ok is False

            st = ctrl.get_status()
            assert st.mode == "PLAYBACK"
            assert st.speaker_path_state == PathState.RUNNING.value
            assert st.microphone_path_state == PathState.UNAVAILABLE.value
            assert ctrl._microphone_child_pid is None
            assert len([m for m in disc.sent_control_messages if m["msg"]["type"] == "DICTATION_START"]) == 0
            assert ctrl._last_actionable_microphone_error == "Standard VB-CABLE Pack43 not found or driver identity mismatch"

            ctrl.shutdown()
    finally:
        if os.path.exists(temp_state):
            os.unlink(temp_state)
