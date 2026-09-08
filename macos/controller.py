"""Production Audio Bridge Controller for macOS.

Implements the single-instance controller lifecycle:
- Start: Idempotently enables speaker receiver and reconciles.
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
from typing import Any, Callable, Optional

from bridge_core.contract import (
    DEFAULT_LOCAL_IPC_PORT,
    DEFAULT_MIC_RTP_PORT,
    DEFAULT_SINGLETON_PORT,
    DEFAULT_SPEAKER_RTP_PORT,
    ControllerStatus,
    DesiredState,
    HostRole,
    LifecycleState,
    PathState,
)
from bridge_core.peer_discovery import PeerDiscoveryService
from bridge_core.preflight import check_runtime_dependencies
from bridge_core.process_runner import ProcessRunner

from .device_resolver import MacCoreAudioDeviceResolver, check_microphone_authorization
from .microphone_sender import MicrophoneSenderBuilder
from .process_runner import MacOwnedProcessRunner
from .speaker_receiver import SpeakerReceiverBuilder

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = os.path.expanduser(
    "~/Library/Application Support/desk-audio-bridge/controller_state.json"
)


class SingleInstanceLock:
    """Guarantees controller singleton execution per machine via local UDP socket bind."""

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
    """TCP server running on 127.0.0.1:50106 to serve CLI requests on macOS."""

    def __init__(self, controller: "MacBridgeController", port: int = DEFAULT_LOCAL_IPC_PORT):
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
                target=self._serve_loop, daemon=True, name="MacLocalControlServer"
            )
            self._thread.start()
            return True
        except Exception as exc:
            logger.debug("Failed to start MacLocalControlServer on port %d: %s", self.port, exc)
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
                    res = {
                        "success": success,
                        "desired_state": self.controller.get_status().desired_state,
                    }
                elif cmd == "stop":
                    success = self.controller.stop()
                    res = {
                        "success": success,
                        "desired_state": self.controller.get_status().desired_state,
                    }
                elif cmd == "reconcile":
                    self.controller.reconcile()
                    res = {"success": True}
                elif cmd == "status":
                    res = self.controller.get_status().to_dict()
                elif cmd == "mic-enable":
                    success = self.controller.set_microphone_enabled(True)
                    res = {
                        "success": success,
                        "microphone_path_state": self.controller.get_status().microphone_path_state,
                    }
                elif cmd == "mic-disable":
                    success = self.controller.set_microphone_enabled(False)
                    res = {
                        "success": success,
                        "microphone_path_state": self.controller.get_status().microphone_path_state,
                    }
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


class MacBridgeController:
    """The central macOS controller for desk-audio-bridge."""

    def __init__(
        self,
        state_file: str = DEFAULT_STATE_FILE,
        process_runner: Optional[ProcessRunner] = None,
        device_resolver: Optional[MacCoreAudioDeviceResolver] = None,
        pipeline_builder: Optional[SpeakerReceiverBuilder] = None,
        discovery_service: Optional[PeerDiscoveryService] = None,
        microphone_sender_builder: Optional[MicrophoneSenderBuilder] = None,
        lock_port: int = DEFAULT_SINGLETON_PORT,
        ipc_port: int = DEFAULT_LOCAL_IPC_PORT,
        mic_permission_probe: Optional[Any] = None,
    ):
        self.state_file = state_file
        self.process_runner = process_runner or MacOwnedProcessRunner()
        self.device_resolver = device_resolver or MacCoreAudioDeviceResolver()
        self.pipeline_builder = pipeline_builder or SpeakerReceiverBuilder()
        self.microphone_sender_builder = (
            microphone_sender_builder or MicrophoneSenderBuilder()
        )
        self.mic_permission_probe = mic_permission_probe or check_microphone_authorization
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

        # Microphone path state & desired state
        self._microphone_desired: bool = False
        self._microphone_path_state = PathState.IDLE
        self._microphone_child_pid: Optional[int] = None
        self._last_actionable_microphone_error: Optional[str] = None
        self._lock = threading.RLock()

        # Wire discovery service for HostRole.MACOS
        self.discovery_service = discovery_service or PeerDiscoveryService(
            local_role=HostRole.MACOS,
            instance_id=f"mac-{os.getpid()}-{int(time.time())}",
            on_peer_discovered=self._on_peer_discovered,
        )

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

    def start_host(self) -> bool:
        """Starts the controller host (used by LaunchAgent / daemon).

        Acquires singleton lock, preflights runtime dependencies, starts IPC server,
        starts peer discovery service, and reconciles according to the PERSISTED desired state.
        DOES NOT mutate or overwrite persisted desired state.
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
                    logger.warning("Controller host start rejected: another process holds the singleton lock")
                    return False

            # Reload persisted desired state to ensure consistency
            self._desired_state = self._load_persisted_desired_state()
            if self._desired_state == DesiredState.STOPPED_BY_USER:
                self._controller_state = LifecycleState.STOPPED
            else:
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

    def start(self) -> bool:
        """Explicit user start intent: enables the controller and triggers reconcile.
        
        Persists DesiredState.ENABLED.
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
        """Explicit desired-state control seam for macOS microphone capability."""
        with self._lock:
            self._microphone_desired = enabled
            if not enabled:
                if self._microphone_child_pid is not None:
                    self.process_runner.stop_process(self._microphone_child_pid)
                    self._microphone_child_pid = None
                self._microphone_path_state = PathState.STOPPED
                self._last_actionable_microphone_error = None
                return True

            # When enabling microphone, run reconcile
            self.reconcile()
            return self._microphone_path_state == PathState.RUNNING

    def stop(self) -> bool:
        """Idempotently stops speaker and microphone pipelines and persists STOPPED_BY_USER."""
        with self._lock:
            self._desired_state = DesiredState.STOPPED_BY_USER
            self._persist_desired_state(DesiredState.STOPPED_BY_USER)

            # Stop owned speaker pipeline child
            if self._speaker_child_pid is not None:
                self.process_runner.stop_process(self._speaker_child_pid)
                self._speaker_child_pid = None
            self._active_peer_address = None
            self._active_local_bind = None
            self._speaker_path_state = PathState.STOPPED

            # Stop owned microphone pipeline child
            if self._microphone_child_pid is not None:
                self.process_runner.stop_process(self._microphone_child_pid)
                self._microphone_child_pid = None
            self._microphone_path_state = PathState.STOPPED

            # Stop discovery
            self.discovery_service.stop()

            self._controller_state = LifecycleState.STOPPED
            return True

    def shutdown_host(self) -> None:
        """Shuts down the controller host process without mutating persisted user desired state."""
        with self._lock:
            # Stop owned speaker pipeline child
            if self._speaker_child_pid is not None:
                self.process_runner.stop_process(self._speaker_child_pid)
                self._speaker_child_pid = None
            self._active_peer_address = None
            self._active_local_bind = None
            self._speaker_path_state = PathState.STOPPED

            # Stop owned microphone pipeline child
            if self._microphone_child_pid is not None:
                self.process_runner.stop_process(self._microphone_child_pid)
                self._microphone_child_pid = None
            self._microphone_path_state = PathState.STOPPED

            # Stop discovery
            self.discovery_service.stop()

            # Stop IPC server & release singleton lock
            self._ipc_server.stop()
            self._singleton_lock.release()
            self._controller_state = LifecycleState.STOPPED

    def shutdown(self) -> None:
        """Full shutdown of controller host. Does NOT mutate persisted user desired state."""
        self.shutdown_host()

    def get_status(self) -> ControllerStatus:
        """Pure read-only query of controller status without side-effects."""
        with self._lock:
            owned_count = 0
            if (
                self._speaker_child_pid
                and self.process_runner.is_running(self._speaker_child_pid)
            ):
                owned_count += 1
            if (
                self._microphone_child_pid
                and self.process_runner.is_running(self._microphone_child_pid)
            ):
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

            # Determine honest microphone path state reflection:
            # - If currently RUNNING, FAILED, UNAVAILABLE, or explicitly STOPPED, keep as-is
            # - If not running:
            #   * if STOPPED by user: STOPPED
            #   * otherwise: IDLE
            mic_state = self._microphone_path_state
            if mic_state not in (
                PathState.RUNNING,
                PathState.FAILED,
                PathState.UNAVAILABLE,
                PathState.STOPPED,
            ):
                if self._desired_state == DesiredState.STOPPED_BY_USER:
                    mic_state = PathState.STOPPED
                else:
                    mic_state = PathState.IDLE

            return ControllerStatus(
                controller_state=self._controller_state.value,
                desired_state=self._desired_state.value,
                role=HostRole.MACOS.value,
                peer_available=self.discovery_service.peer_available,
                peer_address=peer_addr,
                local_bind_address=local_bind,
                speaker_path_state=self._speaker_path_state.value,
                speaker_target_port=DEFAULT_SPEAKER_RTP_PORT,
                last_actionable_error=last_err,
                owned_children_count=owned_count,
                owner_pid=os.getpid() if self._singleton_lock.is_held else None,
                microphone_path_state=mic_state.value,
                microphone_port=DEFAULT_MIC_RTP_PORT,
                pack43_available=None,
                last_actionable_microphone_error=self._last_actionable_microphone_error,
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
                self._last_actionable_error = (
                    "Multiple opposite-role responders discovered; manual peer selection required"
                )
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
        """Idempotently reconciles the macOS speaker receiver path."""
        # Resolve CoreAudio built-in speaker output device
        dev = self.device_resolver.resolve_builtin_speaker_device()
        if not dev or dev.device_id is None:
            self._last_actionable_error = "CoreAudio built-in speaker output resolution failed"
            self._controller_state = LifecycleState.ERROR
            self._speaker_path_state = PathState.FAILED
            return

        # Verify if speaker pipeline already running
        if self._speaker_child_pid and self.process_runner.is_running(self._speaker_child_pid):
            self._controller_state = LifecycleState.ACTIVE
            self._speaker_path_state = PathState.RUNNING
            return

        # Determine local bind address
        local_bind = self.discovery_service.local_bind_address
        if not local_bind or local_bind == "0.0.0.0":
            self._last_actionable_error = "Valid local bind address could not be resolved from peer route"
            self._controller_state = LifecycleState.ERROR
            self._speaker_path_state = PathState.FAILED
            return

        # Build GStreamer receiver command
        cmd = self.pipeline_builder.build_receiver_command(
            local_bind_ip=local_bind,
            local_port=DEFAULT_SPEAKER_RTP_PORT,
            device_id=dev.device_id,
        )

        try:
            pid = self.process_runner.start_process(cmd)
            self._speaker_child_pid = pid
            self._active_peer_address = self.discovery_service.peer_address
            self._active_local_bind = local_bind
            self._speaker_path_state = PathState.RUNNING
            self._controller_state = LifecycleState.ACTIVE
            self._last_actionable_error = None
        except Exception as exc:
            self._last_actionable_error = f"Failed to start speaker receiver: {exc}"
            self._active_peer_address = None
            self._active_local_bind = None
            self._speaker_path_state = PathState.FAILED
            self._controller_state = LifecycleState.ERROR

    def _reconcile_microphone(self) -> None:
        """Idempotently reconciles the macOS microphone sender path."""
        if not self._microphone_desired:
            if self._microphone_child_pid is not None:
                self.process_runner.stop_process(self._microphone_child_pid)
                self._microphone_child_pid = None
            if self._desired_state == DesiredState.STOPPED_BY_USER:
                self._microphone_path_state = PathState.STOPPED
            else:
                self._microphone_path_state = PathState.IDLE
            return

        # Microphone is desired: check if pipeline already running
        if self._microphone_child_pid is not None:
            if self.process_runner.is_running(self._microphone_child_pid):
                self._microphone_path_state = PathState.RUNNING
                return
            # Child exited unexpectedly
            self._microphone_child_pid = None

        # Check macOS microphone permission authorization status
        if self.mic_permission_probe:
            try:
                auth_status = self.mic_permission_probe()
                if auth_status in (1, 2):  # 1: Restricted, 2: Denied
                    self._last_actionable_microphone_error = (
                        "macOS Microphone permission denied: please grant permission in "
                        "System Settings -> Privacy & Security -> Microphone"
                    )
                    self._microphone_path_state = PathState.FAILED
                    return
            except Exception as exc:
                logger.debug("Microphone authorization check probe error: %s", exc)

        # Check GStreamer availability for sender
        if not self.microphone_sender_builder.is_gstreamer_available():
            self._last_actionable_microphone_error = (
                "GStreamer binary not found for microphone sender"
            )
            self._microphone_path_state = PathState.FAILED
            return

        # Resolve CoreAudio built-in microphone device
        dev = self.device_resolver.resolve_builtin_microphone_device()
        if not dev or dev.device_id is None:
            self._last_actionable_microphone_error = (
                "CoreAudio built-in microphone resolution failed"
            )
            self._microphone_path_state = PathState.UNAVAILABLE
            return

        # Determine target host and local bind
        target_ip = self.discovery_service.peer_address
        local_bind = self.discovery_service.local_bind_address

        cmd = self.microphone_sender_builder.build_sender_command(
            target_host=target_ip,
            target_port=DEFAULT_MIC_RTP_PORT,
            device_id=dev.device_id,
            local_bind_ip=local_bind,
        )

        try:
            pid = self.process_runner.start_process(cmd)
            self._microphone_child_pid = pid
            self._microphone_path_state = PathState.RUNNING
            self._last_actionable_microphone_error = None
        except Exception as exc:
            self._last_actionable_microphone_error = (
                f"Failed to start microphone sender: {exc}"
            )
            self._microphone_path_state = PathState.FAILED

    def _on_peer_discovered(self, peer_ip: str, local_ip: str, peer_port: int, peer_inst: str) -> None:
        with self._lock:
            if self._desired_state == DesiredState.ENABLED:
                self.reconcile()
