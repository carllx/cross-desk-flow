"""Port 50100 Dictation Control Protocol transport and dispatch logic.

Provides separation between discovery packets and dictation control packets
on the shared control port (50100).
"""

import json
import logging
import socket
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def is_dictation_message(msg: Any) -> bool:
    """Checks if message dictionary represents a DICTATION_* control packet."""
    if not isinstance(msg, dict):
        return False
    msg_type = msg.get("type", "")
    return isinstance(msg_type, str) and msg_type.startswith("DICTATION_")


def send_dictation_control_message(
    sock: Optional[socket.socket],
    peer_ip: str,
    port: int,
    message: dict,
) -> bool:
    """Encodes and sends a directed DICTATION control packet to the elected peer."""
    if not sock:
        return False
    data = json.dumps(message).encode("utf-8")
    try:
        sock.sendto(data, (peer_ip, port))
        return True
    except Exception as exc:
        logger.warning("Failed to send control message to %s:%d: %s", peer_ip, port, exc)
        return False


def dispatch_dictation_message(
    msg: dict,
    peer_ip: str,
    elected_peer_address: Optional[str],
    elected_peer_instance_id: Optional[str],
    is_ambiguous: bool,
    callback: Optional[Callable[[dict, str], None]],
) -> bool:
    """Validates and dispatches an incoming DICTATION message.

    Enforces peer identity and route verification:
    - Peer must not be ambiguous.
    - Peer must be elected.
    - Sender IP must match elected peer address.
    - Sender instance_id must match elected peer instance ID.
    Returns True if handled (whether accepted or dropped due to validation failure).
    """
    if is_ambiguous or not elected_peer_address or not elected_peer_instance_id:
        logger.debug("Dropping DICTATION message: peer ambiguous or not yet elected")
        return True

    if peer_ip != elected_peer_address:
        logger.warning(
            "Dropping DICTATION message from non-elected IP %s (elected %s)",
            peer_ip,
            elected_peer_address,
        )
        return True

    sender_inst = msg.get("instance_id")
    if sender_inst != elected_peer_instance_id:
        logger.warning(
            "Dropping DICTATION message from unverified instance %s (elected %s)",
            sender_inst,
            elected_peer_instance_id,
        )
        return True

    if callback:
        try:
            callback(msg, peer_ip)
        except Exception as exc:
            logger.warning("Error in on_control_message callback: %s", exc)
    return True


class DictationControlMixin:
    """Mixin providing Port 50100 DICTATION control plane capabilities to PeerDiscoveryService."""

    on_control_message: Optional[Callable[[dict, str], None]] = None

    def send_control_message(self, peer_ip: str, message: dict) -> bool:
        """Sends a directed DICTATION control packet to elected peer on port 50100."""
        with getattr(self, "_lock"):
            if not getattr(self, "_running", False) or peer_ip != getattr(self, "_peer_address", None):
                logger.warning(
                    "Cannot send control message: %s does not match elected peer %s",
                    peer_ip,
                    getattr(self, "_peer_address", None),
                )
                return False
            sock = getattr(self, "_listener_sock", None)
        return send_dictation_control_message(
            sock, peer_ip, getattr(self, "control_port", 50100), message
        )

    def handle_dictation_message(self, msg: Any, peer_ip: str) -> bool:
        """Branches and dispatches DICTATION control packets before discovery logic."""
        parsed_msg = msg
        if isinstance(msg, (bytes, bytearray)):
            try:
                parsed_msg = json.loads(msg.decode("utf-8"))
            except Exception:
                return False
        if not is_dictation_message(parsed_msg):
            return False
        with getattr(self, "_lock"):
            peer_addr = getattr(self, "_peer_address", None)
            peer_inst = getattr(self, "_peer_instance_id", None)
            ambiguous = getattr(self, "_is_ambiguous", False)
        dispatch_dictation_message(
            parsed_msg, peer_ip, peer_addr, peer_inst, ambiguous, self.on_control_message
        )
        return True
