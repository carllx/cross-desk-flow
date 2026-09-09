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

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = os.environ.get(
    "DESK_AUDIO_BRIDGE_STATE_FILE",
    os.path.expanduser("~/Library/Application Support/desk-audio-bridge/controller_state.json"),
)
DEFAULT_JOURNAL_FILE = os.path.expanduser(
    "~/Library/Application Support/desk-audio-bridge/ownership_journal.json"
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

        self._desired_state = self._load_persisted_desired_state()
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
        """Loads ownership journal.
        
        Returns:
            (journal_dict, None) if journal exists and is valid.
            (None, None) if journal file is absent.
            (None, error_str) if journal file exists but is corrupt / unreadable.
        """
        if not os.path.exists(self.journal_file):
            return None, None
        try:
            with open(self.journal_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    return None, f"Corrupt ownership journal: expected JSON object, got {type(data).__name__}"
                return data, None
        except Exception as exc:
            err_msg = f"Corrupt or unreadable ownership journal at {self.journal_file}: {exc}"
            logger.error(err_msg)
            return None, err_msg

    def _write_ownership_journal(self, data: dict) -> bool:
        """Atomically writes ownership journal. Raises or returns False on failure."""
        try:
            os.makedirs(os.path.dirname(self.journal_file), exist_ok=True)
            tmp_path = f"{self.journal_file}.tmp.{os.getpid()}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp_path, self.journal_file)
            return True
        except Exception as exc:
            logger.error("Failed to write ownership journal %s: %s", self.journal_file, exc)
            return False

    def _clear_ownership_journal(self) -> bool:
        """Removes the ownership journal file cleanly."""
        try:
            if os.path.exists(self.journal_file):
                os.remove(self.journal_file)
            return True
        except Exception as exc:
            logger.warning("Failed to remove ownership journal %s: %s", self.journal_file, exc)
            return False

    def _stop_child(self, role: str) -> bool:
        """Stops an owned child process, confirms its death, and updates journal.
        
        Returns True if process is confirmed dead and journal updated.
        Returns False if termination could not be confirmed; retains PID and journal entry.
        """
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
        else:
            self._microphone_child_pid = None
        return True

    def _record_child_started(self, role: str, pid: int, cmd: List[str], port: int) -> bool:
        """Records child startup in journal with strict create_time verification.
        
        Returns True on success; False if create_time cannot be obtained or journal write fails.
        """
        create_time = None
        try:
            import psutil
            create_time = psutil.Process(pid).create_time()
        except Exception:
            pass

        # Also check runner metadata if psutil failed
        if create_time is None and hasattr(self.process_runner, "get_child_metadata"):
            meta = self.process_runner.get_child_metadata(pid)
            if meta:
                create_time = meta.get("create_time")

        if create_time is None:
            logger.error("Failed to acquire reliable create_time for spawned %s child [PID %d]; refusing journal record", role, pid)
            return False

        owner_create_time = None
        try:
            import psutil
            owner_create_time = psutil.Process(os.getpid()).create_time()
        except Exception:
            pass

        journal, err = self._load_ownership_journal()
        if err:
            logger.error("Cannot record child started because journal is corrupt: %s", err)
            return False

        if not journal:
            journal = {
                "version": 1,
                "owner_pid": os.getpid(),
                "owner_create_time": owner_create_time,
                "children": {},
            }
        journal["owner_pid"] = os.getpid()
        journal["owner_create_time"] = owner_create_time
        if "children" not in journal:
            journal["children"] = {}

        journal["children"][role] = {
            "role": role,
            "pid": pid,
            "create_time": create_time,
            "port": port,
            "cmd_tokens": cmd,
        }
        return self._write_ownership_journal(journal)

    def _record_child_stopped(self, role: str) -> bool:
        """Removes a stopped child from journal only after its death is confirmed.
        
        Clears the journal file ONLY when no children remain.
        Fails closed (returns False) if journal cannot be read due to corruption/error.
        """
        journal, err = self._load_ownership_journal()
        if err:
            logger.error("Cannot record child stopped because journal is corrupt or unreadable: %s", err)
            return False
        if not journal:
            return True
        if "children" in journal:
            journal["children"].pop(role, None)
            if not journal["children"]:
                return self._clear_ownership_journal()
            else:
                return self._write_ownership_journal(journal)
        return True

    def _verify_child_identity(self, pid: int, child_info: dict) -> Tuple[bool, Optional[str]]:
        """Verifies that a running process strictly matches recorded desk-audio-bridge GStreamer child.

        Returns (is_verified, reason_if_not)
        """
        try:
            import psutil
            if not psutil.pid_exists(pid):
                return False, "Process does not exist"
            proc = psutil.Process(pid)
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return False, "Process is not running or is zombie"

            recorded_time = child_info.get("create_time")
            if recorded_time is not None:
                if abs(proc.create_time() - recorded_time) > 1.0:
                    return False, f"create_time mismatch (expected {recorded_time}, got {proc.create_time()})"

            actual_cmd = proc.cmdline()
            if not actual_cmd:
                return False, "Process cmdline is empty"

            exe_name = os.path.basename(actual_cmd[0])
            if exe_name != "gst-launch-1.0":
                return False, f"Executable is {exe_name}, expected gst-launch-1.0"

            role = child_info.get("role")
            port = child_info.get("port")
            cmd_str = " ".join(actual_cmd)

            if port is not None and str(port) not in cmd_str:
                return False, f"Missing expected port {port} in cmdline"

            if role == "speaker":
                if "udpsrc" not in cmd_str:
                    return False, "Missing expected speaker pipeline token udpsrc"
            elif role == "microphone":
                if "osxaudiosrc" not in cmd_str:
                    return False, "Missing expected microphone pipeline token osxaudiosrc"

            return True, None
        except Exception as exc:
            return False, f"Verification error: {exc}"

    def _terminate_stale_child(self, pid: int, child_info: dict) -> bool:
        """Terminates stale verified child and confirms death.
        
        Returns True ONLY if process is confirmed completely dead / PID reused.
        Returns False on any inspection error, permission failure, or if process remains alive.
        """
        try:
            import psutil
            if not psutil.pid_exists(pid):
                return True
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                return True
            except Exception:
                pass

        start_t = time.time()
        while time.time() - start_t < 2.0:
            try:
                import psutil
                if not psutil.pid_exists(pid) or not psutil.Process(pid).is_running():
                    return True
            except (psutil.NoSuchProcess, ProcessLookupError):
                return True
            except Exception:
                # Any inspection exception during wait -> cannot confirm death yet, continue loop
                pass
            time.sleep(0.05)

        try:
            import psutil
            if psutil.pid_exists(pid) and psutil.Process(pid).is_running():
                try:
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    return True
                except Exception:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        return True
                    except Exception:
                        pass
                start_kill_t = time.time()
                while time.time() - start_kill_t < 1.0:
                    try:
                        if not psutil.pid_exists(pid) or not psutil.Process(pid).is_running():
                            return True
                    except (psutil.NoSuchProcess, ProcessLookupError):
                        return True
                    except Exception:
                        pass
                    time.sleep(0.05)
        except (psutil.NoSuchProcess, ProcessLookupError):
            return True
        except Exception:
            pass

        # Final death check: fail-closed on any error or uncertainty
        try:
            import psutil
            if not psutil.pid_exists(pid):
                return True
            proc = psutil.Process(pid)
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return True
            # Also check create_time in case of instantaneous PID reuse
            exp_time = child_info.get("create_time")
            if exp_time is not None and abs(proc.create_time() - exp_time) > 1.0:
                return True
            # Process is still running and is the same process
            return False
        except (psutil.NoSuchProcess, ProcessLookupError):
            return True
        except Exception as exc:
            # Inspection exception / permission failure -> fail closed!
            logger.warning("Uncertainty during death confirmation for PID %d (%s); failing closed", pid, exc)
            return False

    def _recover_stale_owned_children(self) -> Tuple[bool, Optional[str]]:
        """Recovers and terminates verified orphaned child processes left by previous abnormal controller death.
        
        Returns:
            (True, None) if recovery completed cleanly (or no journal present).
            (False, error_str) if recovery failed closed due to corrupt journal, unverified live PID, or termination failure.
        """
        journal, err = self._load_ownership_journal()
        if err:
            err_msg = f"Recovery failed-closed: {err}"
            logger.error(err_msg)
            return False, err_msg

        if not journal:
            return True, None

        prior_owner_pid = journal.get("owner_pid")
        prior_owner_time = journal.get("owner_create_time")
        current_pid = os.getpid()

        # If recorded owner is still the current process, nothing to recover
        if prior_owner_pid == current_pid:
            return True, None

        # Verify prior owner is no longer the same live process
        if prior_owner_pid and isinstance(prior_owner_pid, int):
            try:
                import psutil
                if psutil.pid_exists(prior_owner_pid):
                    p_owner = psutil.Process(prior_owner_pid)
                    if p_owner.is_running() and p_owner.status() != psutil.STATUS_ZOMBIE:
                        if prior_owner_time is None or abs(p_owner.create_time() - prior_owner_time) <= 1.0:
                            err_msg = f"Prior controller owner PID {prior_owner_pid} is still alive; cannot recover"
                            logger.warning(err_msg)
                            return False, err_msg
            except Exception:
                pass

        children = journal.get("children", {})
        retained_children = {}
        recovery_failed = False
        first_failure_reason = None

        for role, child_info in list(children.items()):
            if not isinstance(child_info, dict):
                retained_children[role] = child_info
                recovery_failed = True
                first_failure_reason = f"Malformed child info for role {role}"
                continue

            c_pid = child_info.get("pid")
            if not c_pid or not isinstance(c_pid, int):
                retained_children[role] = child_info
                recovery_failed = True
                first_failure_reason = f"Invalid PID in child info for role {role}"
                continue

            import psutil
            if not psutil.pid_exists(c_pid):
                # Process no longer exists: safely resolved
                logger.debug("Recorded child [PID %d] no longer exists; safely resolved", c_pid)
                continue

            # PID exists: check if create_time mismatch proves PID reuse
            try:
                p_child = psutil.Process(c_pid)
                rec_time = child_info.get("create_time")
                if rec_time is not None and abs(p_child.create_time() - rec_time) > 1.0:
                    # Original process is dead; PID has been reused by unrelated process
                    logger.info("Recorded child [PID %d] create_time mismatch; process is gone (PID reused); safely resolved", c_pid)
                    continue
            except Exception:
                # Process disappeared
                continue

            # Process still exists with same or unconfirmed identity: verify ownership
            verified, reason = self._verify_child_identity(c_pid, child_info)
            if verified:
                logger.info("Recovering verified orphaned %s child [PID %d] from crashed controller", role, c_pid)
                death_confirmed = self._terminate_stale_child(c_pid, child_info)
                if death_confirmed:
                    logger.info("Confirmed death of orphaned %s child [PID %d]", role, c_pid)
                else:
                    err_msg = f"Failed to terminate orphaned {role} child [PID {c_pid}]; still alive"
                    logger.error(err_msg)
                    retained_children[role] = child_info
                    recovery_failed = True
                    if not first_failure_reason:
                        first_failure_reason = err_msg
            else:
                err_msg = f"Child [PID {c_pid}] role {role} exists but failed identity verification ({reason}); retaining record"
                logger.warning(err_msg)
                retained_children[role] = child_info
                recovery_failed = True
                if not first_failure_reason:
                    first_failure_reason = err_msg

        if retained_children:
            # Update journal with remaining unresolved entries
            journal["children"] = retained_children
            self._write_ownership_journal(journal)
            return False, first_failure_reason

        # All entries safely resolved: clear journal
        if not self._clear_ownership_journal():
            err_msg = "All children resolved but failed to remove ownership journal file; failing closed"
            logger.error(err_msg)
            return False, err_msg
        return True, None


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

            # Recover any stale orphaned children left by previous crashed owner
            rec_ok, rec_err = self._recover_stale_owned_children()
            if not rec_ok:
                self._last_actionable_error = rec_err or "Stale child recovery failed-closed"
                self._controller_state = LifecycleState.ERROR
                logger.error("Controller host start rejected: %s", self._last_actionable_error)
                return False

            # Reload persisted desired state to ensure consistency
            self._desired_state = self._load_persisted_desired_state()
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
            rec_ok, rec_err = self._recover_stale_owned_children()
            if not rec_ok:
                self._last_actionable_error = rec_err or "Stale child recovery failed-closed"
                self._controller_state = LifecycleState.ERROR
                logger.error("Controller start rejected: %s", self._last_actionable_error)
                return False


            self._desired_state = DesiredState.ENABLED
            self._persist_desired_state(DesiredState.ENABLED)
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
            self._persist_desired_state(DesiredState.STOPPED_BY_USER)
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

        # Build GStreamer receiver command
        cmd = self.pipeline_builder.build_receiver_command(
            local_bind_ip=local_bind,
            local_port=DEFAULT_SPEAKER_RTP_PORT,
            device_id=dev.device_id,
        )

        try:
            pid = self.process_runner.start_process(cmd)
            self._speaker_child_pid = pid
            journal_ok = self._record_child_started("speaker", pid, cmd, DEFAULT_SPEAKER_RTP_PORT)
            if not journal_ok:
                cleanup_ok = self.process_runner.stop_process(pid)
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
