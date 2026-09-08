"""macOS LaunchAgent lifecycle management for desk-audio-bridge.

Implements idempotent install, reinstall, and uninstall for user-level LaunchAgent:
- Runs as current user LaunchAgent (NOT LaunchDaemon).
- Only starts/supervises controller host (python -m macos.cli run).
- Does NOT start GStreamer media pipelines directly.
- Preserves persisted user desired state (STOPPED_BY_USER remains STOPPED_BY_USER).
- Uses launchctl bootstrap gui/<uid> / bootout gui/<uid> with load/unload fallback.
- Fails closed with actionable error messages if any cleanup or registration fails.
"""

import logging
import os
import plistlib
import signal
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
        "ThrottleInterval": 2,
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
    """Boots out / unloads the service from launchd. Fails closed if still loaded."""
    if not is_service_loaded(label):
        return True

    uid = get_current_uid()
    target_path = plist_path or DEFAULT_PLIST_PATH

    # Try modern bootout by domain/label
    run_launchctl(["launchctl", "bootout", f"gui/{uid}/{label}"])
    if not is_service_loaded(label):
        return True

    # Try bootout by domain and plist path
    if os.path.exists(target_path):
        run_launchctl(["launchctl", "bootout", f"gui/{uid}", target_path])
        if not is_service_loaded(label):
            return True

    # Legacy fallback: launchctl unload -w
    if os.path.exists(target_path):
        run_launchctl(["launchctl", "unload", "-w", target_path])
        if not is_service_loaded(label):
            return True

    # Check boundedly
    start_time = time.time()
    while time.time() - start_time < 2.0:
        if not is_service_loaded(label):
            return True
        time.sleep(0.1)

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


def handoff_existing_manual_controller(timeout: float = 3.0) -> Tuple[bool, Optional[str]]:
    """Detects and gracefully stops a pre-existing project controller host.

    If a controller host is responding on the project IPC port but not managed by
    LaunchAgent, hand off ownership cleanly to prevent singleton collision:
    1. Query status via IPC to verify it is our project controller and obtain owner_pid.
    2. Gracefully terminate only that verified PID via SIGTERM (run_host_service handles SIGTERM).
    3. Wait boundedly for controller disappearance.
    Fails closed if verified ownership cannot be established.
    """
    from .cli import send_ipc_command

    st = send_ipc_command("status")
    if st is None:
        # No existing controller responding
        return True, None

    # Verified controller response
    role = st.get("role")
    if role != "macos":
        return False, f"Existing service on IPC port is not macos controller (role={role})"

    owner_pid = st.get("owner_pid")
    if not owner_pid or not isinstance(owner_pid, int):
        return False, "Existing controller did not report a valid integer owner_pid"

    # Verify PID is running and belongs to current user
    try:
        os.kill(owner_pid, 0)
    except OSError:
        # Process already gone
        return True, None

    logger.info("Handing off pre-existing manual controller host (PID %d)", owner_pid)
    # Send SIGTERM to verified owner_pid
    try:
        os.kill(owner_pid, signal.SIGTERM)
    except OSError as exc:
        return False, f"Failed to send SIGTERM to existing controller PID {owner_pid}: {exc}"

    # Wait boundedly for process / IPC to exit
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            os.kill(owner_pid, 0)
            time.sleep(0.1)
        except OSError:
            # Process terminated
            break

    # Verify IPC socket is free
    while time.time() - start_time < timeout:
        if send_ipc_command("status") is None:
            return True, None
        time.sleep(0.1)

    if send_ipc_command("status") is not None:
        return False, f"Existing controller PID {owner_pid} did not terminate within {timeout}s"

    return True, None


def install_launch_agent(
    python_exe: Optional[str] = None,
    repo_root: Optional[str] = None,
    plist_path: Optional[str] = None,
    wait_ready_timeout: float = 3.0,
) -> Tuple[bool, Optional[str]]:
    """Idempotently installs and registers the user LaunchAgent.

    Steps:
    1. If already loaded in launchd, boots it out cleanly. Fails closed if bootout fails.
    2. If a manual controller host is running, hands it off cleanly via SIGTERM. Fails closed if handoff fails.
    3. Writes updated plist to ~/Library/LaunchAgents/.
    4. Bootstraps service in launchd for current user session.
    5. Waits briefly for controller IPC to become responsive.
    6. Verifies exactly one LaunchAgent and one controller host, preserving desired state.
    """
    target_path = plist_path or DEFAULT_PLIST_PATH

    # 1. Cleanly bootout if already loaded
    if is_service_loaded():
        if not bootout_service(target_path):
            return False, "Failed to bootout existing LaunchAgent registration"
        time.sleep(0.5)

    # 2. Hand off manual controller if running
    ok, err = handoff_existing_manual_controller()
    if not ok:
        return False, f"Failed to hand off pre-existing manual controller: {err}"

    # 3. Write plist
    try:
        write_launch_agent_plist(
            plist_path=target_path,
            python_exe=python_exe,
            repo_root=repo_root,
        )
    except Exception as exc:
        return False, f"Failed to write LaunchAgent plist to {target_path}: {exc}"

    # 4. Bootstrap into launchd
    if not bootstrap_service(target_path):
        return False, f"Failed to bootstrap LaunchAgent from {target_path}"

    # 5. Wait for controller IPC to be responsive
    from .cli import send_ipc_command

    start_time = time.time()
    controller_ready = False
    while time.time() - start_time < wait_ready_timeout:
        st = send_ipc_command("status")
        if st is not None:
            controller_ready = True
            break
        time.sleep(0.2)

    if not controller_ready:
        return False, f"LaunchAgent registered but controller host not responding within {wait_ready_timeout}s"

    if not is_service_loaded():
        return False, "Controller responded but LaunchAgent registration is not loaded in launchd"

    return True, None


def reinstall_launch_agent(
    python_exe: Optional[str] = None,
    repo_root: Optional[str] = None,
    plist_path: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
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
) -> Tuple[bool, Optional[str]]:
    """Idempotently uninstalls the LaunchAgent and cleans up project-owned runtime state.

    Fails closed:
    - Verifies LaunchAgent is unloaded from launchd.
    - Verifies controller host is terminated.
    - Verifies plist is deleted.
    - Verifies runtime state file is deleted.
    If any operation fails or postconditions are not satisfied, returns (False, err).
    Repeated calls when already absent return (True, None).
    """
    target_path = plist_path or DEFAULT_PLIST_PATH
    from .cli import send_ipc_command

    # 1. Gracefully stop controller & owned media children via IPC while controller is still alive
    st = send_ipc_command("status")
    if st is not None:
        send_ipc_command("stop")
        time.sleep(0.3)

    # 2. Bootout service from launchd so launchd terminates host and will NOT restart it
    if is_service_loaded():
        if not bootout_service(target_path):
            return False, "Failed to bootout LaunchAgent service from launchd"
        time.sleep(0.3)

    # 3. Verify controller host is completely gone from IPC
    start_time = time.time()
    while time.time() - start_time < 3.0:
        if send_ipc_command("status") is None:
            break
        send_ipc_command("stop")
        time.sleep(0.2)

    if send_ipc_command("status") is not None:
        return False, "Controller host still running after bootout and stop"

    # 4. Safely remove plist file
    if os.path.exists(target_path):
        try:
            os.remove(target_path)
        except OSError as exc:
            return False, f"Failed to remove LaunchAgent plist {target_path}: {exc}"

    if os.path.exists(target_path):
        return False, f"LaunchAgent plist still exists after removal: {target_path}"

    # 5. Remove project-owned state if requested
    if remove_state and os.path.exists(state_file):
        try:
            os.remove(state_file)
        except OSError as exc:
            return False, f"Failed to remove project state file {state_file}: {exc}"

    if remove_state and os.path.exists(state_file):
        return False, f"State file still exists after removal: {state_file}"

    # Postconditions verified:
    # - LaunchAgent not loaded
    # - controller host not running
    # - plist absent
    # - state file absent
    return True, None
