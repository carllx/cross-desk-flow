"""macOS LaunchAgent lifecycle management for desk-audio-bridge.

Implements idempotent install, reinstall, and uninstall for user-level LaunchAgent:
- Runs as current user LaunchAgent (NOT LaunchDaemon).
- Only starts/supervises controller host (python -m macos.cli run).
- Does NOT start GStreamer media pipelines directly.
- Preserves persisted user desired state (STOPPED_BY_USER remains STOPPED_BY_USER).
- Uses launchctl bootstrap gui/<uid> / bootout gui/<uid> with load/unload fallback.
"""

import logging
import os
import plistlib
import shutil
import subprocess
import sys
import time
from typing import Dict, Optional, Tuple

from bridge_core.contract import DEFAULT_LOCAL_IPC_PORT
from .controller import DEFAULT_STATE_FILE

logger = logging.getLogger(__name__)

LAUNCH_AGENT_LABEL = "com.carllx.desk-audio-bridge.controller"
DEFAULT_PLIST_FILENAME = f"{LAUNCH_AGENT_LABEL}.plist"
USER_LAUNCH_AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
DEFAULT_PLIST_PATH = os.path.join(USER_LAUNCH_AGENTS_DIR, DEFAULT_PLIST_FILENAME)
LOG_DIR = os.path.expanduser("~/Library/Logs/desk-audio-bridge")
STDOUT_LOG_PATH = os.path.join(LOG_DIR, "controller.stdout.log")
STDERR_LOG_PATH = os.path.join(LOG_DIR, "controller.stderr.log")


def get_current_uid() -> int:
    """Returns current user UID."""
    return os.getuid()


def get_launch_agent_plist_path() -> str:
    """Returns the canonical path to the user LaunchAgent plist."""
    return DEFAULT_PLIST_PATH


def generate_launch_agent_plist(
    python_exe: Optional[str] = None,
    repo_root: Optional[str] = None,
    stdout_log: str = STDOUT_LOG_PATH,
    stderr_log: str = STDERR_LOG_PATH,
) -> dict:
    """Generates the plist dictionary for the LaunchAgent."""
    py_bin = python_exe or sys.executable
    if not repo_root:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.abspath(os.path.join(current_dir, ".."))

    plist_dict = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            py_bin,
            "-m",
            "macos.cli",
            "run",
        ],
        "WorkingDirectory": repo_root,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": stdout_log,
        "StandardErrorPath": stderr_log,
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": repo_root,
        },
    }
    return plist_dict


def write_launch_agent_plist(
    plist_path: Optional[str] = None,
    python_exe: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> str:
    """Writes the LaunchAgent plist file to disk."""
    target_path = plist_path or DEFAULT_PLIST_PATH
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    plist_dict = generate_launch_agent_plist(python_exe=python_exe, repo_root=repo_root)
    with open(target_path, "wb") as f:
        plistlib.dump(plist_dict, f)
    return target_path


def run_launchctl(cmd: list) -> subprocess.CompletedProcess:
    """Executes launchctl command and returns completed process."""
    return subprocess.run(cmd, capture_output=True, text=True)


def is_service_loaded(label: str = LAUNCH_AGENT_LABEL) -> bool:
    """Checks whether the service label is currently loaded in launchd."""
    uid = get_current_uid()
    res = run_launchctl(["launchctl", "print", f"gui/{uid}/{label}"])
    if res.returncode == 0:
        return True

    res_list = run_launchctl(["launchctl", "list"])
    if res_list.returncode == 0 and label in res_list.stdout:
        return True
    return False


def bootout_service(plist_path: Optional[str] = None, label: str = LAUNCH_AGENT_LABEL) -> bool:
    """Boots out / unloads the service from launchd."""
    uid = get_current_uid()
    target_path = plist_path or DEFAULT_PLIST_PATH

    res = run_launchctl(["launchctl", "bootout", f"gui/{uid}/{label}"])
    if res.returncode == 0:
        return True

    if os.path.exists(target_path):
        res_path = run_launchctl(["launchctl", "bootout", f"gui/{uid}", target_path])
        if res_path.returncode == 0:
            return True

    if os.path.exists(target_path):
        res_legacy = run_launchctl(["launchctl", "unload", "-w", target_path])
        if res_legacy.returncode == 0:
            return True

    return not is_service_loaded(label)


def bootstrap_service(plist_path: Optional[str] = None, label: str = LAUNCH_AGENT_LABEL) -> bool:
    """Bootstraps / loads the service into launchd."""
    uid = get_current_uid()
    target_path = plist_path or DEFAULT_PLIST_PATH

    if not os.path.exists(target_path):
        logger.error("LaunchAgent plist does not exist at %s", target_path)
        return False

    res = run_launchctl(["launchctl", "bootstrap", f"gui/{uid}", target_path])
    if res.returncode == 0:
        return True

    res_legacy = run_launchctl(["launchctl", "load", "-w", target_path])
    if res_legacy.returncode == 0:
        return True

    logger.warning("launchctl bootstrap/load failed: bootstrap err: %s, legacy err: %s", res.stderr, res_legacy.stderr)
    return is_service_loaded(label)


def install_launch_agent(
    python_exe: Optional[str] = None,
    repo_root: Optional[str] = None,
    plist_path: Optional[str] = None,
    wait_ready_timeout: float = 3.0,
) -> bool:
    """Idempotently installs and registers the user LaunchAgent.

    Steps:
    1. If already loaded, safely boots out old instance.
    2. Writes updated plist to ~/Library/LaunchAgents/.
    3. Bootstraps service in launchd for current user session.
    4. Waits briefly for controller IPC to become responsive.
    Preserves existing desired_state (e.g. STOPPED_BY_USER).
    """
    target_path = plist_path or DEFAULT_PLIST_PATH

    if is_service_loaded():
        bootout_service(target_path)
        time.sleep(0.5)

    write_launch_agent_plist(
        plist_path=target_path,
        python_exe=python_exe,
        repo_root=repo_root,
    )

    if not bootstrap_service(target_path):
        return False

    from .cli import send_ipc_command

    start_time = time.time()
    while time.time() - start_time < wait_ready_timeout:
        st = send_ipc_command("status")
        if st is not None:
            return True
        time.sleep(0.2)

    return is_service_loaded()


def reinstall_launch_agent(
    python_exe: Optional[str] = None,
    repo_root: Optional[str] = None,
    plist_path: Optional[str] = None,
) -> bool:
    """Reinstalls the LaunchAgent while strictly preserving user desired state."""
    return install_launch_agent(
        python_exe=python_exe,
        repo_root=repo_root,
        plist_path=plist_path,
    )


def uninstall_launch_agent(
    plist_path: Optional[str] = None,
    remove_state: bool = True,
    state_file: str = DEFAULT_STATE_FILE,
) -> bool:
    """Idempotently uninstalls the LaunchAgent and cleans up project-owned runtime state.

    Order of operations (race-safe):
    1. Gracefully stop controller and owned media children via IPC while host is alive.
    2. Bootout service from launchd so launchd will NOT restart it.
    3. Remove plist file.
    4. Remove project-owned runtime state if requested.
    Repeated calls succeed without error.
    """
    target_path = plist_path or DEFAULT_PLIST_PATH
    from .cli import send_ipc_command

    # 1. First gracefully stop controller & owned children via IPC while controller is alive
    send_ipc_command("stop")
    time.sleep(0.3)

    # 2. Bootout service so launchd terminates the host process and won't restart it
    bootout_service(target_path)
    time.sleep(0.3)

    # 3. Double-check if still responding on IPC port, send stop again
    send_ipc_command("stop")

    # 4. Safely remove plist file
    if os.path.exists(target_path):
        try:
            os.remove(target_path)
        except OSError as exc:
            logger.warning("Failed to remove plist %s: %s", target_path, exc)

    # 5. Remove project-owned state if requested
    if remove_state and os.path.exists(state_file):
        try:
            os.remove(state_file)
        except OSError as exc:
            logger.warning("Failed to remove state file %s: %s", state_file, exc)

    return True
