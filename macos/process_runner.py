"""Process runner and ownership supervisor for macOS.

Enforces strict process ownership using POSIX process sessions and local tracking:
- Any spawned pipeline child is started in its own process session (start_new_session=True).
- ONLY project-owned children explicitly launched by this runner are tracked and terminated.
- Termination is performed strictly via stored Popen references / PID / PGID.
- Does NOT scan global process tables.
- Does NOT perform broad/global process kills by name or killall.
"""

import logging
import os
import signal
import subprocess
from typing import Dict, List, Optional

from bridge_core.process_runner import ProcessRunner

logger = logging.getLogger(__name__)


class MacOwnedProcessRunner(ProcessRunner):
    """macOS-specific owned process supervisor backed by explicit Popen tracking."""

    def __init__(self):
        self._owned_processes: Dict[int, subprocess.Popen] = {}
        self._owned_metadata: Dict[int, dict] = {}

    def start_process(self, cmd: List[str]) -> int:
        """Starts a child process in a dedicated session and tracks ownership."""
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid = proc.pid
        self._owned_processes[pid] = proc
        create_time = None
        try:
            import psutil
            create_time = psutil.Process(pid).create_time()
        except Exception:
            pass
        self._owned_metadata[pid] = {
            "pid": pid,
            "cmd": list(cmd),
            "create_time": create_time,
        }
        logger.info("Started owned child process [PID %d]: %s", pid, cmd[0])
        return pid

    def stop_process(self, pid: int) -> None:
        """Stops ONLY the specific owned process by PID / PGID."""
        proc = self._owned_processes.pop(pid, None)
        self._owned_metadata.pop(pid, None)
        if not proc:
            logger.debug("PID %d is not in owned processes map; ignoring", pid)
            return

        # Attempt graceful termination via PGID / process
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass

        logger.info("Stopped owned child process [PID %d]", pid)

    def is_running(self, pid: int) -> bool:
        """Checks whether the owned process is still running."""
        proc = self._owned_processes.get(pid)
        if not proc:
            return False
        return proc.poll() is None

    def get_child_metadata(self, pid: int) -> Optional[dict]:
        """Returns startup metadata for a tracked child process."""
        return self._owned_metadata.get(pid)

    def stop_all_owned(self) -> None:
        """Stops all processes owned by this controller instance."""
        pids = list(self._owned_processes.keys())
        for pid in pids:
            self.stop_process(pid)

    def __del__(self):
        self.stop_all_owned()

