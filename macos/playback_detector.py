"""macOS CoreAudio Local Playback Detector.

Monitors active CoreAudio client processes to detect whether any external
process is performing local audio playback (output streams active).

Generic rule:
ANY external CoreAudio process with kAudioProcessPropertyIsRunningOutput == 1
=> external local playback active.

Exclusion authority:
The controller's CURRENT owned speaker playback child process is dynamically
excluded based on runtime ownership.
"""

from __future__ import annotations

import ctypes
import logging
import struct
import threading
import time
from typing import Callable, List, Optional, Set

logger = logging.getLogger(__name__)

def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode("ascii"))[0]

kAudioObjectSystemObject = 1
kAudioObjectPropertyScopeGlobal = _fourcc("glob")
kAudioObjectPropertyElementMain = 0
kAudioHardwarePropertyProcessObjectList = _fourcc("prs#")
kAudioProcessPropertyPID = _fourcc("ppid")
kAudioProcessPropertyIsRunningOutput = _fourcc("piro")


class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


class MacPlaybackActivityDetector:
    """Detects active external audio playback across all CoreAudio client processes.
    
    Excludes Cross-Desk Flow's current authoritative speaker child PID dynamically.
    """

    def __init__(
        self,
        on_activity_change: Optional[Callable[[bool], None]] = None,
        get_current_speaker_pid: Optional[Callable[[], Optional[int]]] = None,
        poll_interval_sec: float = 0.1,
    ):
        self.on_activity_change = on_activity_change
        self.get_current_speaker_pid = get_current_speaker_pid or (lambda: None)
        self.poll_interval_sec = poll_interval_sec

        self._is_external_playback_active = False
        self._active_external_pids: Set[int] = set()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # CoreAudio C API binding
        try:
            self._coreaudio = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
            )
            self._available = True
        except Exception as exc:
            logger.warning("CoreAudio library load failed in PlaybackDetector: %s", exc)
            self._coreaudio = None
            self._available = False

    @property
    def is_external_playback_active(self) -> bool:
        with self._lock:
            return self._is_external_playback_active

    def set_external_playback_active_for_test(self, active: bool) -> None:
        """Test helper to simulate external playback transitions."""
        changed = False
        with self._lock:
            if self._is_external_playback_active != active:
                self._is_external_playback_active = active
                changed = True
        if changed and self.on_activity_change:
            try:
                self.on_activity_change(active)
            except Exception as exc:
                logger.error("Error in on_activity_change callback: %s", exc)

    def scan_active_output_pids(self) -> List[int]:
        """Queries CoreAudio for all client processes currently running output."""
        if not self._available or not self._coreaudio:
            return []

        addr = AudioObjectPropertyAddress(
            mSelector=kAudioHardwarePropertyProcessObjectList,
            mScope=kAudioObjectPropertyScopeGlobal,
            mElement=kAudioObjectPropertyElementMain,
        )

        data_size = ctypes.c_uint32(0)
        st = self._coreaudio.AudioObjectGetPropertyDataSize(
            ctypes.c_uint32(kAudioObjectSystemObject),
            ctypes.byref(addr),
            ctypes.c_uint32(0),
            None,
            ctypes.byref(data_size),
        )
        if st != 0 or data_size.value == 0:
            return []

        num_procs = data_size.value // 4
        proc_ids = (ctypes.c_uint32 * num_procs)()
        st = self._coreaudio.AudioObjectGetPropertyData(
            ctypes.c_uint32(kAudioObjectSystemObject),
            ctypes.byref(addr),
            ctypes.c_uint32(0),
            None,
            ctypes.byref(data_size),
            ctypes.byref(proc_ids),
        )
        if st != 0:
            return []

        active_pids: List[int] = []
        for pobj in proc_ids:
            pid = ctypes.c_int32(0)
            psize = ctypes.c_uint32(ctypes.sizeof(pid))
            paddr = AudioObjectPropertyAddress(
                mSelector=kAudioProcessPropertyPID,
                mScope=kAudioObjectPropertyScopeGlobal,
                mElement=kAudioObjectPropertyElementMain,
            )
            if (
                self._coreaudio.AudioObjectGetPropertyData(
                    ctypes.c_uint32(pobj),
                    ctypes.byref(paddr),
                    0,
                    None,
                    ctypes.byref(psize),
                    ctypes.byref(pid),
                )
                != 0
            ):
                continue

            is_out = ctypes.c_uint32(0)
            psize = ctypes.c_uint32(ctypes.sizeof(is_out))
            paddr.mSelector = kAudioProcessPropertyIsRunningOutput
            if (
                self._coreaudio.AudioObjectGetPropertyData(
                    ctypes.c_uint32(pobj),
                    ctypes.byref(paddr),
                    0,
                    None,
                    ctypes.byref(psize),
                    ctypes.byref(is_out),
                )
                != 0
            ):
                continue

            if is_out.value != 0:
                active_pids.append(int(pid.value))

        return active_pids

    def check_activity_once(self) -> bool:
        """Executes a single check and dispatches callback if state changed."""
        active_pids = self.scan_active_output_pids()
        excluded_speaker_pid = self.get_current_speaker_pid()

        external_pids = {
            p for p in active_pids if p != excluded_speaker_pid
        }
        new_active = len(external_pids) > 0
        changed = False

        with self._lock:
            self._active_external_pids = external_pids
            if new_active != self._is_external_playback_active:
                self._is_external_playback_active = new_active
                changed = True

        if changed and self.on_activity_change:
            try:
                self.on_activity_change(new_active)
            except Exception as exc:
                logger.error("Error in playback on_activity_change callback: %s", exc)

        return new_active

    def start(self) -> None:
        """Starts background polling thread."""
        if not self._available:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_poll_loop,
            name="MacPlaybackActivityDetector",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stops background polling thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def _run_poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_activity_once()
            except Exception as exc:
                logger.debug("Error in playback activity polling: %s", exc)
            self._stop_event.wait(self.poll_interval_sec)
