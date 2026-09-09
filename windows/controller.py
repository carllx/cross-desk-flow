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
from .device_resolver import WindowsDeviceResolver
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


class LocalControlServer:
    """TCP server running on 127.0.0.1:50106 to serve CLI requests."""

    def __init__(self, controller: "WindowsBridgeController", port: int = DEFAULT_LOCAL_IPC_PORT):
        self.controller = controller
        self.port = port
        self._server_sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> bool:
        if self._running:
            return True
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", self.port))
            s.listen(5)
            self._server_sock = s
            self._running = True
            self._thread = threading.Thread(
                target=self._serve_loop, daemon=True, name="LocalControlServer"
            )
            self._thread.start()
            return True
        except Exception as exc:
            logger.debug("Failed to start LocalControlServer on port %d: %s", self.port, exc)
            return False

    def stop(self) -> None:
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
            self._server_sock = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def _serve_loop(self) -> None:
        while self._running and self._server_sock:
            try:
                client, _ = self._server_sock.accept()
            except (OSError, socket.error):
                break

            try:
                data = client.recv(4096)
                if not data:
                    client.close()
                    continue
                req = json.loads(data.decode("utf-8"))
                cmd = req.get("command")

                res = {}
                if cmd == "start":
                    success = self.controller.start()
                    res = {"success": success, "desired_state": self.controller.get_status().desired_state}
                elif cmd == "stop":
                    success = self.controller.stop()
                    res = {"success": success, "desired_state": self.controller.get_status().desired_state}
                elif cmd == "reconcile":
                    self.controller.reconcile()
                    res = {"success": True}
                elif cmd == "status":
                    res = self.controller.get_status().to_dict()
                elif cmd == "mic-enable":
                    success = self.controller.set_microphone_enabled(True)
                    res = {"success": success, "microphone_path_state": self.controller.get_status().microphone_path_state}
                elif cmd == "mic-disable":
                    success = self.controller.set_microphone_enabled(False)
                    res = {"success": success, "microphone_path_state": self.controller.get_status().microphone_path_state}
                elif cmd == "dictation-start":
                    success = self.controller.start_dictation()
                    st = self.controller.get_status()
                    res = {
                        "success": success,
                        "mode": st.mode,
                        "microphone_path_state": st.microphone_path_state,
                        "speaker_path_state": st.speaker_path_state,
                        "error": st.last_actionable_microphone_error or st.last_actionable_error,
                    }
                elif cmd == "dictation-end":
                    success = self.controller.end_dictation()
                    st = self.controller.get_status()
                    res = {
                        "success": success,
                        "mode": st.mode,
                        "microphone_path_state": st.microphone_path_state,
                        "speaker_path_state": st.speaker_path_state,
                    }
                elif cmd == "shutdown":

                    # Request graceful host shutdown via main loop: do not mutate desired state
                    self.controller.request_host_shutdown()
                    res = {"success": True}
                else:
                    res = {"error": f"Unknown command {cmd}"}

                client.sendall(json.dumps(res).encode("utf-8"))
            except Exception as exc:
                try:
                    client.sendall(json.dumps({"error": str(exc)}).encode("utf-8"))
                except Exception:
                    pass
            finally:
                try:
                    client.close()
                except Exception:
                    pass


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
        lock_port: int = DEFAULT_SINGLETON_PORT,
        ipc_port: int = DEFAULT_LOCAL_IPC_PORT,
    ):
        self.state_file = state_file
        self.process_runner = process_runner or WindowsOwnedProcessRunner()
        self.device_resolver = device_resolver or WindowsDeviceResolver()
        self.pipeline_builder = pipeline_builder or SpeakerPipelineBuilder()
        self.pack43_resolver = pack43_resolver or Pack43Resolver()
        self.microphone_receiver_builder = microphone_receiver_builder or MicrophoneReceiverBuilder()
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
            # Default to PLAYBACK mode with microphone off
            self._mode = "PLAYBACK"
            self._microphone_desired = False
            self._current_dictation_session = None
            self._dictation_ack_event.clear()
            self._pack43_recovery_start_time = None
            self._pack43_recovery_attempts = 0
            self._last_pack43_attempt_time = 0.0
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
        import uuid

        with self._lock:
            if self._desired_state != DesiredState.ENABLED:
                logger.warning("Cannot start dictation: controller is not ENABLED")
                return False

            if not self.discovery_service.peer_available or not self.discovery_service.peer_address:
                logger.warning("Cannot start dictation: Mac peer is not available")
                return False

            # Duplicate Start Dictation check: idempotent
            if (
                self._mode == "DICTATION"
                and self._microphone_child_pid is not None
                and self.process_runner.is_running(self._microphone_child_pid)
            ):
                return True

            # 1. Suppress Windows → Mac speaker
            if self._speaker_child_pid is not None:
                self.process_runner.stop_process(self._speaker_child_pid)
                self._speaker_child_pid = None
            self._speaker_path_state = PathState.STOPPED

            # 2. Resolve Pack43
            pack43_result = self.pack43_resolver.resolve_pack43()
            if not pack43_result:
                self._last_actionable_microphone_error = (
                    "Standard VB-CABLE Pack43 not found or driver identity mismatch"
                )
                self._microphone_path_state = PathState.UNAVAILABLE
                # Restore speaker and abort without opening Mac microphone
                self._mode = "PLAYBACK"
                self._microphone_desired = False
                self._reconcile_speaker()
                return False

            # Ensure any prior microphone child is cleanly terminated
            if self._microphone_child_pid is not None:
                self.process_runner.stop_process(self._microphone_child_pid)
                self._microphone_child_pid = None

            # 3. START WINDOWS MICROPHONE RECEIVER
            local_bind = self.discovery_service.local_bind_address
            cmd = self.microphone_receiver_builder.build_receiver_command(
                local_bind_ip=local_bind,
                local_port=DEFAULT_MIC_RTP_PORT,
                device_id=pack43_result.render_endpoint_id,
            )

            try:
                pid = self.process_runner.start_process(cmd)
                self._microphone_child_pid = pid
            except Exception as exc:
                self._last_actionable_microphone_error = f"Failed to start microphone receiver: {exc}"
                self._microphone_path_state = PathState.FAILED
                self._mode = "PLAYBACK"
                self._microphone_desired = False
                self._reconcile_speaker()
                return False

            # 4. Confirm receiver child is actually running/listening
            if not self.process_runner.is_running(self._microphone_child_pid):
                self.process_runner.stop_process(self._microphone_child_pid)
                self._microphone_child_pid = None
                self._last_actionable_microphone_error = "Microphone receiver child exited immediately"
                self._microphone_path_state = PathState.FAILED
                self._mode = "PLAYBACK"
                self._microphone_desired = False
                self._reconcile_speaker()
                return False

            # 5. Generate fresh dictation session_id
            session_id = f"dict-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
            self._current_dictation_session = session_id
            self._dictation_ack_event.clear()
            self._last_dictation_ack = None

            # 6. Send DICTATION_START(session_id) to Mac via authoritative 50100 control plane
            target_ip = self.discovery_service.peer_address
            start_msg = {
                "version": 1,
                "role": HostRole.WINDOWS.value,
                "instance_id": self.discovery_service.instance_id,
                "type": "DICTATION_START",
                "session_id": session_id,
            }
            sent = self.discovery_service.send_control_message(target_ip, start_msg)
            if not sent:
                self._cleanup_failed_dictation(session_id, "Failed to send DICTATION_START to Mac")
                return False

        # 7. Wait for matching ACK from Mac
        got_ack = self._dictation_ack_event.wait(timeout=timeout)

        with self._lock:
            if self._current_dictation_session != session_id:
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
            self._mode = "DICTATION"
            self._microphone_desired = True
            self._microphone_path_state = PathState.RUNNING
            self._last_actionable_microphone_error = None
            return True

    def _cleanup_failed_dictation(self, session_id: str, error_msg: Optional[str] = None) -> None:
        """Rolls back failed dictation: stops Windows receiver, ensures Mac mic stopped, restores speaker."""
        if error_msg:
            self._last_actionable_microphone_error = error_msg
        if self._microphone_child_pid is not None:
            self.process_runner.stop_process(self._microphone_child_pid)
            self._microphone_child_pid = None

        target_ip = self.discovery_service.peer_address
        if target_ip and session_id:
            stop_msg = {
                "version": 1,
                "role": HostRole.WINDOWS.value,
                "instance_id": self.discovery_service.instance_id,
                "type": "DICTATION_STOP",
                "session_id": session_id,
            }
            self.discovery_service.send_control_message(target_ip, stop_msg)

        self._microphone_desired = False
        self._current_dictation_session = None
        self._microphone_path_state = (
            PathState.UNAVAILABLE
            if self.pack43_resolver.is_cached_available is False
            else PathState.IDLE
        )
        self._mode = "PLAYBACK"
        self._reconcile_speaker()

    def end_dictation(self) -> bool:
        """Exits DICTATION mode and returns to PLAYBACK:
        1. stop Mac microphone sender (via 50100 DICTATION_STOP)
        2. stop Windows microphone receiver
        3. microphone children must be 0
        4. restore Windows → Mac speaker
        5. return to PLAYBACK
        """
        with self._lock:
            session_id = self._current_dictation_session
            target_ip = self.discovery_service.peer_address

            # 1. Stop Mac microphone sender
            if target_ip and session_id:
                stop_msg = {
                    "version": 1,
                    "role": HostRole.WINDOWS.value,
                    "instance_id": self.discovery_service.instance_id,
                    "type": "DICTATION_STOP",
                    "session_id": session_id,
                }
                self.discovery_service.send_control_message(target_ip, stop_msg)

            # 2. Stop Windows microphone receiver
            if self._microphone_child_pid is not None:
                self.process_runner.stop_process(self._microphone_child_pid)
                self._microphone_child_pid = None

            # 3. Microphone children must be 0
            self._microphone_desired = False
            self._current_dictation_session = None
            self._microphone_path_state = (
                PathState.READY if self.pack43_resolver.is_cached_available is True else PathState.IDLE
            )

            # 4. Restore Windows → Mac speaker & return to PLAYBACK
            self._mode = "PLAYBACK"
            self._reconcile_speaker()
            return True

    def stop(self) -> bool:

        """Idempotently stops speaker and microphone pipelines and sets STOPPED_BY_USER."""
        with self._lock:
            self._desired_state = DesiredState.STOPPED_BY_USER
            self._persist_desired_state(DesiredState.STOPPED_BY_USER)
            self._mode = "PLAYBACK"
            self._current_dictation_session = None
            self._dictation_ack_event.clear()

            # Stop owned speaker pipeline child
            if self._speaker_child_pid is not None:
                self.process_runner.stop_process(self._speaker_child_pid)
                self._speaker_child_pid = None
            self._active_peer_address = None
            self._active_local_bind = None
            self._speaker_path_state = PathState.STOPPED

            # Stop owned microphone pipeline child and reset microphone intent
            self._microphone_desired = False
            self._pack43_recovery_start_time = None
            self._pack43_recovery_attempts = 0
            self._last_pack43_attempt_time = 0.0
            if self._microphone_child_pid is not None:
                self.process_runner.stop_process(self._microphone_child_pid)
                self._microphone_child_pid = None
            self._microphone_path_state = PathState.STOPPED

            # Stop discovery
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
                self._mode = "PLAYBACK"
                self._microphone_desired = False
                self._current_dictation_session = None
                self._dictation_ack_event.clear()
                self._pack43_recovery_start_time = None
                self._pack43_recovery_attempts = 0
                self._last_pack43_attempt_time = 0.0
                self._controller_state = LifecycleState.STARTING
                self._speaker_path_state = PathState.IDLE
                self._last_actionable_error = None
                try:
                    self.discovery_service.start()
                except Exception as exc:
                    self._last_actionable_error = f"Discovery start failed: {exc}"
                    self._controller_state = LifecycleState.ERROR
                self.reconcile()
            else:
                self._mode = "PLAYBACK"
                self._microphone_desired = False
                self._current_dictation_session = None
                self._dictation_ack_event.clear()
                self._pack43_recovery_start_time = None
                self._pack43_recovery_attempts = 0
                self._last_pack43_attempt_time = 0.0
                self._controller_state = LifecycleState.STOPPED
                self._speaker_path_state = PathState.STOPPED
                self._microphone_path_state = PathState.STOPPED

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

            # Stop owned speaker pipeline child
            if self._speaker_child_pid is not None:
                self.process_runner.stop_process(self._speaker_child_pid)
                self._speaker_child_pid = None
            self._active_peer_address = None
            self._active_local_bind = None
            self._speaker_path_state = PathState.STOPPED

            # Stop owned microphone pipeline child
            self._microphone_desired = False
            if self._microphone_child_pid is not None:
                self.process_runner.stop_process(self._microphone_child_pid)
                self._microphone_child_pid = None

            self._microphone_path_state = PathState.STOPPED

            # Stop discovery
            self.discovery_service.stop()

            # Stop IPC server and release lock
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
                if self._speaker_child_pid is not None:
                    self.process_runner.stop_process(self._speaker_child_pid)
                    self._speaker_child_pid = None
                if self._microphone_child_pid is not None:
                    self.process_runner.stop_process(self._microphone_child_pid)
                    self._microphone_child_pid = None
                self._speaker_path_state = PathState.STOPPED
                self._microphone_path_state = PathState.STOPPED
                self._controller_state = LifecycleState.STOPPED
                return

            # 2. Peer Ambiguous -> Stop both speaker and microphone
            if getattr(self.discovery_service, "is_ambiguous", False):
                self._last_actionable_error = "Multiple opposite-role responders discovered; manual peer selection required"
                self._controller_state = LifecycleState.AMBIGUOUS_PEER
                if self._speaker_child_pid is not None:
                    self.process_runner.stop_process(self._speaker_child_pid)
                    self._speaker_child_pid = None
                    self._active_peer_address = None
                    self._active_local_bind = None
                    self._speaker_path_state = PathState.IDLE
                if self._microphone_child_pid is not None:
                    self.process_runner.stop_process(self._microphone_child_pid)
                    self._microphone_child_pid = None
                    self._microphone_path_state = PathState.IDLE
                return

            # 3. GStreamer binary missing -> Fatal error for pipeline
            if not self.pipeline_builder.is_gstreamer_available():
                self._last_actionable_error = "GStreamer binary not found at configured path"
                self._controller_state = LifecycleState.ERROR
                self._speaker_path_state = PathState.FAILED
                if self._microphone_child_pid is not None:
                    self.process_runner.stop_process(self._microphone_child_pid)
                    self._microphone_child_pid = None
                    self._microphone_path_state = PathState.IDLE
                return

            # 4. Peer Unavailable -> Stop both pipelines, enter DISCOVERING
            if not self.discovery_service.peer_available:
                self._controller_state = LifecycleState.DISCOVERING
                self.discovery_service.broadcast_hello()
                disc_err = getattr(self.discovery_service, "last_enumeration_error", None)
                if disc_err:
                    self._last_actionable_error = disc_err
                    self._controller_state = LifecycleState.ERROR
                if self._speaker_child_pid is not None:
                    self.process_runner.stop_process(self._speaker_child_pid)
                    self._speaker_child_pid = None
                    self._active_peer_address = None
                    self._active_local_bind = None
                    self._speaker_path_state = PathState.IDLE
                if self._microphone_child_pid is not None:
                    self.process_runner.stop_process(self._microphone_child_pid)
                    self._microphone_child_pid = None
                    self._microphone_path_state = PathState.IDLE
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

