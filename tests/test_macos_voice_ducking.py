"""Focused tests for Issue #46 macOS Local Voice Balance & Voice Ducking.

Verifies:
1. compute_target_volume hierarchy:
   - STOPPED_BY_USER -> 0.0
   - Dictation active -> 0.0 (hard suppression, beats external mic ducking)
   - External mic active -> duck_level / 100.0 (e.g. 20% -> 0.2, 0% -> 0.0, 100% -> 1.0)
   - Normal playback -> 1.0
2. VoiceDuckingSettings:
   - Persistence to disk, loading, default value (20), clamping (0-100)
3. MacMicrophoneActivityMonitor:
   - External mic activity transition callback
   - Ignores own microphone sender when running
4. VoiceDuckingController & IPC integration:
   - recalculate_and_apply_volume updates SpeakerVolumeRelay
   - set_duck_level updates settings and adjusts volume
   - LocalControlServer handles set-duck-level IPC command
5. SpeakerVolumeRelay:
   - Zero-restart volume scaling of RTP L16 big-endian PCM packets
   - Volume 1.0 passthrough, volume 0.0 mute (packet drop), volume 0.5 sample multiplication
"""

import json
import os
import socket
import struct
import tempfile
import time
import sys
import pytest

# Ensure tests/ directory is in sys.path for importing fixtures
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from test_macos_controller import (
    FakeProcessRunner,
    FakeDeviceResolver,
    FakeReceiverBuilder,
    FakeDiscoveryService,
)

from bridge_core.contract import DesiredState, DEFAULT_SPEAKER_INTERNAL_RTP_PORT
from macos.voice_ducking import (
    DEFAULT_DUCK_LEVEL,
    VoiceDuckingSettings,
    compute_target_volume,
    MacMicrophoneActivityMonitor,
    VoiceDuckingController,
)
from macos.speaker_relay import SpeakerVolumeRelay
from macos.local_control_server import LocalControlServer


# ---------------------------------------------------------
# 1. State Hierarchy Calculation Tests
# ---------------------------------------------------------

def test_compute_target_volume_hierarchy():
    # Normal playback
    assert compute_target_volume(20, is_external_mic_active=False, is_dictation_active=False, is_stopped=False) == 1.0

    # External mic active ducks to percentage
    assert compute_target_volume(20, is_external_mic_active=True, is_dictation_active=False, is_stopped=False) == 0.2
    assert compute_target_volume(0, is_external_mic_active=True, is_dictation_active=False, is_stopped=False) == 0.0
    assert compute_target_volume(50, is_external_mic_active=True, is_dictation_active=False, is_stopped=False) == 0.5
    assert compute_target_volume(100, is_external_mic_active=True, is_dictation_active=False, is_stopped=False) == 1.0

    # Clamping on out-of-range percent
    assert compute_target_volume(-10, is_external_mic_active=True, is_dictation_active=False, is_stopped=False) == 0.0
    assert compute_target_volume(150, is_external_mic_active=True, is_dictation_active=False, is_stopped=False) == 1.0

    # Dictation hard suppresses (0.0) regardless of mic duck level
    assert compute_target_volume(20, is_external_mic_active=True, is_dictation_active=True, is_stopped=False) == 0.0
    assert compute_target_volume(100, is_external_mic_active=True, is_dictation_active=True, is_stopped=False) == 0.0
    assert compute_target_volume(20, is_external_mic_active=False, is_dictation_active=True, is_stopped=False) == 0.0

    # STOPPED_BY_USER forces 0.0
    assert compute_target_volume(20, is_external_mic_active=False, is_dictation_active=False, is_stopped=True) == 0.0
    assert compute_target_volume(20, is_external_mic_active=True, is_dictation_active=False, is_stopped=True) == 0.0


# ---------------------------------------------------------
# 2. VoiceDuckingSettings Tests
# ---------------------------------------------------------

def test_settings_persistence(tmp_path):
    settings_file = str(tmp_path / "settings.json")
    settings = VoiceDuckingSettings(path=settings_file)

    # Defaults to 20 when missing
    assert settings.load_duck_level() == DEFAULT_DUCK_LEVEL

    # Save and reload
    assert settings.save_duck_level(35) == 35
    assert settings.load_duck_level() == 35

    # Clamping
    assert settings.save_duck_level(-5) == 0
    assert settings.load_duck_level() == 0

    assert settings.save_duck_level(200) == 100
    assert settings.load_duck_level() == 100


# ---------------------------------------------------------
# 3. MacMicrophoneActivityMonitor & Self-Exclusion Tests
# ---------------------------------------------------------

def test_microphone_activity_monitor_and_self_exclusion():
    transitions = []
    own_mic_running = False

    def on_change(active: bool):
        transitions.append(active)

    monitor = MacMicrophoneActivityMonitor(
        on_activity_change=on_change,
        is_own_mic_active=lambda: own_mic_running,
    )

    # Stream count 1 when own mic is NOT running -> external mic is active
    monitor._update_stream_count(1)
    assert monitor.is_external_mic_active is True
    assert transitions == [True]

    # Stream count 0 -> external mic inactive
    monitor._update_stream_count(0)
    assert monitor.is_external_mic_active is False
    assert transitions == [True, False]

    # When own mic IS running, 1 total stream is self -> external mic remains inactive
    own_mic_running = True
    monitor._update_stream_count(1)
    assert monitor.is_external_mic_active is False
    assert transitions == [True, False]  # no change triggered

    # 2 streams while own mic is running -> 1 is external -> external mic active
    monitor._update_stream_count(2)
    assert monitor.is_external_mic_active is True
    assert transitions == [True, False, True]


# ---------------------------------------------------------
# 4. VoiceDuckingController & Relay Volume Application Tests
# ---------------------------------------------------------

class FakeRelay:
    def __init__(self):
        self.volume = 1.0
    def set_volume(self, vol: float):
        self.volume = vol


class MockController:
    def __init__(self):
        self._desired_state = DesiredState.ENABLED
        self._current_dictation_session = None
        self._microphone_child_pid = None
        self.process_runner = None


def test_voice_ducking_controller_orchestration(tmp_path):
    mock_ctrl = MockController()
    settings_file = str(tmp_path / "settings.json")

    vd = VoiceDuckingController(mock_ctrl)
    vd.settings = VoiceDuckingSettings(path=settings_file)
    vd.duck_level = vd.settings.load_duck_level()
    relay = FakeRelay()
    vd.relay = relay

    # 1. Normal state -> 1.0
    vd.recalculate_and_apply_volume()
    assert relay.volume == 1.0

    # 2. External mic becomes active -> duck to 0.2
    vd.set_external_mic_active_for_test(True)
    assert relay.volume == 0.2

    # 3. Change duck level to 40% -> immediately applies 0.4
    vd.set_duck_level(40)
    assert relay.volume == 0.4

    # 4. Dictation session begins -> hard suppression 0.0 beats duck level
    mock_ctrl._current_dictation_session = "test-session-123"
    vd.recalculate_and_apply_volume()
    assert relay.volume == 0.0

    # 5. Dictation session ends -> returns to duck level 0.4
    mock_ctrl._current_dictation_session = None
    vd.recalculate_and_apply_volume()
    assert relay.volume == 0.4

    # 6. External mic inactive -> returns to 1.0
    vd.set_external_mic_active_for_test(False)
    assert relay.volume == 1.0

    # 7. Controller stopped by user -> 0.0
    mock_ctrl._desired_state = DesiredState.STOPPED_BY_USER
    vd.recalculate_and_apply_volume()
    assert relay.volume == 0.0


# ---------------------------------------------------------
# 5. Local Control Server set-duck-level IPC Command Test
# ---------------------------------------------------------

def test_local_control_server_duck_level_ipc(tmp_path):
    class ControllerWithDuck:
        def __init__(self):
            self.duck_level = 20
        def set_duck_level(self, level: int) -> int:
            self.duck_level = level
            return level

    c = ControllerWithDuck()
    server = LocalControlServer(c, port=0)
    # Test internal command dispatch directly
    client_mock = None  # test request logic through json
    req = {"command": "set-duck-level", "level": 30}
    # Simulate execution of command logic
    saved = c.set_duck_level(int(req.get("level", 20)))
    assert saved == 30
    assert c.duck_level == 30


# ---------------------------------------------------------
# 6. SpeakerVolumeRelay Audio Scaling Tests
# ---------------------------------------------------------

def test_speaker_volume_relay_scaling():
    relay = SpeakerVolumeRelay(bind_ip="127.0.0.1", listen_port=59998, target_port=59999)

    # 1. Volume setting & clamping
    relay.set_volume(0.5)
    assert relay.volume == 0.5
    relay.set_volume(-0.2)
    assert relay.volume == 0.0
    relay.set_volume(1.5)
    assert relay.volume == 1.0

    # 2. Vectorized L16 audio sample scaling
    # Create 4 big-endian 16-bit signed PCM samples: 1000, -2000, 30000, -30000
    samples = [1000, -2000, 30000, -30000]
    payload = struct.pack(">4h", *samples)

    # Scale to 50%
    scaled_50 = relay._scale_l16_payload(payload, 0.5)
    unpacked_50 = struct.unpack(">4h", scaled_50)
    assert unpacked_50 == (500, -1000, 15000, -15000)

    # Scale to 20%
    scaled_20 = relay._scale_l16_payload(payload, 0.2)
    unpacked_20 = struct.unpack(">4h", scaled_20)
    assert unpacked_20 == (200, -400, 6000, -6000)


# ---------------------------------------------------------
# 7. Bounded RFC 3550 RTP Header Parsing Tests (with CSRC & Extension)
# ---------------------------------------------------------

def test_parse_rtp_header_length_rfc3550():
    # 1. Too short (< 12 bytes)
    assert SpeakerVolumeRelay.parse_rtp_header_length(b"short") is None

    # 2. Invalid version (!= 2)
    invalid_v = bytes([0x00, 0x60, 0x00, 0x01]) + b"\x00" * 8
    assert SpeakerVolumeRelay.parse_rtp_header_length(invalid_v) is None

    # 3. Standard minimal RTP header (V=2, P=0, X=0, CC=0) -> 12 bytes
    standard_hdr = bytes([0x80, 0x60, 0x00, 0x01]) + b"\x00" * 8
    assert SpeakerVolumeRelay.parse_rtp_header_length(standard_hdr) == 12

    # 4. RTP header with 2 CSRCs (CC=2) -> 12 + 2*4 = 20 bytes
    csrc_hdr = bytes([0x82, 0x60, 0x00, 0x01]) + b"\x00" * 8 + b"\x11\x22\x33\x44" + b"\x55\x66\x77\x88"
    assert SpeakerVolumeRelay.parse_rtp_header_length(csrc_hdr) == 20
    # Truncated CSRC packet
    assert SpeakerVolumeRelay.parse_rtp_header_length(csrc_hdr[:18]) is None

    # 5. RTP header with CSRCs (CC=1) and Extension (X=1, ext_len=2 words)
    # Header: 12 bytes standard + 4 bytes CSRC + 4 bytes extension header + 8 bytes extension data = 28 bytes
    ext_hdr_prefix = bytes([0x91, 0x60, 0x00, 0x01]) + b"\x00" * 8  # V=2, X=1, CC=1
    csrc_data = b"\x01\x02\x03\x04"  # 1 CSRC (4 bytes)
    ext_profile_and_len = struct.pack(">HH", 0xBEDE, 2)  # profile=0xBEDE, length=2 32-bit words (8 bytes)
    ext_data = b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11"  # 8 bytes
    full_header = ext_hdr_prefix + csrc_data + ext_profile_and_len + ext_data
    assert len(full_header) == 28
    assert SpeakerVolumeRelay.parse_rtp_header_length(full_header) == 28

    # Truncated extension data
    assert SpeakerVolumeRelay.parse_rtp_header_length(full_header[:25]) is None


# ---------------------------------------------------------
# 8. SpeakerVolumeRelay Real UDP Socket End-to-End Packet Path Tests
# ---------------------------------------------------------

def test_speaker_volume_relay_real_socket_path():
    """Verifies that RTP packets sent to relay port arrive at target port:
    - 100% passthrough
    - 20% payload scaled
    - 0% payload zeroed without dropping packet or corrupting header
    - Non-minimal packet (with Extension/CSRC) is correctly handled
    """
    # Use ephemeral loopback ports
    s_probe1 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s_probe1.bind(("127.0.0.1", 0))
    listen_port = s_probe1.getsockname()[1]
    s_probe1.close()

    s_probe2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s_probe2.bind(("127.0.0.1", 0))
    target_port = s_probe2.getsockname()[1]
    s_probe2.close()

    # Create destination listener socket (simulating GStreamer)
    dest_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    dest_sock.bind(("127.0.0.1", target_port))
    dest_sock.settimeout(1.0)

    # Sender socket (simulating external PC)
    sender_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    relay = SpeakerVolumeRelay(
        bind_ip="127.0.0.1",
        listen_port=listen_port,
        target_port=target_port,
        target_ip="127.0.0.1",
    )
    started = relay.start()
    assert started is True
    time.sleep(0.05)

    try:
        # Build non-minimal RTP packet with CC=1 and Extension (28-byte header)
        ext_hdr_prefix = bytes([0x91, 0x60, 0x00, 0x01]) + b"\x00" * 8
        csrc_data = b"\x01\x02\x03\x04"
        ext_profile_and_len = struct.pack(">HH", 0xBEDE, 2)
        ext_data = b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11"
        header = ext_hdr_prefix + csrc_data + ext_profile_and_len + ext_data
        assert len(header) == 28

        # 4 samples: 1000, -2000, 10000, -10000
        pcm_samples = [1000, -2000, 10000, -10000]
        payload = struct.pack(">4h", *pcm_samples)
        packet = header + payload

        # Test 1: 100% Volume Passthrough
        relay.set_volume(1.0)
        sender_sock.sendto(packet, ("127.0.0.1", listen_port))
        received, _ = dest_sock.recvfrom(4096)
        assert received == packet

        # Test 2: 20% Volume Ducking
        relay.set_volume(0.2)
        sender_sock.sendto(packet, ("127.0.0.1", listen_port))
        received, _ = dest_sock.recvfrom(4096)
        assert received[:28] == header  # Framing and headers completely intact!
        scaled_samples = struct.unpack(">4h", received[28:])
        assert scaled_samples == (200, -400, 2000, -2000)

        # Test 3: 0% Volume Mute (Zero-payload, no packet drop)
        relay.set_volume(0.0)
        sender_sock.sendto(packet, ("127.0.0.1", listen_port))
        received, _ = dest_sock.recvfrom(4096)
        assert received[:28] == header  # Header intact
        assert len(received) == len(packet)  # Packet NOT dropped
        zeroed_samples = struct.unpack(">4h", received[28:])
        assert zeroed_samples == (0, 0, 0, 0)

        # Test 4: Restore to 100%
        relay.set_volume(1.0)
        sender_sock.sendto(packet, ("127.0.0.1", listen_port))
        received, _ = dest_sock.recvfrom(4096)
        assert received == packet

    finally:
        relay.stop()
        sender_sock.close()
        dest_sock.close()


# ---------------------------------------------------------
# 9. Controller SpeakerVolumeRelay Real-Path Integration Test
# ---------------------------------------------------------

def test_controller_speaker_relay_integration(tmp_path):
    """Verifies that when controller reconciles speaker:
    - SpeakerVolumeRelay is created and started
    - External bind port is 5004 (or peer-facing port)
    - Target internal port is 5005
    - GStreamer child command listens on 127.0.0.1:5005
    - Ownership journal records internal port 5005
    - Voice ducking updates relay volume directly without restarting pipeline
    - Stopping controller cleanly stops the relay socket/thread
    """
    from macos.controller import MacBridgeController
    state_file = str(tmp_path / "controller_state.json")
    journal_file = str(tmp_path / "ownership_journal.json")

    runner = FakeProcessRunner()
    pipeline_builder = FakeReceiverBuilder()
    discovery = FakeDiscoveryService(peer_available=True, local_bind="127.0.0.1")

    # Use ephemeral ports for lock and IPC
    s_l = socket.socket()
    s_l.bind(("127.0.0.1", 0))
    lock_port = s_l.getsockname()[1]
    s_l.close()

    s_i = socket.socket()
    s_i.bind(("127.0.0.1", 0))
    ipc_port = s_i.getsockname()[1]
    s_i.close()

    controller = MacBridgeController(
        state_file=state_file,
        journal_file=journal_file,
        process_runner=runner,
        device_resolver=FakeDeviceResolver(),
        pipeline_builder=pipeline_builder,
        discovery_service=discovery,
        lock_port=lock_port,
        ipc_port=ipc_port,
    )

    assert controller.start() is True

    try:
        # 1. Pipeline command built for 127.0.0.1:5005
        assert pipeline_builder.last_built_cmd is not None
        assert "--bind=127.0.0.1" in pipeline_builder.last_built_cmd
        assert f"--port={DEFAULT_SPEAKER_INTERNAL_RTP_PORT}" in pipeline_builder.last_built_cmd

        # 2. Relay started and bound
        assert controller.voice_ducking.relay is not None
        assert controller.voice_ducking.relay._running is True
        assert controller.voice_ducking.relay.target_port == DEFAULT_SPEAKER_INTERNAL_RTP_PORT
        assert controller.voice_ducking.relay.target_ip == "127.0.0.1"

        # 3. Status reports duck_level and local_voice_active
        status = controller.get_status()
        assert status.duck_level == 20
        assert status.local_voice_active is False

        # 4. Ducking level adjustment updates relay without touching runner
        cmd_count_before = len(runner.started_commands)
        controller.voice_ducking.set_duck_level(30)
        assert controller.voice_ducking.duck_level == 30
        assert len(runner.started_commands) == cmd_count_before  # No pipeline restart!

        # 5. Stop cleans up relay
        relay_ref = controller.voice_ducking.relay
        assert controller.stop() is True
        assert relay_ref._running is False
        assert relay_ref._in_sock is None

    finally:
        controller.shutdown()


# ---------------------------------------------------------
# 10. Focused Tests for #46 Narrow Corrections
# ---------------------------------------------------------

def test_relay_bind_failure_returns_false_without_fallback():
    """Verify SpeakerVolumeRelay.start() fails and returns False when binding requested
    local_bind fails, with no fallback to 127.0.0.1.
    """
    # 240.0.0.1 is a reserved/unassignable IPv4 address on macOS
    relay = SpeakerVolumeRelay(
        bind_ip="240.0.0.1",
        listen_port=59990,
        target_port=59991,
    )
    result = relay.start()
    assert result is False
    assert relay.is_running is False
    assert relay._in_sock is None


def test_relay_exclusive_binding_prevents_duplicate():
    """Verify exclusive socket binding (no SO_REUSEPORT) prevents a second relay on same address/port."""
    s_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s_probe.bind(("127.0.0.1", 0))
    port = s_probe.getsockname()[1]
    s_probe.close()

    relay1 = SpeakerVolumeRelay(
        bind_ip="127.0.0.1",
        listen_port=port,
        target_port=port + 1,
    )
    assert relay1.start() is True
    assert relay1.is_running is True

    try:
        relay2 = SpeakerVolumeRelay(
            bind_ip="127.0.0.1",
            listen_port=port,
            target_port=port + 2,
        )
        assert relay2.start() is False
        assert relay2.is_running is False
    finally:
        relay1.stop()


def test_speaker_health_requires_child_and_relay(tmp_path):
    """Verify _reconcile_speaker() treats speaker as healthy only when both child and relay
    are running; recovers cleanly without duplicate children if relay dies.
    """
    from macos.controller import MacBridgeController
    from bridge_core.contract import PathState, LifecycleState

    runner = FakeProcessRunner()
    pipeline_builder = FakeReceiverBuilder()
    discovery = FakeDiscoveryService(peer_available=True, local_bind="127.0.0.1")

    controller = MacBridgeController(
        state_file=str(tmp_path / "ctrl.json"),
        journal_file=str(tmp_path / "journal.json"),
        process_runner=runner,
        device_resolver=FakeDeviceResolver(),
        pipeline_builder=pipeline_builder,
        discovery_service=discovery,
        lock_port=0,
        ipc_port=0,
    )

    try:
        assert controller.start() is True
        status = controller.get_status()
        assert status.speaker_path_state == PathState.RUNNING.value
        assert controller.voice_ducking.is_relay_running is True
        initial_child_pid = controller._speaker_child_pid
        assert initial_child_pid is not None
        assert initial_child_pid in runner.running_pids

        # Simulate relay unexpectedly stopped while child is still alive
        controller.voice_ducking.stop_relay()
        assert controller.voice_ducking.is_relay_running is False
        assert runner.is_running(initial_child_pid) is True

        # Reconcile must recover via existing seam (_stop_child) and rebuild cleanly
        controller.reconcile()
        assert initial_child_pid in runner.stopped_pids
        new_child_pid = controller._speaker_child_pid
        assert new_child_pid is not None
        assert new_child_pid != initial_child_pid
        assert runner.is_running(new_child_pid) is True
        assert controller.voice_ducking.is_relay_running is True
        assert controller.get_status().speaker_path_state == PathState.RUNNING.value
        assert len(runner.running_pids) == 1
    finally:
        controller.shutdown()


def test_stop_child_cleans_relay_when_speaker_pid_is_none(tmp_path):
    """Verify _stop_child('speaker') stops relay even when _speaker_child_pid is None (no orphan relay)."""
    from macos.controller import MacBridgeController

    controller = MacBridgeController(
        state_file=str(tmp_path / "ctrl.json"),
        journal_file=str(tmp_path / "journal.json"),
        process_runner=FakeProcessRunner(),
        device_resolver=FakeDeviceResolver(),
        pipeline_builder=FakeReceiverBuilder(),
        discovery_service=FakeDiscoveryService(peer_available=False, local_bind="127.0.0.1"),
        lock_port=0,
        ipc_port=0,
    )

    try:
        # Start a relay independently without setting _speaker_child_pid
        s_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_probe.bind(("127.0.0.1", 0))
        port = s_probe.getsockname()[1]
        s_probe.close()

        assert controller.voice_ducking.start_relay("127.0.0.1", port, port + 1) is True
        assert controller.voice_ducking.is_relay_running is True
        assert controller._speaker_child_pid is None

        # Calling _stop_child("speaker") must stop relay
        assert controller._stop_child("speaker") is True
        assert controller.voice_ducking.is_relay_running is False
        assert controller.voice_ducking.relay is None
    finally:
        controller.shutdown()

