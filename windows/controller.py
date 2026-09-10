"""Production Audio Bridge Controller for Windows.

Implements the single-instance controller lifecycle:
- Start: Idempotently enables speaker path and reconciles.
- Stop: Idempotently stops owned pipeline and sets STOPPED_BY_USER.
- Status: Read-only query of controller, peer, and speaker state without side effects.
- Singleton guard: Ensures only one controller instance runs per host.
- Ownership: Tracks and terminates ONLY owned child processes.
- Local IPC Server: Serves Start/Stop/Status/Reconcile requests to CLI processes.
- Dependency preflight: Refuses start if runtime dependencies are missing.
"""

import json
import logging
import os
import socket
import sys
import threading
import time
from typing import Optional

from bridge_core.contract import (
    DEFAULT_LOCAL_IPC_PORT,
    DEFAULT_MIC_RTP_PORT,
    DEFAULT_SINGLETON_PORT,
    ControllerStatus,
    DesiredState,
    HostRole,
    LifecycleState,
    PathState,
)
from bridge_core.preflight import check_runtime_dependencies
from .capture_detector import (
    WasapiCaptureSessionDetector,
    WindowsMicrophoneDemandMonitor,
)
from .device_resolver import WindowsDeviceResolver
from .dictation_coordinator import WindowsDictationCoordinator
from .local_control_server import LocalControlServer
from .microphone_receiver import MicrophoneReceiverBuilder
from .pack43_resolver import Pack43Resolver
from .peer_discovery import PeerDiscoveryService
from .process_runner import ProcessRunner, WindowsOwnedProcessRunner
from .speaker_pipeline import SpeakerPipelineBuilder

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = os.path.join(
    os.environ.get("LOCALAPPDATA", "."), "desk-audio-bridge", "controller_state.json"
)

STARTUP_PACK43_MAX_ATTEMPTS: int = 3
STARTUP_PACK43_WINDOW_SEC: float = 30.0
STARTUP_PACK43_RETRY_INTERVAL_SEC: float = 2.0


class SingleInstanceLock:
    """Guarantees controller singleton execution per machine via local socket bind."""

    def __init__(self, port: int = DEFAULT_SINGLETON_PORT):
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._held = False

    def acquire(self) -> bool:
        if self._held and self._sock:
            return True
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", self.port))
            self._sock = s
            self._held = True
            return True
        except (OSError, socket.error):
            self._sock = None
            self._held = False
            return False

    def release(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._held = False

    @property
    def is_held(self) -> bool:
        return self._held


class WindowsBridgeController:
    """The central Windows controller for desk-audio-bridge."""

    def __init__(
        self,
        state_file: str = DEFAULT_STATE_FILE,
        process_runner: Optional[ProcessRunner] = None,
        device_resolver: Optional[WindowsDeviceResolver] = None,
        pipeline_builder: Optional[SpeakerPipelineBuilder] = None,
        discovery_service: Optional[PeerDiscoveryService] = None,
        pack43_resolver: Optional[Pack43Resolver] = None,
        microphone_receiver_builder: Optional[MicrophoneReceiverBuilder] = None,
        capture_detector: Optional[WasapiCaptureSessionDetector] = None,
        demand_monitor: Optional[WindowsMicrophoneDemandMonitor] = None,
        lock_port: int = DEFAULT_SINGLETON_PORT,
        ipc_port: int = DEFAULT_LOCAL_IPC_PORT,
    ):
        self.state_file = state_file
        self.process_runner = process_runner or WindowsOwnedProcessRunner()
        self.device_resolver = device_resolver or WindowsDeviceResolver()
        self.pipeline_builder = pipeline_builder or SpeakerPipelineBuilder()
        self.pack43_resolver = pack43_resolver or Pack43Resolver()
        self.microphone_receiver_builder = microphone_receiver_builder or MicrophoneReceiverBuilder()
        self.capture_detector = capture_detector or WasapiCaptureSessionDetector()
        self.demand_monitor = demand_monitor or WindowsMicrophoneDemandMonitor(
            controller=self,
            detector=self.capture_detector,
        )
        self.lock_port = lock_port
        self.ipc_port = ipc_port
        self._singleton_lock = SingleInstanceLock(port=lock_port)
        self._ipc_server = LocalControlServer(self, port=ipc_port)

        self._desired_state = self._load_persisted_desired_state()
        self._controller_state = LifecycleState.STOPPED
        self._speaker_path_state = PathState.IDLE
        self._last_actionable_error: Optional[str] = None
        self._speaker_child_pid: Optional[int] = None
        self._active_peer_address: Optional[str] = None
        self._active_local_bind: Optional[str] = None

        # Operating mode: PLAYBACK (default) or DICTATION
        self.dictation_coordinator = WindowsDictationCoordinator(self)
        self._mode: str = "PLAYBACK"

        # Microphone path state & desired state:
        # Under Issue #43 Playback/Dictation baseline, microphone defaults to False
        self._microphone_desired: bool = False
        self._microphone_path_state = PathState.IDLE
        self._microphone_child_pid: Optional[int] = None
        self._last_actionable_microphone_error: Optional[str] = None
        self._current_dictation_session: Optional[str] = None
        self._dictation_ack_event = threading.Event()
        self._last_dictation_ack: Optional[dict] = None
        self._shutdown_requested = threading.Event()
        self._lock = threading.RLock()

        # Bounded startup Pack43 recovery tracking
        self._pack43_recovery_start_time: Optional[float] = None
        self._pack43_recovery_attempts: int = 0
        self._last_pack43_attempt_time: float = 0.0

        # Wire discovery service
        self.discovery_service = discovery_service or PeerDiscoveryService(
            local_role=HostRole.WINDOWS,
            instance_id=f"win-{os.getpid()}-{int(time.time())}",
            on_peer_discovered=self._on_peer_discovered,
            on_control_message=self._on_control_message,
        )

    def _on_control_message(self, msg: dict, peer_ip: str) -> None:
        """Handles incoming DICTATION_* control packets from elected peer."""
        msg_type = msg.get("type")
        if msg_type == "DICTATION_START_ACK":
            session_id = msg.get("session_id")
            if session_id and session_id == self._current_dictation_session:
                self._last_dictation_ack = msg
                self._dictation_ack_event.set()

    def _load_persisted_desired_state(self) -> DesiredState:
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

    def _persist_desired_state(self, state: DesiredState) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"desired_state": state.value}, f)
        except Exception as exc:
            logger.warning("Could not persist desired state: %s", exc)

    def _reset_dictation_and_recovery_tracking(self) -> None:
        self._mode = "PLAYBACK"
        self._microphone_desired = False
        self._current_dictation_session = None
        self._dictation_ack_event.clear()
        self._pack43_recovery_start_time = None
        self._pack43_recovery_attempts = 0
        self._last_pack43_attempt_time = 0.0

    def _stop_active_pipelines(self, path_state: PathState = PathState.IDLE) -> None:
        if self._speaker_child_pid is not None:
            self.process_runner.stop_process(self._speaker_child_pid)
            self._speaker_child_pid = None
            self._active_peer_address = None
            self._active_local_bind = None
        self._speaker_path_state = path_state
        if self._microphone_child_pid is not None:
            self.process_runner.stop_process(self._microphone_child_pid)
            self._microphone_child_pid = None
        self._microphone_path_state = path_state

    def start(self) -> bool:
        """Enables the controller and triggers reconcile.
        
        Returns True if this instance successfully holds or already holds the singleton lock.
        Returns False if another process holds the singleton lock.
        """
        with self._lock:
            # Check runtime dependencies preflight
            ok, err_msg = check_runtime_dependencies()
            if not ok:
                self._last_actionable_error = err_msg
                self._controller_state = LifecycleState.ERROR
                logger.error("Preflight failure: %s", err_msg)
                return False

            if not self._singleton_lock.is_held:
                if not self._singleton_lock.acquire():
                    logger.warning("Controller start rejected: another process holds the singleton lock")
                    return False

            self._desired_state = DesiredState.ENABLED
            self._persist_desired_state(DesiredState.ENABLED)
            self._reset_dictation_and_recovery_tracking()
            self._controller_state = LifecycleState.STARTING
            self._speaker_path_state = PathState.IDLE
            self._last_actionable_error = None


            # Start IPC server
            self._ipc_server.start()

            # Start discovery
            try:
                self.discovery_service.start()
            except Exception as exc:
                self._last_actionable_error = f"Discovery start failed: {exc}"
                self._controller_state = LifecycleState.ERROR

            # Start demand monitor
            self.demand_monitor.start()

            self.reconcile()
            return True

    def set_microphone_enabled(self, enabled: bool) -> bool:
        """Explicit desired-state control seam for Windows microphone capability."""
        with self._lock:
            self._microphone_desired = enabled
            if not enabled:
                if self._microphone_child_pid is not None:
                    self.process_runner.stop_process(self._microphone_child_pid)
                    self._microphone_child_pid = None
                self._microphone_path_state = PathState.STOPPED
                self._last_actionable_microphone_error = None
                self._pack43_recovery_start_time = None
                self._pack43_recovery_attempts = 0
                return True

            # When enabling microphone, arm recovery budget and invalidate stale/negative cache
            self._pack43_recovery_start_time = None
            self._pack43_recovery_attempts = 0
            self._last_pack43_attempt_time = 0.0
            if self.pack43_resolver.is_cached_available is not True:
                self.pack43_resolver.invalidate_cache()

            # When enabling microphone, run reconcile
            self.reconcile()
            return self._microphone_path_state == PathState.RUNNING

    def start_dictation(self, timeout: float = 3.0) -> bool:
        return self.dictation_coordinator.start_dictation(timeout=timeout)

    def end_dictation(self) -> bool:
        return self.dictation_coordinator.end_dictation()

    def stop(self) -> bool:
        """Idempotently stops speaker and microphone pipelines and sets STOPPED_BY_USER."""
        with self._lock:
            self._desired_state = DesiredState.STOPPED_BY_USER
            self._persist_desired_state(DesiredState.STOPPED_BY_USER)
            self._reset_dictation_and_recovery_tracking()
            self._stop_active_pipelines(PathState.STOPPED)
            self.demand_monitor.stop()
            self.discovery_service.stop()
            self._controller_state = LifecycleState.STOPPED
            return True

    def start_host(self) -> bool:
        """Starts controller host runtime according to persisted desired state without mutating it.
        
        - Checks preflight dependencies.
        - Acquires singleton lock.
        - Starts local IPC server.
        - Loads persisted desired state.
        - If ENABLED: defaults to PLAYBACK (microphone False), starts discovery, and reconciles pipelines.
        - If STOPPED_BY_USER: leaves controller in STOPPED state with zero media children.
        """
        with self._lock:
            ok, err_msg = check_runtime_dependencies()
            if not ok:
                self._last_actionable_error = err_msg
                self._controller_state = LifecycleState.ERROR
                logger.error("Preflight failure: %s", err_msg)
                return False

            if not self._singleton_lock.is_held:
                if not self._singleton_lock.acquire():
                    logger.warning("Controller start rejected: another process holds the singleton lock")
                    return False

            self._desired_state = self._load_persisted_desired_state()
            self._ipc_server.start()

            if self._desired_state == DesiredState.ENABLED:
                self._reset_dictation_and_recovery_tracking()
                self._controller_state = LifecycleState.STARTING
                self._speaker_path_state = PathState.IDLE
                self._last_actionable_error = None
                try:
                    self.discovery_service.start()
                except Exception as exc:
                    self._last_actionable_error = f"Discovery start failed: {exc}"
                    self._controller_state = LifecycleState.ERROR
                self.demand_monitor.start()
                self.reconcile()
            else:
                self._reset_dictation_and_recovery_tracking()
                self._controller_state = LifecycleState.STOPPED
                self._speaker_path_state = PathState.STOPPED
                self._microphone_path_state = PathState.STOPPED
                self.demand_monitor.stop()

            return True

    def request_host_shutdown(self) -> None:
        """Requests graceful host shutdown from the host main loop without mutating desired state."""
        self._shutdown_requested.set()

    @property
    def is_shutdown_requested(self) -> bool:
        """Returns True if a shutdown has been requested."""
        return self._shutdown_requested.is_set()

    def shutdown_host(self) -> None:
        """Shuts down host runtime, stopping owned children, IPC, and singleton WITHOUT mutating desired state."""
        with self._lock:
            self._mode = "PLAYBACK"
            self._current_dictation_session = None
            self._dictation_ack_event.clear()
            self._stop_active_pipelines(PathState.STOPPED)
            self.demand_monitor.stop()
            self.discovery_service.stop()
            self._ipc_server.stop()
            self._singleton_lock.release()
            self._controller_state = LifecycleState.STOPPED

    def shutdown(self) -> None:
        """Full shutdown of controller host. Deprecated alias for shutdown_host."""
        self.shutdown_host()

    def get_status(self) -> ControllerStatus:
        """Pure read-only query of controller status without side-effects."""
        with self._lock:
            owned_count = 0
            if self._speaker_child_pid and self.process_runner.is_running(self._speaker_child_pid):
                owned_count += 1
            if self._microphone_child_pid and self.process_runner.is_running(self._microphone_child_pid):
                owned_count += 1

            # Check if discovery reported an enumeration error
            last_err = self._last_actionable_error
            disc_err = getattr(self.discovery_service, "last_enumeration_error", None)
            if not last_err and disc_err:
                last_err = disc_err

            peer_addr = self.discovery_service.peer_address
            local_bind = self.discovery_service.local_bind_address
            if self._speaker_path_state == PathState.RUNNING and self._active_peer_address:
                peer_addr = self._active_peer_address
                local_bind = self._active_local_bind

            # Pack43 availability: tri-state without triggering CIM/WMI (True / False / None)
            pack43_avail = self.pack43_resolver.is_cached_available

            # Determine honest microphone path state reflection:
            # - If currently RUNNING, FAILED, UNAVAILABLE, or explicitly STOPPED, keep as-is
            # - If not running:
            #   * if STOPPED by user: STOPPED
            #   * if Pack43 is cached available: READY
            #   * if Pack43 is cached unavailable: UNAVAILABLE
            #   * otherwise: IDLE
            mic_state = self._microphone_path_state
            if mic_state not in (PathState.RUNNING, PathState.FAILED, PathState.UNAVAILABLE, PathState.STOPPED):
                if self._desired_state == DesiredState.STOPPED_BY_USER:
                    mic_state = PathState.STOPPED
                elif pack43_avail is True:
                    mic_state = PathState.READY
                elif pack43_avail is False:
                    mic_state = PathState.UNAVAILABLE
                else:
                    mic_state = PathState.IDLE

            return ControllerStatus(
                controller_state=self._controller_state.value,
                desired_state=self._desired_state.value,
                role=HostRole.WINDOWS.value,
                peer_available=self.discovery_service.peer_available,
                peer_address=peer_addr,
                local_bind_address=local_bind,
                speaker_path_state=self._speaker_path_state.value,
                speaker_target_port=self.discovery_service.peer_speaker_port,
                last_actionable_error=last_err,
                owned_children_count=owned_count,
                owner_pid=os.getpid() if self._singleton_lock.is_held else None,
                microphone_path_state=mic_state.value,
                microphone_port=DEFAULT_MIC_RTP_PORT,
                pack43_available=pack43_avail,
                last_actionable_microphone_error=self._last_actionable_microphone_error,
                mode=self._mode,
            )


    def reconcile(self) -> None:
        """Idempotently brings actual state toward desired state."""
        with self._lock:
            # Explicitly advance discovery state, pruning expired responders and handling ambiguity recovery
            if hasattr(self.discovery_service, "refresh_peer_state"):
                self.discovery_service.refresh_peer_state()

            # 1. Controller STOPPED_BY_USER -> Stop both speaker and microphone
            if self._desired_state == DesiredState.STOPPED_BY_USER:
                self._stop_active_pipelines(PathState.STOPPED)
                self._controller_state = LifecycleState.STOPPED
                return

            # 2. Peer Ambiguous -> Stop both speaker and microphone
            if getattr(self.discovery_service, "is_ambiguous", False):
                self._last_actionable_error = "Multiple opposite-role responders discovered; manual peer selection required"
                self._controller_state = LifecycleState.AMBIGUOUS_PEER
                self._stop_active_pipelines(PathState.IDLE)
                return

            # 3. GStreamer binary missing -> Fatal error for pipeline
            if not self.pipeline_builder.is_gstreamer_available():
                self._last_actionable_error = "GStreamer binary not found at configured path"
                self._controller_state = LifecycleState.ERROR
                self._stop_active_pipelines(PathState.IDLE)
                self._speaker_path_state = PathState.FAILED
                return

            # 4. Peer Unavailable -> Stop both pipelines, enter DISCOVERING
            if not self.discovery_service.peer_available:
                self._controller_state = LifecycleState.DISCOVERING
                self.discovery_service.broadcast_hello()
                disc_err = getattr(self.discovery_service, "last_enumeration_error", None)
                if disc_err:
                    self._last_actionable_error = disc_err
                    self._controller_state = LifecycleState.ERROR
                self._stop_active_pipelines(PathState.IDLE)
                return

            # Peer is valid and available: reconcile speaker and microphone independently
            self._reconcile_speaker()
            self._reconcile_microphone()

    def _reconcile_speaker(self) -> None:
        """Idempotently reconciles the Windows speaker sender path."""
        # Suppress speaker when in DICTATION mode
        if self._mode == "DICTATION":
            if self._speaker_child_pid is not None:
                self.process_runner.stop_process(self._speaker_child_pid)
                self._speaker_child_pid = None
            self._speaker_path_state = PathState.STOPPED
            return

        # Resolve playback endpoint for speaker
        endpoint_id = self.device_resolver.resolve_default_playback_endpoint_id()
        if not endpoint_id:
            self._last_actionable_error = "Windows Playback Source endpoint resolution failed"
            self._controller_state = LifecycleState.ERROR
            self._speaker_path_state = PathState.FAILED
            return

        # Verify if speaker pipeline already running
        if self._speaker_child_pid and self.process_runner.is_running(self._speaker_child_pid):
            self._controller_state = LifecycleState.ACTIVE
            self._speaker_path_state = PathState.RUNNING
            return

        # Create speaker pipeline child
        target_ip = self.discovery_service.peer_address
        target_port = self.discovery_service.peer_speaker_port
        local_bind = self.discovery_service.local_bind_address
        cmd = self.pipeline_builder.build_sender_command(
            target_host=target_ip,
            target_port=target_port,
            device_id=endpoint_id,
            local_bind_ip=local_bind,
        )

        try:
            pid = self.process_runner.start_process(cmd)
            self._speaker_child_pid = pid
            self._active_peer_address = target_ip
            self._active_local_bind = local_bind
            self._speaker_path_state = PathState.RUNNING
            self._controller_state = LifecycleState.ACTIVE
            self._last_actionable_error = None
        except Exception as exc:
            self._last_actionable_error = f"Failed to start speaker pipeline: {exc}"
            self._active_peer_address = None
            self._active_local_bind = None
            self._speaker_path_state = PathState.FAILED
            self._controller_state = LifecycleState.ERROR

    def _reconcile_microphone(self) -> None:
        """Idempotently reconciles the Windows microphone receiver path."""
        if not self._microphone_desired:
            if self._microphone_child_pid is not None:
                self.process_runner.stop_process(self._microphone_child_pid)
                self._microphone_child_pid = None
            if self._desired_state == DesiredState.STOPPED_BY_USER:
                self._microphone_path_state = PathState.STOPPED
            else:
                self._microphone_path_state = PathState.READY if self.pack43_resolver.is_cached_available is True else PathState.IDLE
            return

        # Microphone is desired: check if pipeline already running
        if self._microphone_child_pid is not None:
            if self.process_runner.is_running(self._microphone_child_pid):
                self._microphone_path_state = PathState.RUNNING
                return
            # Child exited unexpectedly
            self._microphone_child_pid = None
            self.pack43_resolver.invalidate_cache()

        # Check GStreamer availability for receiver
        if not self.microphone_receiver_builder.is_gstreamer_available():
            self._last_actionable_microphone_error = "GStreamer binary not found for microphone receiver"
            self._microphone_path_state = PathState.FAILED
            return

        # Bounded startup Pack43 recovery probe logic:
        # If Pack43 is cached unavailable, check whether we are still within the startup recovery budget.
        # If within budget and retry interval has elapsed, invalidate the negative cache to allow
        # another probe attempt during startup. Once budget is exhausted, do not retry (no steady-state hammering).
        now = time.time()
        if self.pack43_resolver.is_cached_available is False:
            can_retry = False
            if self._pack43_recovery_start_time is not None:
                within_window = (now - self._pack43_recovery_start_time) < STARTUP_PACK43_WINDOW_SEC
                within_attempts = self._pack43_recovery_attempts < STARTUP_PACK43_MAX_ATTEMPTS
                interval_elapsed = (now - self._last_pack43_attempt_time) >= STARTUP_PACK43_RETRY_INTERVAL_SEC
                if within_window and within_attempts and interval_elapsed:
                    can_retry = True

            if can_retry:
                logger.info(
                    "Retrying transient Pack43 startup resolution (attempt %d/%d)",
                    self._pack43_recovery_attempts + 1,
                    STARTUP_PACK43_MAX_ATTEMPTS,
                )
                self._pack43_recovery_attempts += 1
                self._last_pack43_attempt_time = now
                self.pack43_resolver.invalidate_cache()
            else:
                # Budget exhausted or interval not elapsed: fail-closed without WMI enumeration
                self._last_actionable_microphone_error = "Standard VB-CABLE Pack43 not found or driver identity mismatch"
                self._microphone_path_state = PathState.UNAVAILABLE
                return

        # Resolve Pack43 render endpoint
        pack43_result = self.pack43_resolver.resolve_pack43()
        if not pack43_result:
            # First negative result: initialize startup recovery budget if not already tracking
            if self._pack43_recovery_start_time is None:
                self._pack43_recovery_start_time = now
                self._pack43_recovery_attempts = 1
                self._last_pack43_attempt_time = now
            self._last_actionable_microphone_error = "Standard VB-CABLE Pack43 not found or driver identity mismatch"
            self._microphone_path_state = PathState.UNAVAILABLE
            return

        # Resolution succeeded: reset startup recovery tracking
        self._pack43_recovery_start_time = None
        self._pack43_recovery_attempts = 0

        # Build receiver command
        local_bind = self.discovery_service.local_bind_address
        cmd = self.microphone_receiver_builder.build_receiver_command(
            local_bind_ip=local_bind,
            local_port=DEFAULT_MIC_RTP_PORT,
            device_id=pack43_result.render_endpoint_id,
        )

        try:
            pid = self.process_runner.start_process(cmd)
            self._microphone_child_pid = pid
            self._microphone_path_state = PathState.RUNNING
            self._last_actionable_microphone_error = None
        except Exception as exc:
            self._last_actionable_microphone_error = f"Failed to start microphone receiver: {exc}"
            self._microphone_path_state = PathState.FAILED
            self.pack43_resolver.invalidate_cache()

    def _on_peer_discovered(self, peer_ip: str, local_ip: str, peer_port: int, peer_inst: str) -> None:
        with self._lock:
            if self._desired_state == DesiredState.ENABLED:
                self.reconcile()

    def _on_control_message(self, msg: dict, peer_ip: str) -> None:
        self.dictation_coordinator.handle_control_message(msg, peer_ip)

    @property
    def _mode(self) -> str:
        return self.dictation_coordinator.mode

    @_mode.setter
    def _mode(self, val: str) -> None:
        self.dictation_coordinator.mode = val

    @property
    def _current_dictation_session(self) -> Optional[str]:
        return self.dictation_coordinator.current_dictation_session

    @_current_dictation_session.setter
    def _current_dictation_session(self, val: Optional[str]) -> None:
        self.dictation_coordinator.current_dictation_session = val


