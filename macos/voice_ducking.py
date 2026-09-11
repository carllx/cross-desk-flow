"""macOS Local Voice Ducking & Activity Monitor.

Provides:
- Settings persistence for Mac microphone duck level (0-100%).
- Detection of external Mac microphone capture activity via Apple Unified Log.
- Filtering out Cross-Desk-Flow's own project microphone sender.
- Pure state machine for duck factor calculation (Normal, Ducked, Dictation-suppressed, Stopped).
- VoiceDuckingController to cleanly orchestrate monitor, relay, and volume state for MacBridgeController.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from bridge_core.contract import DesiredState

if TYPE_CHECKING:
    from .controller import MacBridgeController
    from .speaker_relay import SpeakerVolumeRelay

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS_PATH = os.path.expanduser(
    "~/Library/Application Support/desk-audio-bridge/settings.json"
)
DEFAULT_DUCK_LEVEL = 20  # 20% volume when Mac mic is active (0-100)


class VoiceDuckingSettings:
    """Manages persistent settings for macOS Local Voice ducking."""

    def __init__(self, path: str = DEFAULT_SETTINGS_PATH):
        self.path = path

    def load_duck_level(self) -> int:
        """Loads duck level percentage (0-100). Defaults to 20."""
        if not os.path.isfile(self.path):
            return DEFAULT_DUCK_LEVEL
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            val = data.get("duck_level", DEFAULT_DUCK_LEVEL)
            return max(0, min(100, int(val)))
        except Exception:
            return DEFAULT_DUCK_LEVEL

    def save_duck_level(self, level: int) -> int:
        """Persists duck level percentage (clamped to 0-100)."""
        clamped = max(0, min(100, int(level)))
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            data = {}
            if os.path.isfile(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            data["duck_level"] = clamped
            tmp = f"{self.path}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:
            logger.warning("Failed to save duck level settings: %s", exc)
        return clamped


def compute_target_volume(
    duck_level_percent: int,
    is_external_mic_active: bool,
    is_dictation_active: bool,
    is_stopped: bool,
) -> float:
    """Computes the target bridge volume multiplier (0.0 to 1.0).

    Hierarchy:
    1. STOPPED_BY_USER -> 0.0
    2. Dictation active -> 0.0 (hard suppression for Windows dictation)
    3. External mic active -> duck_level_percent / 100.0 (0.0 if 0%, 1.0 if 100%)
    4. Normal playback -> 1.0
    """
    if is_stopped or is_dictation_active:
        return 0.0
    if is_external_mic_active:
        clamped = max(0, min(100, duck_level_percent))
        return float(clamped) / 100.0
    return 1.0


class MacMicrophoneActivityMonitor:
    """Monitors system-wide microphone capture stream transitions via coreaudiod log stream.

    Distinguishes external app mic capture from Cross-Desk-Flow's own microphone sender.
    """

    def __init__(
        self,
        on_activity_change: Optional[Callable[[bool], None]] = None,
        is_own_mic_active: Optional[Callable[[], bool]] = None,
    ):
        self.on_activity_change = on_activity_change
        self.is_own_mic_active = is_own_mic_active or (lambda: False)

        self._active_stream_count = 0
        self._is_external_active = False
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    @property
    def is_external_mic_active(self) -> bool:
        with self._lock:
            return self._is_external_active

    def set_external_mic_active_for_test(self, active: bool) -> None:
        """Test helper to simulate external mic capture activity transitions."""
        changed = False
        with self._lock:
            if self._is_external_active != active:
                self._is_external_active = active
                changed = True
        if changed and self.on_activity_change:
            self.on_activity_change(active)

    def start(self) -> None:
        """Starts background log streaming monitor."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_stream,
            name="MacMicrophoneActivityMonitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stops background log streaming monitor."""
        self._stop_event.set()
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=0.5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        self._proc = None

    def _run_stream(self) -> None:
        cmd = [
            "log",
            "stream",
            "--process",
            "coreaudiod",
            "--predicate",
            'eventMessage CONTAINS "MicrophoneDSPDevice: startStream" || '
            'eventMessage CONTAINS "MicrophoneDSPDevice: stopStream" || '
            'eventMessage CONTAINS "Digital Mic: Thread context"',
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            logger.warning("Could not launch log stream for microphone monitoring: %s", exc)
            return

        while not self._stop_event.is_set() and self._proc:
            line = self._proc.stdout.readline()
            if not line:
                break

            self._process_log_line(line)

    def _process_log_line(self, line: str) -> None:
        """Parses a log line and updates active stream count."""
        is_start = False
        is_stop = False

        if "startStream: running state: 1" in line:
            is_start = True
        elif "stopStream: running state: 0" in line:
            is_stop = True
        elif "Digital Mic:" in line:
            if " start with use case" in line:
                m = re.search(r"count:\s*(\d+)", line)
                if m:
                    self._update_stream_count(int(m.group(1)))
                    return
            elif " stop with use case" in line:
                m = re.search(r"count:\s*(\d+)", line)
                if m:
                    self._update_stream_count(int(m.group(1)))
                    return

        if is_start:
            self._update_stream_count(self._active_stream_count + 1)
        elif is_stop:
            self._update_stream_count(max(0, self._active_stream_count - 1))

    def _update_stream_count(self, count: int) -> None:
        changed = False
        new_ext_state = False

        with self._lock:
            self._active_stream_count = max(0, count)
            own_active = self.is_own_mic_active()
            total_active = self._active_stream_count
            ext_count = max(0, total_active - (1 if own_active else 0))
            new_ext_state = ext_count > 0

            if new_ext_state != self._is_external_active:
                self._is_external_active = new_ext_state
                changed = True

        if changed and self.on_activity_change:
            try:
                self.on_activity_change(new_ext_state)
            except Exception as exc:
                logger.error("Error in on_activity_change callback: %s", exc)


class VoiceDuckingController:
    """Manages voice ducking settings, microphone activity monitoring, and volume relay integration."""

    def __init__(self, controller: Any):
        self.controller = controller
        self.settings = VoiceDuckingSettings()
        self.duck_level = self.settings.load_duck_level()
        self.monitor = MacMicrophoneActivityMonitor(
            on_activity_change=self._on_activity_changed,
            is_own_mic_active=self._is_own_mic_active,
        )
        self.relay: Optional[SpeakerVolumeRelay] = None

    def start_monitoring(self) -> None:
        """Starts background microphone activity monitoring."""
        self.monitor.start()

    def stop_monitoring(self) -> None:
        """Stops background microphone activity monitoring and stops relay."""
        self.monitor.stop()
        self.stop_relay()

    def set_duck_level(self, level: int) -> int:
        """Saves new duck level (0-100) and immediately recalculates volume."""
        saved = self.settings.save_duck_level(level)
        self.duck_level = saved
        self.recalculate_and_apply_volume()
        return saved

    def get_duck_level(self) -> int:
        return self.duck_level

    @property
    def is_external_mic_active(self) -> bool:
        return self.monitor.is_external_mic_active

    def set_external_mic_active_for_test(self, active: bool) -> None:
        self.monitor.set_external_mic_active_for_test(active)

    def _is_own_mic_active(self) -> bool:
        c = self.controller
        try:
            return bool(
                c._microphone_child_pid is not None
                and c.process_runner.is_running(c._microphone_child_pid)
            )
        except Exception:
            return False

    def _on_activity_changed(self, active: bool) -> None:
        self.recalculate_and_apply_volume()

    def recalculate_and_apply_volume(self) -> float:
        """Calculates target volume from state hierarchy and applies to relay atomically."""
        c = self.controller
        is_stopped = bool(c._desired_state == DesiredState.STOPPED_BY_USER)
        is_dictation = bool(c._current_dictation_session is not None)
        is_ext_mic = self.is_external_mic_active

        target_vol = compute_target_volume(
            duck_level_percent=self.duck_level,
            is_external_mic_active=is_ext_mic,
            is_dictation_active=is_dictation,
            is_stopped=is_stopped,
        )

        if self.relay:
            self.relay.set_volume(target_vol)
        return target_vol

    def start_relay(self, bind_ip: str, listen_port: int, target_port: int) -> bool:
        """Starts or reconfigures the speaker volume relay proxy."""
        from .speaker_relay import SpeakerVolumeRelay

        if self.relay:
            if (
                self.relay.bind_ip == bind_ip
                and self.relay.listen_port == listen_port
                and self.relay.target_port == target_port
                and self.relay._running
            ):
                self.recalculate_and_apply_volume()
                return True
            self.relay.stop()
            self.relay = None

        relay = SpeakerVolumeRelay(
            bind_ip=bind_ip,
            listen_port=listen_port,
            target_port=target_port,
        )
        if relay.start():
            self.relay = relay
            self.recalculate_and_apply_volume()
            return True
        return False

    def stop_relay(self) -> None:
        """Stops the speaker volume relay proxy."""
        if self.relay:
            try:
                self.relay.stop()
            except Exception:
                pass
            self.relay = None
