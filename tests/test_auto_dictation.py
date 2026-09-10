"""Automated test suite for Issue #22: Automatic Dictation Mode.

Verifies:
1. Inactive -> Active edge (0 -> 1) triggers controller.start_dictation() exactly once.
2. Stable active (1 -> 1) is idempotent and does not repeatedly call start_dictation().
3. Active -> Inactive edge (1 -> 0) respects end grace debounce:
   - Does not end dictation immediately.
   - Calls controller.end_dictation() exactly once after grace period.
4. Stable inactive (0 -> 0) does not repeatedly call end_dictation().
5. STOPPED_BY_USER contract:
   - When desired_state is STOPPED_BY_USER, capture activity MUST NOT auto-start Dictation.
6. Peer availability guard:
   - If peer is not available, capture activity does not start dictation.
   - If peer is lost during active auto dictation, dictation is cleanly ended.
7. Start failure recovery & cooldown:
   - When start_dictation fails (e.g. ACK timeout), monitor backs off and does not storm calls.
8. Monitor thread lifecycle:
   - Idempotent start and stop, clean thread termination without leaking resources.
9. Product Shell UI state mapping:
   - Normal (Playback): Speaker Active, Mic Standby, Voice input Automatic / Standby.
   - Dictation (Voice Input): Speaker Paused for voice input, Mic Active, Voice input Active.
   - Stopped: Speaker Stopped, Mic Stopped, Voice input Standby / Off.
"""

import threading
import time
from unittest.mock import MagicMock, patch
import pytest

from bridge_core.contract import DesiredState, PathState
from windows.capture_detector import (
    WasapiCaptureSessionDetector,
    WindowsMicrophoneDemandMonitor,
)
from windows.pack43_resolver import Pack43ResolutionResult
from windows.product_shell import (
    DIRECTION_ACTIVE,
    DIRECTION_PAUSED_VOICE,
    DIRECTION_STANDBY,
    DIRECTION_STOPPED,
    MODE_DICTATION,
    MODE_PLAYBACK,
    OVERALL_CONNECTED,
    OVERALL_STOPPED,
    VOICE_INPUT_ACTIVE,
    VOICE_INPUT_OFF,
    VOICE_INPUT_STANDBY,
    map_ui_state,
)


class MockController:
    """Lightweight test double for WindowsBridgeController."""

    def __init__(self):
        self._desired_state = DesiredState.ENABLED
        self.is_shutdown_requested = False

        # Discovery mock
        self.discovery_service = MagicMock()
        self.discovery_service.peer_available = True
        self.discovery_service.peer_address = "192.168.1.50"

        # Pack43 mock
        self.pack43_resolver = MagicMock()
        self.pack43_resolver.resolve_pack43.return_value = Pack43ResolutionResult(
            render_endpoint_id="{mock-render}",
            capture_endpoint_id="{mock-capture}",
            driver_version="1.0.3.5",
        )

        # Dictation coordinator mock
        self.dictation_coordinator = MagicMock()
        self.dictation_coordinator.mode = "PLAYBACK"

        # Call counters
        self.start_dictation_calls = 0
        self.end_dictation_calls = 0
        self.should_start_succeed = True

    def start_dictation(self, timeout: float = 3.0) -> bool:
        self.start_dictation_calls += 1
        if self.should_start_succeed:
            self.dictation_coordinator.mode = "DICTATION"
            return True
        return False

    def end_dictation(self) -> bool:
        self.end_dictation_calls += 1
        self.dictation_coordinator.mode = "PLAYBACK"
        return True


def test_edge_inactive_to_active_starts_dictation():
    controller = MockController()
    active_flag = False
    detector = WasapiCaptureSessionDetector(query_func=lambda ep: active_flag)
    monitor = WindowsMicrophoneDemandMonitor(
        controller=controller,
        detector=detector,
        poll_interval=0.01,
        end_grace_seconds=0.2,
    )

    # Initial state: inactive
    monitor.check_demand_step()
    assert controller.start_dictation_calls == 0
    assert not monitor.is_auto_dictation_active

    # Transition 0 -> 1
    active_flag = True
    monitor.check_demand_step()
    assert controller.start_dictation_calls == 1
    assert monitor.is_auto_dictation_active
    assert controller.dictation_coordinator.mode == "DICTATION"


def test_stable_active_does_not_duplicate_start():
    controller = MockController()
    detector = WasapiCaptureSessionDetector(query_func=lambda ep: True)
    monitor = WindowsMicrophoneDemandMonitor(
        controller=controller,
        detector=detector,
        poll_interval=0.01,
        end_grace_seconds=0.2,
    )

    # First active step: starts dictation
    monitor.check_demand_step()
    assert controller.start_dictation_calls == 1

    # Second and third active steps: idempotent
    monitor.check_demand_step()
    monitor.check_demand_step()
    assert controller.start_dictation_calls == 1


def test_edge_active_to_inactive_respects_end_grace():
    controller = MockController()
    active_flag = True
    detector = WasapiCaptureSessionDetector(query_func=lambda ep: active_flag)
    monitor = WindowsMicrophoneDemandMonitor(
        controller=controller,
        detector=detector,
        poll_interval=0.01,
        end_grace_seconds=0.1,  # 100ms grace
    )

    # 1. Start dictation on active
    monitor.check_demand_step()
    assert controller.start_dictation_calls == 1

    # 2. Activity drops to 0 (speech pause)
    active_flag = False
    monitor.check_demand_step()
    # Immediately after drop: grace period NOT elapsed yet
    assert controller.end_dictation_calls == 0
    assert monitor.is_auto_dictation_active

    # 3. Wait until grace period elapses
    time.sleep(0.12)
    monitor.check_demand_step()
    assert controller.end_dictation_calls == 1
    assert not monitor.is_auto_dictation_active
    assert controller.dictation_coordinator.mode == "PLAYBACK"


def test_stable_inactive_does_not_duplicate_end():
    controller = MockController()
    active_flag = True
    detector = WasapiCaptureSessionDetector(query_func=lambda ep: active_flag)
    monitor = WindowsMicrophoneDemandMonitor(
        controller=controller,
        detector=detector,
        poll_interval=0.01,
        end_grace_seconds=0.05,
    )

    # Trigger start then end
    monitor.check_demand_step()
    active_flag = False
    time.sleep(0.06)
    monitor.check_demand_step()
    assert controller.end_dictation_calls == 1

    # Continued inactive steps: must not call end_dictation again
    monitor.check_demand_step()
    monitor.check_demand_step()
    assert controller.end_dictation_calls == 1


def test_stopped_by_user_blocks_auto_dictation():
    controller = MockController()
    controller._desired_state = DesiredState.STOPPED_BY_USER
    detector = WasapiCaptureSessionDetector(query_func=lambda ep: True)
    monitor = WindowsMicrophoneDemandMonitor(
        controller=controller,
        detector=detector,
    )

    # Active capture detected, but user explicitly stopped controller
    monitor.check_demand_step()
    assert controller.start_dictation_calls == 0
    assert not monitor.is_auto_dictation_active


def test_peer_unavailable_blocks_auto_dictation():
    controller = MockController()
    controller.discovery_service.peer_available = False
    detector = WasapiCaptureSessionDetector(query_func=lambda ep: True)
    monitor = WindowsMicrophoneDemandMonitor(
        controller=controller,
        detector=detector,
    )

    monitor.check_demand_step()
    assert controller.start_dictation_calls == 0


def test_peer_loss_during_dictation_ends_session():
    controller = MockController()
    active_flag = True
    detector = WasapiCaptureSessionDetector(query_func=lambda ep: active_flag)
    monitor = WindowsMicrophoneDemandMonitor(
        controller=controller,
        detector=detector,
    )

    # Start dictation
    monitor.check_demand_step()
    assert controller.start_dictation_calls == 1
    assert monitor.is_auto_dictation_active

    # Peer drops
    controller.discovery_service.peer_available = False
    monitor.check_demand_step()
    assert controller.end_dictation_calls == 1
    assert not monitor.is_auto_dictation_active


def test_start_dictation_failure_backs_off():
    controller = MockController()
    controller.should_start_succeed = False  # e.g. ACK timeout or error
    detector = WasapiCaptureSessionDetector(query_func=lambda ep: True)
    monitor = WindowsMicrophoneDemandMonitor(
        controller=controller,
        detector=detector,
    )

    # First attempt fails
    monitor.check_demand_step()
    assert controller.start_dictation_calls == 1
    assert not monitor.is_auto_dictation_active

    # Immediate next step during cooldown: must not spam start_dictation
    monitor.check_demand_step()
    assert controller.start_dictation_calls == 1


def test_demand_monitor_thread_lifecycle():
    controller = MockController()
    detector = WasapiCaptureSessionDetector(query_func=lambda ep: False)
    monitor = WindowsMicrophoneDemandMonitor(
        controller=controller,
        detector=detector,
        poll_interval=0.01,
    )

    assert not monitor.is_running

    # Start thread
    monitor.start()
    assert monitor.is_running

    # Duplicate start: idempotent
    monitor.start()
    assert monitor.is_running

    # Stop thread
    monitor.stop()
    assert not monitor.is_running

    # Duplicate stop: idempotent
    monitor.stop()
    assert not monitor.is_running


def test_product_shell_ui_mapping_automatic_dictation():
    # 1. Normal Playback State
    normal_status = {
        "controller_state": "ACTIVE",
        "desired_state": "ENABLED",
        "role": "windows",
        "peer_available": True,
        "speaker_path_state": "RUNNING",
        "microphone_path_state": "IDLE",
        "mode": "PLAYBACK",
    }
    normal_ui = map_ui_state(normal_status)
    assert normal_ui.overall == OVERALL_CONNECTED
    assert normal_ui.speaker == DIRECTION_ACTIVE
    assert normal_ui.microphone == DIRECTION_STANDBY
    assert normal_ui.voice_input == VOICE_INPUT_STANDBY

    # 2. Voice Input (Dictation) State
    voice_status = {
        "controller_state": "ACTIVE",
        "desired_state": "ENABLED",
        "role": "windows",
        "peer_available": True,
        "speaker_path_state": "STOPPED",
        "microphone_path_state": "RUNNING",
        "mode": "DICTATION",
    }
    voice_ui = map_ui_state(voice_status)
    assert voice_ui.overall == OVERALL_CONNECTED
    assert voice_ui.speaker == DIRECTION_PAUSED_VOICE
    assert voice_ui.microphone == DIRECTION_ACTIVE
    assert voice_ui.voice_input == VOICE_INPUT_ACTIVE

    # 3. Stopped State
    stopped_status = {
        "controller_state": "STOPPED",
        "desired_state": "STOPPED_BY_USER",
        "role": "windows",
        "peer_available": True,
        "speaker_path_state": "STOPPED",
        "microphone_path_state": "STOPPED",
        "mode": "PLAYBACK",
    }
    stopped_ui = map_ui_state(stopped_status)
    assert stopped_ui.overall == OVERALL_STOPPED
    assert stopped_ui.speaker == DIRECTION_STOPPED
    assert stopped_ui.microphone == DIRECTION_STOPPED
    assert stopped_ui.voice_input == VOICE_INPUT_OFF


def test_com_init_uninit_balance_s_ok():
    """Verify S_OK (0) causes exactly one CoUninitialize call."""
    uninit_calls = 0

    def fake_init():
        return 0  # S_OK

    def fake_uninit():
        nonlocal uninit_calls
        uninit_calls += 1

    detector = WasapiCaptureSessionDetector(
        co_init_func=fake_init,
        co_uninit_func=fake_uninit,
    )
    # Query an empty endpoint (will fail early in COM setup, but triggers init -> finally uninit)
    detector.is_capture_active("{mock-endpoint}")
    assert uninit_calls == 1


def test_com_init_uninit_balance_s_false():
    """Verify S_FALSE (1, already initialized on thread) causes exactly one CoUninitialize call."""
    uninit_calls = 0

    def fake_init():
        return 1  # S_FALSE

    def fake_uninit():
        nonlocal uninit_calls
        uninit_calls += 1

    detector = WasapiCaptureSessionDetector(
        co_init_func=fake_init,
        co_uninit_func=fake_uninit,
    )
    detector.is_capture_active("{mock-endpoint}")
    assert uninit_calls == 1


def test_com_init_uninit_balance_error_no_uninit():
    """Verify failed CoInitialize does NOT call CoUninitialize."""
    uninit_calls = 0

    def fake_init():
        return -2147467259  # E_FAIL

    def fake_uninit():
        nonlocal uninit_calls
        uninit_calls += 1

    detector = WasapiCaptureSessionDetector(
        co_init_func=fake_init,
        co_uninit_func=fake_uninit,
    )
    detector.is_capture_active("{mock-endpoint}")
    assert uninit_calls == 0


def test_monitor_stop_retains_thread_reference_on_timeout():
    """Verify demand_monitor.stop() does NOT set _thread = None if thread join times out."""
    controller = MockController()
    worker_started = threading.Event()
    block_event = threading.Event()

    class BlockingDetector(WasapiCaptureSessionDetector):
        def is_capture_active(self, endpoint_id: str) -> bool:
            worker_started.set()
            block_event.wait(timeout=2.0)
            return False

    monitor = WindowsMicrophoneDemandMonitor(
        controller=controller,
        detector=BlockingDetector(),
        poll_interval=0.01,
    )
    monitor.start()
    assert monitor.is_running
    assert worker_started.wait(timeout=2.0)

    # Call stop with very small timeout while worker is blocked
    stopped = monitor.stop(timeout=0.02)
    assert not stopped  # Timed out
    assert monitor._thread is not None  # Retained reference
    assert monitor._thread.is_alive()

    # Unblock worker and cleanly stop
    block_event.set()
    stopped_clean = monitor.stop(timeout=2.0)
    assert stopped_clean
    assert monitor._thread is None


def test_controller_stop_no_deadlock_with_monitor_worker():
    """Regression test: worker thread attempting to start_dictation while controller.stop() is called.
    
    Verifies that because demand_monitor.stop() is outside controller._lock,
    and stop() sets desired_state = STOPPED_BY_USER, there is no deadlock and
    both threads exit cleanly.
    """
    controller = MockController()
    worker_entered = threading.Event()
    release_worker = threading.Event()

    def slow_start_dictation(timeout=3.0):
        worker_entered.set()
        release_worker.wait(timeout=2.0)
        return True

    controller.start_dictation = slow_start_dictation
    detector = WasapiCaptureSessionDetector(query_func=lambda ep: True)
    monitor = WindowsMicrophoneDemandMonitor(
        controller=controller,
        detector=detector,
        poll_interval=0.01,
    )

    monitor.start()
    # Wait until worker has started dictation call
    assert worker_entered.wait(timeout=2.0)

    # Controller stop called from another thread
    stop_finished = threading.Event()

    def do_stop():
        controller._desired_state = DesiredState.STOPPED_BY_USER
        monitor.stop(timeout=2.0)
        stop_finished.set()

    t_stop = threading.Thread(target=do_stop)
    t_stop.start()

    # Release the worker
    release_worker.set()

    t_stop.join(timeout=3.0)
    assert not t_stop.is_alive()
    assert stop_finished.is_set()
    assert not monitor.is_running

