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
import signal
import socket
import sys
import threading
import time
from typing import Any, Callable, List, Optional, Tuple

from bridge_core.contract import (
    CONTROL_PROTOCOL_VERSION,
    DEFAULT_LOCAL_IPC_PORT,
    DEFAULT_MIC_RTP_PORT,
    DEFAULT_SINGLETON_PORT,
    DEFAULT_SPEAKER_RTP_PORT,
    DEFAULT_SPEAKER_INTERNAL_RTP_PORT,
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
from .dictation_responder import MacDictationResponder
from .local_control_server import LocalControlServer
from .ownership_journal import OwnershipJournalManager
from .voice_ducking import VoiceDuckingController
from .single_instance import SingleInstanceLock
from .state_store import ControllerStateStore, DEFAULT_STATE_FILE

logger = logging.getLogger(__name__)

DEFAULT_JOURNAL_FILE = os.path.expanduser(
    "~/Library/Application Support/desk-audio-bridge/ownership_journal.json"
)


class MacBridgeController:
    """The central macOS controller for desk-audio-bridge."""

    def __init__(
        self,
        state_file: str = DEFAULT_STATE_FILE,
        journal_file: Optional[str] = None,
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
        if journal_file:
            self.journal_file = journal_file
        elif state_file != DEFAULT_STATE_FILE:
            self.journal_file = f"{state_file}.journal.json"
        else:
            self.journal_file = DEFAULT_JOURNAL_FILE

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

        self._state_store = ControllerStateStore(state_file=state_file)
        self._desired_state = self._state_store.load_desired_state()
        self._controller_state = LifecycleState.STOPPED
        self._speaker_path_state = PathState.IDLE
        self._last_actionable_error: Optional[str] = None
        self._speaker_child_pid: Optional[int] = None
        self._active_peer_address: Optional[str] = None
        self._active_local_bind: Optional[str] = None

        # Microphone path state & desired state:
        # Under Issue #43 Playback/Dictation baseline, microphone defaults to False
        self._microphone_desired: bool = False
        self._microphone_path_state = PathState.IDLE
        self._microphone_child_pid: Optional[int] = None
        self._last_actionable_microphone_error: Optional[str] = None
        self.dictation_responder = MacDictationResponder(self)
        self.ownership_manager = OwnershipJournalManager(self)
        settings_path = f"{state_file}.settings.json" if state_file != DEFAULT_STATE_FILE else None
        self.voice_ducking = VoiceDuckingController(self, settings_path=settings_path)
        self._lock = threading.RLock()

        # Wire discovery service for HostRole.MACOS
        self.discovery_service = discovery_service or PeerDiscoveryService(
            local_role=HostRole.MACOS,
            instance_id=f"mac-{os.getpid()}-{int(time.time())}",
            on_peer_discovered=self._on_peer_discovered,
            on_control_message=self._on_control_message,
        )

    def _on_control_message(self, msg: dict, peer_ip: str) -> None:
        self.dictation_responder.handle_control_message(msg, peer_ip)

    @property
    def _current_dictation_session(self) -> Optional[str]:
        return self.dictation_responder.current_dictation_session

    @_current_dictation_session.setter
    def _current_dictation_session(self, val: Optional[str]) -> None:
        self.dictation_responder.current_dictation_session = val

    def _load_ownership_journal(self) -> Tuple[Optional[dict], Optional[str]]:
        return self.ownership_manager.load_ownership_journal()

    def _write_ownership_journal(self, entry: dict) -> bool:
        return self.ownership_manager.write_ownership_journal(entry)

    def _clear_ownership_journal(self) -> bool:
        return self.ownership_manager.clear_ownership_journal()

    def _record_child_started(self, role: str, pid: int, port: int, peer: Optional[str] = None) -> bool:
        return self.ownership_manager.record_child_started(role, pid, port, peer)

    def _record_child_stopped(self, role: str) -> None:
        return self.ownership_manager.record_child_stopped(role)

    def _terminate_stale_child(self, pid: int, role: str, timeout_sec: float = 3.0) -> bool:
        return self.ownership_manager.terminate_stale_child(pid, role, timeout_sec)

    def set_duck_level(self, level: int) -> int:
        """Sets the ducking level (0-100) via VoiceDuckingController."""
        return self.voice_ducking.set_duck_level(level)

    def _stop_child(self, role: str) -> bool:
        """Stops an owned child process, confirms its death, and updates journal."""
        pid = self._speaker_child_pid if role == "speaker" else self._microphone_child_pid
        if pid is None:
            return True

        stopped = self.process_runner.stop_process(pid)
        if not stopped:
            err_msg = f"Failed to confirm termination of owned {role} child [PID {pid}]; process still alive or unverified"
            logger.error(err_msg)
            if role == "speaker":
                self._last_actionable_error = err_msg
                self._speaker_path_state = PathState.FAILED
            else:
                self._last_actionable_microphone_error = err_msg
                self._microphone_path_state = PathState.FAILED
            return False

        # Termination confirmed: record in journal
        journal_updated = self._record_child_stopped(role)
        if not journal_updated:
            err_msg = f"Failed to atomically update ownership journal after stopping {role} child [PID {pid}]"
            logger.error(err_msg)
            if role == "speaker":
                self._last_actionable_error = err_msg
                self._speaker_path_state = PathState.FAILED
            else:
                self._last_actionable_microphone_error = err_msg
                self._microphone_path_state = PathState.FAILED
            return False

        if role == "speaker":
            self._speaker_child_pid = None
            self.voice_ducking.stop_relay()
        else:
            self._microphone_child_pid = None
        return True


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

            # Recover any stale orphaned children left by previous crashed owner
            rec_ok, rec_err = self.ownership_manager.recover_stale_owned_children()
            if not rec_ok:
                self._last_actionable_error = rec_err or "Stale child recovery failed-closed"
                self._controller_state = LifecycleState.ERROR
                logger.error("Controller host start rejected: %s", self._last_actionable_error)
                return False

            # Reload persisted desired state to ensure consistency
            self._desired_state = self._state_store.load_desired_state()
            if self._desired_state == DesiredState.STOPPED_BY_USER:
                self._controller_state = LifecycleState.STOPPED
                self._microphone_desired = False
            else:
                self._controller_state = LifecycleState.STARTING
                self._microphone_desired = False
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

            self.voice_ducking.start_monitoring()
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

            # Recover any stale orphaned children left by previous crashed owner
            rec_ok, rec_err = self.ownership_manager.recover_stale_owned_children()
            if not rec_ok:
                self._last_actionable_error = rec_err or "Stale child recovery failed-closed"
                self._controller_state = LifecycleState.ERROR
                logger.error("Controller start rejected: %s", self._last_actionable_error)
                return False

            self._desired_state = DesiredState.ENABLED
            self._state_store.persist_desired_state(DesiredState.ENABLED)
            self._microphone_desired = False
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

            self.voice_ducking.start_monitoring()
            self.reconcile()
            return True

    def set_microphone_enabled(self, enabled: bool) -> bool:
        """Explicit desired-state control seam for macOS microphone capability."""
        with self._lock:
            self._microphone_desired = enabled
            if not enabled:
                stopped = self._stop_child("microphone")
                if not stopped:
                    return False
                self._microphone_path_state = (
                    PathState.STOPPED
                    if self._desired_state == DesiredState.STOPPED_BY_USER
                    else PathState.IDLE
                )
                self._last_actionable_microphone_error = None
                return True

            # When enabling microphone, run reconcile
            self.reconcile()
            return self._microphone_path_state == PathState.RUNNING

    def stop(self) -> bool:
        """Idempotently stops speaker and microphone pipelines and persists STOPPED_BY_USER."""
        with self._lock:
            self._desired_state = DesiredState.STOPPED_BY_USER
            self._state_store.persist_desired_state(DesiredState.STOPPED_BY_USER)
            self._microphone_desired = False

            # Stop owned speaker pipeline child
            spk_stopped = self._stop_child("speaker")
            if spk_stopped:
                self._active_peer_address = None
                self._active_local_bind = None
                self._speaker_path_state = PathState.STOPPED

            # Stop owned microphone pipeline child
            mic_stopped = self._stop_child("microphone")
            if mic_stopped:
                self._microphone_path_state = PathState.STOPPED

            if hasattr(self.process_runner, "stop_all_owned"):
                self.process_runner.stop_all_owned()

            # Stop discovery
            self.discovery_service.stop()
            self.voice_ducking.stop_monitoring()

            if not spk_stopped or not mic_stopped:
                self._controller_state = LifecycleState.ERROR
                err_msg = "Stop failed: one or more owned child processes could not be confirmed stopped"
                if not self._last_actionable_error:
                    self._last_actionable_error = err_msg
                logger.error(err_msg)
                return False

            self._controller_state = LifecycleState.STOPPED
            return True

    def shutdown_host(self) -> None:
        """Shuts down the controller host process without mutating persisted user desired state."""
        with self._lock:
            # Stop owned speaker pipeline child
            spk_stopped = self._stop_child("speaker")
            if spk_stopped:
                self._active_peer_address = None
                self._active_local_bind = None
                self._speaker_path_state = PathState.STOPPED

            # Stop owned microphone pipeline child
            mic_stopped = self._stop_child("microphone")
            if mic_stopped:
                self._microphone_path_state = PathState.STOPPED

            if hasattr(self.process_runner, "stop_all_owned"):
                self.process_runner.stop_all_owned()

            # Stop discovery
            self.discovery_service.stop()
            self.voice_ducking.stop_monitoring()

            # Stop IPC server & release singleton lock
            self._ipc_server.stop()
            self._singleton_lock.release()
            if not spk_stopped or not mic_stopped:
                self._controller_state = LifecycleState.ERROR
            else:
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
                duck_level=self.voice_ducking.get_duck_level(),
                local_voice_active=self.voice_ducking.is_external_mic_active,
            )

    def reconcile(self) -> None:
        """Idempotently brings actual state toward desired state."""
        with self._lock:
            # Explicitly advance discovery state, pruning expired responders and handling ambiguity recovery
            if hasattr(self.discovery_service, "refresh_peer_state"):
                self.discovery_service.refresh_peer_state()

            # 1. Controller STOPPED_BY_USER -> Stop both speaker and microphone
            if self._desired_state == DesiredState.STOPPED_BY_USER:
                spk_stopped = self._stop_child("speaker")
                mic_stopped = self._stop_child("microphone")
                if spk_stopped:
                    self._active_peer_address = None
                    self._active_local_bind = None
                    self._speaker_path_state = PathState.STOPPED
                if mic_stopped:
                    self._microphone_path_state = PathState.STOPPED

                if not spk_stopped or not mic_stopped:
                    self._controller_state = LifecycleState.ERROR
                    err_msg = "Stop failed: one or more owned child processes could not be confirmed stopped or unjournaled"
                    if not self._last_actionable_error:
                        self._last_actionable_error = err_msg
                    logger.error(err_msg)
                else:
                    self._controller_state = LifecycleState.STOPPED
                return

            # 2. Peer Ambiguous -> Stop both speaker and microphone
            if getattr(self.discovery_service, "is_ambiguous", False):
                self._last_actionable_error = (
                    "Multiple opposite-role responders discovered; manual peer selection required"
                )
                self._controller_state = LifecycleState.AMBIGUOUS_PEER
                if self._stop_child("speaker"):
                    self._active_peer_address = None
                    self._active_local_bind = None
                    self._speaker_path_state = PathState.IDLE
                if self._stop_child("microphone"):
                    self._microphone_path_state = PathState.IDLE
                return

            # 3. GStreamer binary missing -> Fatal error for pipeline
            if not self.pipeline_builder.is_gstreamer_available():
                self._last_actionable_error = "GStreamer binary not found at configured path"
                self._controller_state = LifecycleState.ERROR
                self._speaker_path_state = PathState.FAILED
                if self._stop_child("microphone"):
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
                if self._stop_child("speaker"):
                    self._active_peer_address = None
                    self._active_local_bind = None
                    self._speaker_path_state = PathState.IDLE
                if self._stop_child("microphone"):
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

        # Start SpeakerVolumeRelay to intercept external RTP (5004) and forward to internal receiver (5005)
        if not self.voice_ducking.start_relay(
            bind_ip=local_bind,
            listen_port=DEFAULT_SPEAKER_RTP_PORT,
            target_port=DEFAULT_SPEAKER_INTERNAL_RTP_PORT,
        ):
            self._last_actionable_error = "Failed to start speaker volume relay proxy"
            self._controller_state = LifecycleState.ERROR
            self._speaker_path_state = PathState.FAILED
            return

        # Build GStreamer receiver command listening on internal loopback port
        cmd = self.pipeline_builder.build_receiver_command(
            local_bind_ip="127.0.0.1",
            local_port=DEFAULT_SPEAKER_INTERNAL_RTP_PORT,
            device_id=dev.device_id,
        )

        try:
            pid = self.process_runner.start_process(cmd)
            self._speaker_child_pid = pid
            journal_ok = self._record_child_started("speaker", pid, cmd, DEFAULT_SPEAKER_INTERNAL_RTP_PORT)
            if not journal_ok:
                cleanup_ok = self.process_runner.stop_process(pid)
                self.voice_ducking.stop_relay()
                if cleanup_ok:
                    self._speaker_child_pid = None
                    self._speaker_path_state = PathState.FAILED
                    self._controller_state = LifecycleState.ERROR
                    self._last_actionable_error = "Failed to atomically record speaker child in ownership journal; failing closed"
                else:
                    self._speaker_path_state = PathState.FAILED
                    self._controller_state = LifecycleState.ERROR
                    err_msg = (
                        f"Failed to atomically record speaker child in ownership journal, and "
                        f"spawned child [PID {pid}] cleanup could not be confirmed"
                    )
                    self._last_actionable_error = err_msg
                    logger.error(err_msg)
                return

            self._active_peer_address = self.discovery_service.peer_address
            self._active_local_bind = local_bind
            self._speaker_path_state = PathState.RUNNING
            self._controller_state = LifecycleState.ACTIVE
            self._last_actionable_error = None
        except Exception as exc:
            self.voice_ducking.stop_relay()
            self._last_actionable_error = f"Failed to start speaker receiver: {exc}"
            self._active_peer_address = None
            self._active_local_bind = None
            self._speaker_path_state = PathState.FAILED
            self._controller_state = LifecycleState.ERROR


    def _reconcile_microphone(self) -> None:
        """Idempotently reconciles the macOS microphone sender path."""
        if not self._microphone_desired:
            mic_stopped = self._stop_child("microphone")
            if mic_stopped:
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
            self._record_child_stopped("microphone")
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
            journal_ok = self._record_child_started("microphone", pid, cmd, DEFAULT_MIC_RTP_PORT)
            if not journal_ok:
                cleanup_ok = self.process_runner.stop_process(pid)
                if cleanup_ok:
                    self._microphone_child_pid = None
                    self._microphone_path_state = PathState.FAILED
                    self._last_actionable_microphone_error = "Failed to atomically record microphone child in ownership journal; failing closed"
                else:
                    self._microphone_path_state = PathState.FAILED
                    self._controller_state = LifecycleState.ERROR
                    err_msg = (
                        f"Failed to atomically record microphone child in ownership journal, and "
                        f"spawned child [PID {pid}] cleanup could not be confirmed"
                    )
                    self._last_actionable_microphone_error = err_msg
                    logger.error(err_msg)
                return

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
