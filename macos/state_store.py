"""Persistent desired state storage for macOS controller."""

from __future__ import annotations

import json
import logging
import os

from bridge_core.contract import DesiredState

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = os.environ.get(
    "DESK_AUDIO_BRIDGE_STATE_FILE",
    os.path.expanduser("~/Library/Application Support/desk-audio-bridge/controller_state.json"),
)


class ControllerStateStore:
    """Encapsulates loading and persisting DesiredState on disk."""

    def __init__(self, state_file: str = DEFAULT_STATE_FILE):
        self.state_file = state_file

    def load_desired_state(self) -> DesiredState:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    val = data.get("desired_state")
                    if val in (DesiredState.ENABLED.value, DesiredState.STOPPED_BY_USER.value):
                        return DesiredState(val)
            except Exception as exc:
                logger.debug("Could not read state file: %s", exc)
        return DesiredState.STOPPED_BY_USER

    def persist_desired_state(self, state: DesiredState) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"desired_state": state.value}, f)
        except Exception as exc:
            logger.warning("Could not persist desired state: %s", exc)
