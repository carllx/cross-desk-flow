"""macOS Dictation Responder.

Extracts DICTATION_* control packet handling and dictation session
response management from MacBridgeController.
"""

import logging
from typing import TYPE_CHECKING, Any, Optional

from bridge_core.contract import (
    CONTROL_PROTOCOL_VERSION,
    DesiredState,
    HostRole,
    PathState,
)

if TYPE_CHECKING:
    from .controller import MacBridgeController

logger = logging.getLogger(__name__)


class MacDictationResponder:
    """Handles incoming dictation control messages for macOS bridge controller."""

    def __init__(self, controller: Any):
        self.controller = controller
        self.current_dictation_session: Optional[str] = None

    def handle_control_message(self, msg: dict, peer_ip: str) -> None:
        """Handles incoming DICTATION_* control packets from verified elected peer."""
        c = self.controller
        msg_type = msg.get("type")
        session_id = msg.get("session_id")
        if not session_id:
            return

        with c._lock:
            if msg_type == "DICTATION_START":
                # Reject if stopped by user
                if c._desired_state == DesiredState.STOPPED_BY_USER:
                    c.discovery_service.send_control_message(
                        peer_ip,
                        {
                            "version": CONTROL_PROTOCOL_VERSION,
                            "role": HostRole.MACOS.value,
                            "instance_id": c.discovery_service.instance_id,
                            "type": "DICTATION_START_ACK",
                            "session_id": session_id,
                            "success": False,
                            "error": "macOS host is stopped by user",
                        },
                    )
                    return

                # If duplicate start with same session_id and mic already running: idempotent
                if (
                    self.current_dictation_session == session_id
                    and c._microphone_child_pid is not None
                    and c.process_runner.is_running(c._microphone_child_pid)
                ):
                    c.discovery_service.send_control_message(
                        peer_ip,
                        {
                            "version": CONTROL_PROTOCOL_VERSION,
                            "role": HostRole.MACOS.value,
                            "instance_id": c.discovery_service.instance_id,
                            "type": "DICTATION_START_ACK",
                            "session_id": session_id,
                            "success": True,
                            "state": PathState.RUNNING.value,
                        },
                    )
                    return

                # If new session: ensure any prior microphone child is cleanly terminated
                if c._microphone_child_pid is not None:
                    c._stop_child("microphone")
                    c._microphone_child_pid = None

                self.current_dictation_session = session_id
                ok = c.set_microphone_enabled(True)
                status = c.get_status()
                c.discovery_service.send_control_message(
                    peer_ip,
                    {
                        "version": CONTROL_PROTOCOL_VERSION,
                        "role": HostRole.MACOS.value,
                        "instance_id": c.discovery_service.instance_id,
                        "type": "DICTATION_START_ACK",
                        "session_id": session_id,
                        "success": ok,
                        "state": status.microphone_path_state,
                        "error": status.last_actionable_microphone_error,
                    },
                )

            elif msg_type == "DICTATION_STOP":
                c.set_microphone_enabled(False)
                self.current_dictation_session = None
                c.discovery_service.send_control_message(
                    peer_ip,
                    {
                        "version": CONTROL_PROTOCOL_VERSION,
                        "role": HostRole.MACOS.value,
                        "instance_id": c.discovery_service.instance_id,
                        "type": "DICTATION_STOP_ACK",
                        "session_id": session_id,
                        "success": True,
                    },
                )
