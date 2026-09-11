"""macOS Process Ownership Journal and Orphan Recovery.

Manages persistent tracking of spawned GStreamer child processes (speaker / microphone),
ensuring safe death verification, atomic journal recordkeeping, and fail-closed orphan recovery.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

if TYPE_CHECKING:
    from .controller import MacBridgeController

logger = logging.getLogger(__name__)


class OwnershipJournalManager:
    """Encapsulates process ownership journal lifecycle, tracking, and orphan recovery."""

    def __init__(self, controller: MacBridgeController):
        self.controller = controller

    @property
    def journal_file(self) -> str:
        return self.controller.journal_file

    def load_journal(self) -> Tuple[Optional[dict], Optional[str]]:
        """Loads ownership journal."""
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

    def write_journal(self, data: dict) -> bool:
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

    def clear_journal(self) -> bool:
        """Removes the ownership journal file cleanly."""
        try:
            if os.path.exists(self.journal_file):
                os.remove(self.journal_file)
            return True
        except Exception as exc:
            logger.warning("Failed to remove ownership journal %s: %s", self.journal_file, exc)
            return False

    def record_child_started(self, role: str, pid: int, cmd: List[str], port: int) -> bool:
        """Records child startup in journal with strict create_time verification."""
        create_time = None
        try:
            import psutil
            create_time = psutil.Process(pid).create_time()
        except Exception:
            pass

        if create_time is None and hasattr(self.controller.process_runner, "get_child_metadata"):
            meta = self.controller.process_runner.get_child_metadata(pid)
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

        journal, err = self.load_journal()
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
        return self.write_journal(journal)

    def record_child_stopped(self, role: str) -> bool:
        """Removes a stopped child from journal only after its death is confirmed."""
        journal, err = self.load_journal()
        if err:
            logger.error("Cannot record child stopped because journal is corrupt or unreadable: %s", err)
            return False
        if not journal:
            return True
        if "children" in journal:
            journal["children"].pop(role, None)
            if not journal["children"]:
                return self.clear_journal()
            else:
                return self.write_journal(journal)
        return True

    def verify_child_identity(self, pid: int, child_info: dict) -> Tuple[bool, Optional[str]]:
        """Verifies that a running process strictly matches recorded desk-audio-bridge GStreamer child."""
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

    def terminate_stale_child(self, pid: int, child_info: dict) -> bool:
        """Terminates stale verified child and confirms death."""
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

        try:
            import psutil
            if not psutil.pid_exists(pid):
                return True
            proc = psutil.Process(pid)
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return True
            exp_time = child_info.get("create_time")
            if exp_time is not None and abs(proc.create_time() - exp_time) > 1.0:
                return True
            return False
        except (psutil.NoSuchProcess, ProcessLookupError):
            return True
        except Exception as exc:
            logger.warning("Uncertainty during death confirmation for PID %d (%s); failing closed", pid, exc)
            return False

    def recover_stale_owned_children(self) -> Tuple[bool, Optional[str]]:
        """Recovers and terminates verified orphaned child processes left by previous abnormal controller death."""
        journal, err = self.load_journal()
        if err:
            err_msg = f"Recovery failed-closed: {err}"
            logger.error(err_msg)
            return False, err_msg

        if not journal:
            return True, None

        prior_owner_pid = journal.get("owner_pid")
        prior_owner_time = journal.get("owner_create_time")
        current_pid = os.getpid()

        if prior_owner_pid == current_pid:
            return True, None

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
                logger.debug("Recorded child [PID %d] no longer exists; safely resolved", c_pid)
                continue

            try:
                p_child = psutil.Process(c_pid)
                rec_time = child_info.get("create_time")
                if rec_time is not None and abs(p_child.create_time() - rec_time) > 1.0:
                    logger.info("Recorded child [PID %d] create_time mismatch; process is gone (PID reused); safely resolved", c_pid)
                    continue
            except Exception:
                continue

            verified, reason = self.verify_child_identity(c_pid, child_info)
            if verified:
                logger.info("Recovering verified orphaned %s child [PID %d] from crashed controller", role, c_pid)
                death_confirmed = self.terminate_stale_child(c_pid, child_info)
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
            journal["children"] = retained_children
            self.write_journal(journal)
            return False, first_failure_reason

        if not self.clear_journal():
            err_msg = "All children resolved but failed to remove ownership journal file; failing closed"
            logger.error(err_msg)
            return False, err_msg
        return True, None
