"""Focused test contract for Issue #46: Local Audio Focus & Playback Ducking.

Verifies:
1. external mic only => duck
2. external local playback only => duck
3. mic + playback simultaneously => duck
4. one trigger disappears while the other remains => still ducked
5. both disappear => restore normal level
6. current owned speaker child output => does NOT self-trigger
7. speaker child ownership changes => exclusion follows current child
8. Dictation hard suppression overrides Local Audio Focus
9. Dictation exit + Local Audio Focus still active => configured duck level
10. STOPPED_BY_USER remains authoritative
11. 0% / normal configured level semantics remain correct
12. no ordinary duck/restore requires speaker-pipeline restart
"""

import os
import sys
import socket
import pytest

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from test_macos_controller import (
    FakeProcessRunner,
    FakeDeviceResolver,
    FakeReceiverBuilder,
    FakeDiscoveryService,
)

from bridge_core.contract import DesiredState, PathState
from macos.voice_ducking import (
    DEFAULT_DUCK_LEVEL,
    VoiceDuckingSettings,
    compute_target_volume,
    VoiceDuckingController,
)
from macos.playback_detector import MacPlaybackActivityDetector
from macos.controller import MacBridgeController


def test_1_external_mic_only_ducks():
    """Item 1: external mic only => duck to configured level."""
    # 20% duck level
    vol = compute_target_volume(
        duck_level_percent=20,
        is_external_mic_active=True,
        is_dictation_active=False,
        is_stopped=False,
        is_external_playback_active=False,
    )
    assert vol == 0.2


def test_2_external_local_playback_only_ducks():
    """Item 2: external local playback only => duck to configured level."""
    vol = compute_target_volume(
        duck_level_percent=30,
        is_external_mic_active=False,
        is_dictation_active=False,
        is_stopped=False,
        is_external_playback_active=True,
    )
    assert vol == 0.3


def test_3_mic_plus_playback_simultaneously():
    """Item 3: mic + playback simultaneously => duck."""
    vol = compute_target_volume(
        duck_level_percent=25,
        is_external_mic_active=True,
        is_dictation_active=False,
        is_stopped=False,
        is_external_playback_active=True,
    )
    assert vol == 0.25


def test_4_one_trigger_disappears_other_remains():
    """Item 4: one trigger disappears while the other remains => still ducked."""
    # Mic stops, playback remains
    vol1 = compute_target_volume(
        duck_level_percent=20,
        is_external_mic_active=False,
        is_dictation_active=False,
        is_stopped=False,
        is_external_playback_active=True,
    )
    assert vol1 == 0.2

    # Playback stops, mic remains
    vol2 = compute_target_volume(
        duck_level_percent=20,
        is_external_mic_active=True,
        is_dictation_active=False,
        is_stopped=False,
        is_external_playback_active=False,
    )
    assert vol2 == 0.2


def test_5_both_disappear_restores_normal():
    """Item 5: both disappear => restore normal level (1.0)."""
    vol = compute_target_volume(
        duck_level_percent=20,
        is_external_mic_active=False,
        is_dictation_active=False,
        is_stopped=False,
        is_external_playback_active=False,
    )
    assert vol == 1.0


def test_6_current_owned_speaker_child_does_not_self_trigger():
    """Item 6: current owned speaker child output => does NOT self-trigger."""
    current_speaker_pid = 52853
    detector = MacPlaybackActivityDetector(
        get_current_speaker_pid=lambda: current_speaker_pid,
    )

    # Mock scan returning only current speaker child
    detector.scan_active_output_pids = lambda: [current_speaker_pid]

    # Check activity
    is_active = detector.check_activity_once()
    assert is_active is False
    assert detector.is_external_playback_active is False


def test_7_speaker_child_ownership_changes_exclusion_follows():
    """Item 7: speaker child ownership changes => exclusion follows current child."""
    active_speaker_pid = 52853
    detector = MacPlaybackActivityDetector(
        get_current_speaker_pid=lambda: active_speaker_pid,
    )

    # Both old PID and another process outputting
    detector.scan_active_output_pids = lambda: [active_speaker_pid]
    assert detector.check_activity_once() is False

    # Speaker child replaced with new PID 60000
    active_speaker_pid = 60000
    detector.scan_active_output_pids = lambda: [60000]
    # Old PID 52853 is no longer owned, new PID 60000 is excluded
    assert detector.check_activity_once() is False

    # External process 77777 starts outputting alongside owned speaker child 60000
    detector.scan_active_output_pids = lambda: [60000, 77777]
    assert detector.check_activity_once() is True
    assert detector.is_external_playback_active is True


def test_8_dictation_hard_suppression_overrides_local_audio_focus():
    """Item 8: Dictation hard suppression overrides Local Audio Focus (0.0)."""
    vol = compute_target_volume(
        duck_level_percent=40,
        is_external_mic_active=True,
        is_dictation_active=True,
        is_stopped=False,
        is_external_playback_active=True,
    )
    assert vol == 0.0


def test_9_dictation_exit_with_focus_still_active_restores_duck_level():
    """Item 9: Dictation exit + Local Audio Focus still active => configured duck level."""
    # Dictation ends (is_dictation_active=False), but external playback is still playing
    vol = compute_target_volume(
        duck_level_percent=40,
        is_external_mic_active=False,
        is_dictation_active=False,
        is_stopped=False,
        is_external_playback_active=True,
    )
    assert vol == 0.4


def test_10_stopped_by_user_remains_authoritative():
    """Item 10: STOPPED_BY_USER remains authoritative (0.0)."""
    vol = compute_target_volume(
        duck_level_percent=50,
        is_external_mic_active=True,
        is_dictation_active=False,
        is_stopped=True,
        is_external_playback_active=True,
    )
    assert vol == 0.0


def test_11_zero_percent_and_normal_configured_level_semantics():
    """Item 11: 0% / normal configured level semantics remain correct."""
    # 0% duck level => full mute during focus
    vol_0 = compute_target_volume(
        duck_level_percent=0,
        is_external_mic_active=False,
        is_dictation_active=False,
        is_stopped=False,
        is_external_playback_active=True,
    )
    assert vol_0 == 0.0

    # 100% duck level => passthrough
    vol_100 = compute_target_volume(
        duck_level_percent=100,
        is_external_mic_active=False,
        is_dictation_active=False,
        is_stopped=False,
        is_external_playback_active=True,
    )
    assert vol_100 == 1.0


def test_12_controller_duck_and_restore_requires_no_pipeline_restart(tmp_path):
    """Item 12: no ordinary duck/restore requires speaker-pipeline restart."""
    runner = FakeProcessRunner()
    controller = MacBridgeController(
        state_file=str(tmp_path / "ctrl.json"),
        journal_file=str(tmp_path / "journal.json"),
        process_runner=runner,
        device_resolver=FakeDeviceResolver(),
        pipeline_builder=FakeReceiverBuilder(),
        discovery_service=FakeDiscoveryService(peer_available=True, local_bind="127.0.0.1"),
        lock_port=0,
        ipc_port=0,
    )

    try:
        assert controller.start() is True
        status = controller.get_status()
        assert status.speaker_path_state == PathState.RUNNING.value
        initial_spk_pid = controller._speaker_child_pid
        assert initial_spk_pid is not None

        # 1. Normal state
        assert controller.voice_ducking.is_local_audio_focus_active is False
        assert controller.voice_ducking.relay.volume == 1.0

        # 2. Local playback starts (external)
        controller.voice_ducking.set_external_playback_active_for_test(True)
        assert controller.voice_ducking.is_local_audio_focus_active is True
        assert controller.voice_ducking.relay.volume == 0.2
        # Pipeline PID unchanged!
        assert controller._speaker_child_pid == initial_spk_pid
        assert initial_spk_pid not in runner.stopped_pids

        # 3. Local playback stops
        controller.voice_ducking.set_external_playback_active_for_test(False)
        assert controller.voice_ducking.is_local_audio_focus_active is False
        assert controller.voice_ducking.relay.volume == 1.0
        # Pipeline PID still unchanged!
        assert controller._speaker_child_pid == initial_spk_pid
        assert initial_spk_pid not in runner.stopped_pids
    finally:
        controller.shutdown()
