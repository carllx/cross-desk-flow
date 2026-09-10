"""WASAPI Audio Session Capture Detector and Demand Monitor for Windows (Issue #22).

Detects active WASAPI capture streams on Pack43 (e.g. input method, speech recognition,
recording apps) to trigger automatic Dictation mode transitions on WindowsBridgeController.
"""

import ctypes
from ctypes import wintypes
import logging
import sys
import threading
import time
from typing import Any, Callable, Optional

from bridge_core.contract import DesiredState

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", wintypes.BYTE * 8),
        ]

    CLSID_MMDeviceEnumerator = GUID(
        0xBCDE0395,
        0xE52F,
        0x467C,
        (wintypes.BYTE * 8)(0x8E, 0x3D, 0xC4, 0x57, 0x92, 0x91, 0x69, 0x2E),
    )
    IID_IMMDeviceEnumerator = GUID(
        0xA95664D2,
        0x9614,
        0x4F35,
        (wintypes.BYTE * 8)(0xA7, 0x46, 0xDE, 0x8D, 0xB6, 0x36, 0x17, 0xE6),
    )
    IID_IAudioSessionManager2 = GUID(
        0x77AA99A0,
        0x1BD6,
        0x484F,
        (wintypes.BYTE * 8)(0x8B, 0xC7, 0x2C, 0x65, 0x4C, 0x9A, 0x9B, 0x6F),
    )
    CLSCTX_ALL = 23
else:
    GUID = None  # type: ignore


class WasapiCaptureSessionDetector:
    """Detects active WASAPI audio client capture sessions on a specified endpoint."""

    def __init__(
        self,
        query_func: Optional[Callable[[str], bool]] = None,
        co_init_func: Optional[Callable[[], int]] = None,
        co_uninit_func: Optional[Callable[[], None]] = None,
    ):
        self._query_func = query_func
        self._co_init_func = co_init_func
        self._co_uninit_func = co_uninit_func

    def is_capture_active(self, endpoint_id: str) -> bool:
        """Returns True if at least one audio session on endpoint_id is in AudioSessionStateActive."""
        if self._query_func is not None:
            return bool(self._query_func(endpoint_id))

        if not endpoint_id:
            return False

        if sys.platform != "win32" and self._co_init_func is None:
            return False

        return self._query_wasapi(endpoint_id)

    def _query_wasapi(self, endpoint_id: str) -> bool:
        if "{" in endpoint_id:
            endpoint_id = endpoint_id[endpoint_id.index("{"):]

        ole32 = getattr(ctypes.windll, "ole32", None) if sys.platform == "win32" else None
        need_uninit = False
        if self._co_init_func is not None:
            try:
                hr = self._co_init_func()
                need_uninit = hr in (0, 1)
            except Exception as exc:
                logger.debug("Custom COM init failed: %s", exc)
                return False
        elif ole32 is not None:
            try:
                hr = ole32.CoInitialize(None)
                # S_OK (0) and S_FALSE (1) are successful COM initializations that must be balanced by CoUninitialize()
                need_uninit = hr in (0, 1)
            except Exception as exc:
                logger.debug("CoInitialize failed: %s", exc)
                return False

        try:
            if ole32 is None:
                return False
            p_enum = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(
                ctypes.byref(CLSID_MMDeviceEnumerator),
                None,
                CLSCTX_ALL,
                ctypes.byref(IID_IMMDeviceEnumerator),
                ctypes.byref(p_enum),
            )
            if hr != 0 or not p_enum.value:
                return False

            try:
                enum_vtable = ctypes.cast(
                    p_enum, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
                ).contents
                get_device_proto = ctypes.WINFUNCTYPE(
                    wintypes.LONG,
                    ctypes.c_void_p,
                    wintypes.LPCWSTR,
                    ctypes.POINTER(ctypes.c_void_p),
                )
                get_device = get_device_proto(enum_vtable[5])

                p_dev = ctypes.c_void_p()
                hr = get_device(p_enum, endpoint_id, ctypes.byref(p_dev))
                if hr != 0 or not p_dev.value:
                    return False

                try:
                    dev_vtable = ctypes.cast(
                        p_dev, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
                    ).contents
                    activate_proto = ctypes.WINFUNCTYPE(
                        wintypes.LONG,
                        ctypes.c_void_p,
                        ctypes.POINTER(GUID),
                        wintypes.DWORD,
                        ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_void_p),
                    )
                    activate = activate_proto(dev_vtable[3])

                    p_mgr = ctypes.c_void_p()
                    hr = activate(
                        p_dev,
                        ctypes.byref(IID_IAudioSessionManager2),
                        CLSCTX_ALL,
                        None,
                        ctypes.byref(p_mgr),
                    )
                    if hr != 0 or not p_mgr.value:
                        return False

                    try:
                        mgr_vtable = ctypes.cast(
                            p_mgr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
                        ).contents
                        get_sess_enum_proto = ctypes.WINFUNCTYPE(
                            wintypes.LONG,
                            ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_void_p),
                        )
                        get_session_enum = get_sess_enum_proto(mgr_vtable[5])

                        p_sess_enum = ctypes.c_void_p()
                        hr = get_session_enum(p_mgr, ctypes.byref(p_sess_enum))
                        if hr != 0 or not p_sess_enum.value:
                            return False

                        try:
                            sess_vtable = ctypes.cast(
                                p_sess_enum,
                                ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
                            ).contents
                            get_count_proto = ctypes.WINFUNCTYPE(
                                wintypes.LONG,
                                ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_int),
                            )
                            get_session_proto = ctypes.WINFUNCTYPE(
                                wintypes.LONG,
                                ctypes.c_void_p,
                                ctypes.c_int,
                                ctypes.POINTER(ctypes.c_void_p),
                            )
                            sess_get_count = get_count_proto(sess_vtable[3])
                            sess_get_session = get_session_proto(sess_vtable[4])

                            scount = ctypes.c_int(0)
                            hr = sess_get_count(p_sess_enum, ctypes.byref(scount))
                            if hr != 0:
                                return False

                            for i in range(scount.value):
                                p_ctrl = ctypes.c_void_p()
                                hr = sess_get_session(p_sess_enum, i, ctypes.byref(p_ctrl))
                                if hr == 0 and p_ctrl.value:
                                    try:
                                        ctrl_vtable = ctypes.cast(
                                            p_ctrl,
                                            ctypes.POINTER(
                                                ctypes.POINTER(ctypes.c_void_p)
                                            ),
                                        ).contents
                                        get_state_proto = ctypes.WINFUNCTYPE(
                                            wintypes.LONG,
                                            ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_int),
                                        )
                                        get_state = get_state_proto(ctrl_vtable[3])
                                        st = ctypes.c_int(0)
                                        if get_state(p_ctrl, ctypes.byref(st)) == 0:
                                            if st.value == 1:  # AudioSessionStateActive
                                                return True
                                    finally:
                                        release_ctrl = ctypes.WINFUNCTYPE(
                                            wintypes.ULONG, ctypes.c_void_p
                                        )(ctrl_vtable[2])
                                        release_ctrl(p_ctrl)
                        finally:
                            release_sess_enum = ctypes.WINFUNCTYPE(
                                wintypes.ULONG, ctypes.c_void_p
                            )(sess_vtable[2])
                            release_sess_enum(p_sess_enum)
                    finally:
                        release_mgr = ctypes.WINFUNCTYPE(
                            wintypes.ULONG, ctypes.c_void_p
                        )(mgr_vtable[2])
                        release_mgr(p_mgr)
                finally:
                    release_dev = ctypes.WINFUNCTYPE(
                        wintypes.ULONG, ctypes.c_void_p
                    )(dev_vtable[2])
                    release_dev(p_dev)
            finally:
                release_enum = ctypes.WINFUNCTYPE(
                    wintypes.ULONG, ctypes.c_void_p
                )(enum_vtable[2])
                release_enum(p_enum)
        except Exception as exc:
            logger.debug("Error checking capture activity for %s: %s", endpoint_id, exc)
            return False
        finally:
            if need_uninit:
                if self._co_uninit_func is not None:
                    try:
                        self._co_uninit_func()
                    except Exception:
                        pass
                elif ole32 is not None:
                    try:
                        ole32.CoUninitialize()
                    except Exception:
                        pass
        return False


class WindowsMicrophoneDemandMonitor:
    """Monitors Windows microphone demand on Pack43 capture endpoint and triggers Dictation transitions.
    
    Adheres strictly to the Issue #22 Product Contract:
    - Normal (Playback): Speaker active, Mic standby, Voice input Automatic/Standby.
    - Capture activity 0 -> 1: start_dictation() exactly once.
    - Capture activity 1 -> 0: end_dictation() exactly once after end_grace_seconds debounce.
    - When controller desired_state is STOPPED_BY_USER: capture activity MUST NOT auto-start Dictation.
    """

    def __init__(
        self,
        controller: Any,
        detector: Optional[WasapiCaptureSessionDetector] = None,
        poll_interval: float = 0.1,
        end_grace_seconds: float = 0.5,
    ):
        self.controller = controller
        self.detector = detector or WasapiCaptureSessionDetector()
        self.poll_interval = poll_interval
        self.end_grace_seconds = end_grace_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Demand tracking state
        self._auto_dictation_active = False
        self._last_active_time: float = 0.0
        self._failed_cooldown: float = 0.0

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def is_auto_dictation_active(self) -> bool:
        return self._auto_dictation_active

    def start(self) -> None:
        """Starts the background demand monitor thread idempotently."""
        with self._lock:
            if self._thread is not None:
                if self._thread.is_alive():
                    return
                self._thread = None
            self._stop_event.clear()
            self._auto_dictation_active = False
            self._last_active_time = 0.0
            self._failed_cooldown = 0.0
            self._thread = threading.Thread(
                target=self._run,
                name="windows-demand-monitor",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> bool:
        """Stops the demand monitor thread idempotently.
        
        Guarantees that:
        - self._thread reference is NEVER cleared if the thread is still alive;
        - Does not hold self._lock while waiting for join;
        - Returns True if thread is stopped or was not running, False if join timed out.
        """
        self._stop_event.set()
        thread_to_join = None
        with self._lock:
            if self._thread is not None:
                if self._thread.is_alive():
                    thread_to_join = self._thread
                else:
                    self._thread = None

        if thread_to_join is not None:
            thread_to_join.join(timeout=timeout)
            with self._lock:
                if not thread_to_join.is_alive():
                    self._thread = None
                    self._auto_dictation_active = False
                    return True
                else:
                    logger.warning(
                        "Demand monitor thread %s did not terminate within %ss timeout",
                        thread_to_join.name,
                        timeout,
                    )
                    return False

        with self._lock:
            self._auto_dictation_active = False
        return True

    def check_demand_step(self) -> None:
        """Performs a single evaluation step of microphone demand.
        
        Public for direct, deterministic unit testing without multi-threading races.
        """
        c = self.controller
        # 1. Check controller desired state: if not ENABLED, do not trigger dictation
        if c._desired_state != DesiredState.ENABLED:
            if self._auto_dictation_active:
                self._auto_dictation_active = False
            return

        # 2. Check shutdown flag
        if getattr(c, "is_shutdown_requested", False):
            return

        # 3. Check Mac peer availability: cannot transition without peer
        disc = getattr(c, "discovery_service", None)
        if not disc or not disc.peer_available:
            if self._auto_dictation_active:
                c.end_dictation()
                self._auto_dictation_active = False
            return

        # 4. Resolve Pack43 capture endpoint ID
        pack43 = getattr(c, "pack43_resolver", None)
        if not pack43:
            return

        pack43_result = pack43.resolve_pack43()
        if not pack43_result or not pack43_result.capture_endpoint_id:
            return

        endpoint_id = pack43_result.capture_endpoint_id

        # 5. Query WASAPI capture activity
        is_active = self.detector.is_capture_active(endpoint_id)
        now = time.time()
        current_mode = getattr(c.dictation_coordinator, "mode", "PLAYBACK")

        if is_active:
            self._last_active_time = now
            if current_mode != "DICTATION":
                # Edge: 0 -> 1
                if now >= self._failed_cooldown:
                    logger.info("Pack43 capture activity detected (0 -> 1); starting dictation")
                    ok = c.start_dictation(timeout=3.0)
                    if ok:
                        self._auto_dictation_active = True
                    else:
                        logger.warning("Auto dictation start failed; backing off for 2.0s")
                        self._failed_cooldown = now + 2.0
            else:
                # Dictation already active; mark auto if not already
                pass
        else:
            # Capture is inactive (0)
            if current_mode == "DICTATION":
                if self._auto_dictation_active:
                    # Edge: 1 -> 0 after end grace debounce
                    if (now - self._last_active_time) >= self.end_grace_seconds:
                        logger.info("Pack43 capture activity ended (1 -> 0); ending dictation")
                        c.end_dictation()
                        self._auto_dictation_active = False
            else:
                self._auto_dictation_active = False

    def _run(self) -> None:
        co_inited = False
        if sys.platform == "win32":
            try:
                hr = ctypes.windll.ole32.CoInitializeEx(None, 0)
                co_inited = hr in (0, 1)
            except Exception:
                pass

        try:
            while not self._stop_event.is_set():
                try:
                    self.check_demand_step()
                except Exception as exc:
                    logger.debug("Error in demand monitor loop: %s", exc)

                if self._stop_event.wait(self.poll_interval):
                    break
        finally:
            if co_inited and sys.platform == "win32":
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass
