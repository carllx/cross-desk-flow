"""Windows Dictation Session Coordinator.

Extracts Dictation lifecycle orchestration, state machine transitions,
Pack43 validation, and port 50100 DICTATION protocol exchanges from WindowsBridgeController.
"""

import logging
import threading
import time
import uuid
from typing import Any, Optional

from bridge_core.contract import (
    DEFAULT_MIC_RTP_PORT,
    DesiredState,
    HostRole,
    PathState,
)

logger = logging.getLogger(__name__)


class WindowsDictationCoordinator:
    """Coordinates Windows-side dictation sessions and mode transitions."""

    def __init__(self, controller: Any):
        self.controller = controller
        self.mode: str = "PLAYBACK"
        self.current_dictation_session: Optional[str] = None
        self._dictation_ack_event = threading.Event()
        self._last_dictation_ack: Optional[dict] = None

    def handle_control_message(self, msg: dict, peer_ip: str) -> None:
        """Handles incoming DICTATION_* control packets from elected peer."""
        msg_type = msg.get("type")
        if msg_type == "DICTATION_START_ACK":
            session_id = msg.get("session_id")
            if session_id and session_id == self.current_dictation_session:
                self._last_dictation_ack = msg
                self._dictation_ack_event.set()

    def start_dictation(self, timeout: float = 3.0) -> bool:
        """Transitions from PLAYBACK to DICTATION mode following strict ordering:
        PLAYBACK
        ↓
        suppress Windows → Mac speaker
        ↓
        resolve Pack43
        ↓
        START WINDOWS MICROPHONE RECEIVER
        ↓
        confirm receiver child is actually running/listening
        ↓
        generate fresh dictation session_id
        ↓
        send DICTATION_START(session_id) to Mac via authoritative 50100 control plane
        ↓
        Mac starts a fresh microphone sender
        ↓
        Mac replies DICTATION_START_ACK(session_id, success)
        ↓
        only after matching ACK:
        mode = DICTATION
        microphone = Active
        """
        c = self.controller
        with c._lock:
            if c._desired_state != DesiredState.ENABLED:
                logger.warning("Cannot start dictation: controller is not ENABLED")
                return False

            if not c.discovery_service.peer_available or not c.discovery_service.peer_address:
                logger.warning("Cannot start dictation: Mac peer is not available")
                return False

            # Duplicate Start Dictation check: idempotent
            if (
                self.mode == "DICTATION"
                and c._microphone_child_pid is not None
                and c.process_runner.is_running(c._microphone_child_pid)
            ):
                return True

            # 1. Suppress Windows → Mac speaker
            if c._speaker_child_pid is not None:
                c.process_runner.stop_process(c._speaker_child_pid)
                c._speaker_child_pid = None
            c._speaker_path_state = PathState.STOPPED

            # 2. Resolve Pack43 with explicit-demand recovery
            pack43_result = c.pack43_resolver.resolve_for_explicit_demand()
            if not pack43_result:
                c._last_actionable_microphone_error = (
                    "Standard VB-CABLE Pack43 not found or driver identity mismatch"
                )
                c._microphone_path_state = PathState.UNAVAILABLE
                # Restore speaker and abort without opening Mac microphone
                self.mode = "PLAYBACK"
                c._microphone_desired = False
                c._reconcile_speaker()
                return False

            # Ensure any prior microphone child is cleanly terminated
            if c._microphone_child_pid is not None:
                c.process_runner.stop_process(c._microphone_child_pid)
                c._microphone_child_pid = None

            # 3. START WINDOWS MICROPHONE RECEIVER
            local_bind = c.discovery_service.local_bind_address
            cmd = c.microphone_receiver_builder.build_receiver_command(
                local_bind_ip=local_bind,
                local_port=DEFAULT_MIC_RTP_PORT,
                device_id=pack43_result.render_endpoint_id,
            )

            try:
                pid = c.process_runner.start_process(cmd)
                c._microphone_child_pid = pid
            except Exception as exc:
                c._last_actionable_microphone_error = f"Failed to start microphone receiver: {exc}"
                c._microphone_path_state = PathState.FAILED
                self.mode = "PLAYBACK"
                c._microphone_desired = False
                c._reconcile_speaker()
                return False

            # 4. Confirm receiver child is actually running/listening
            if not c.process_runner.is_running(c._microphone_child_pid):
                c.process_runner.stop_process(c._microphone_child_pid)
                c._microphone_child_pid = None
                c._last_actionable_microphone_error = "Microphone receiver child exited immediately"
                c._microphone_path_state = PathState.FAILED
                self.mode = "PLAYBACK"
                c._microphone_desired = False
                c._reconcile_speaker()
                return False

            # 5. Generate fresh dictation session_id
            session_id = f"dict-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
            self.current_dictation_session = session_id
            self._dictation_ack_event.clear()
            self._last_dictation_ack = None

            # 6. Send DICTATION_START(session_id) to Mac via authoritative 50100 control plane
            target_ip = c.discovery_service.peer_address
            start_msg = {
                "version": 1,
                "role": HostRole.WINDOWS.value,
                "instance_id": c.discovery_service.instance_id,
                "type": "DICTATION_START",
                "session_id": session_id,
            }
            sent = c.discovery_service.send_control_message(target_ip, start_msg)
            if not sent:
                self._cleanup_failed_dictation(session_id, "Failed to send DICTATION_START to Mac")
                return False

        # 7. Wait for matching ACK from Mac
        got_ack = self._dictation_ack_event.wait(timeout=timeout)

        with c._lock:
            if self.current_dictation_session != session_id:
                logger.warning("Dictation session changed while waiting for ACK")
                return False

            ack = self._last_dictation_ack
            if not got_ack or not ack or not ack.get("success"):
                err_msg = ack.get("error") if ack else "Timeout waiting for Mac to start microphone"
                self._cleanup_failed_dictation(session_id, err_msg)
                return False

            # 8. Only after matching ACK:
            # mode = DICTATION
            # microphone = Active
            self.mode = "DICTATION"
            c._microphone_desired = True
            c._microphone_path_state = PathState.RUNNING
            c._last_actionable_microphone_error = None
            return True

    def _cleanup_failed_dictation(self, session_id: str, error_msg: Optional[str] = None) -> None:
        """Rolls back failed dictation: stops Windows receiver, ensures Mac mic stopped, restores speaker."""
        c = self.controller
        if error_msg:
            c._last_actionable_microphone_error = error_msg
        if c._microphone_child_pid is not None:
            c.process_runner.stop_process(c._microphone_child_pid)
            c._microphone_child_pid = None

        target_ip = c.discovery_service.peer_address
        if target_ip and session_id:
            stop_msg = {
                "version": 1,
                "role": HostRole.WINDOWS.value,
                "instance_id": c.discovery_service.instance_id,
                "type": "DICTATION_STOP",
                "session_id": session_id,
            }
            c.discovery_service.send_control_message(target_ip, stop_msg)

        c._microphone_desired = False
        self.current_dictation_session = None
        c._microphone_path_state = (
            PathState.UNAVAILABLE
            if c.pack43_resolver.is_cached_available is False
            else PathState.IDLE
        )
        self.mode = "PLAYBACK"
        c._reconcile_speaker()

    def end_dictation(self) -> bool:
        """Exits DICTATION mode and returns to PLAYBACK:
        1. stop Mac microphone sender (via 50100 DICTATION_STOP)
        2. stop Windows microphone receiver
        3. microphone children must be 0
        4. restore Windows → Mac speaker
        5. return to PLAYBACK
        """
        c = self.controller
        with c._lock:
            session_id = self.current_dictation_session
            target_ip = c.discovery_service.peer_address

            # 1. Stop Mac microphone sender
            if target_ip and session_id:
                stop_msg = {
                    "version": 1,
                    "role": HostRole.WINDOWS.value,
                    "instance_id": c.discovery_service.instance_id,
                    "type": "DICTATION_STOP",
                    "session_id": session_id,
                }
                c.discovery_service.send_control_message(target_ip, stop_msg)

            # 2. Stop Windows microphone receiver
            if c._microphone_child_pid is not None:
                c.process_runner.stop_process(c._microphone_child_pid)
                c._microphone_child_pid = None

            # 3. Microphone children must be 0
            c._microphone_desired = False
            self.current_dictation_session = None
            c._microphone_path_state = (
                PathState.READY if c.pack43_resolver.is_cached_available is True else PathState.IDLE
            )

            # 4. Restore Windows → Mac speaker & return to PLAYBACK
            self.mode = "PLAYBACK"
            c._reconcile_speaker()
            return True

    def reset(self) -> None:
        """Resets dictation state on controller stop/start/shutdown."""
        c = self.controller
        if c._microphone_child_pid is not None:
            c.process_runner.stop_process(c._microphone_child_pid)
            c._microphone_child_pid = None
        c._microphone_desired = False
        self.mode = "PLAYBACK"
        self.current_dictation_session = None
        self._dictation_ack_event.clear()
        self._last_dictation_ack = None
