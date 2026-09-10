"""Windows Task Scheduler lifecycle manager for desk-audio-bridge.

Implements current-user Scheduled Task lifecycle management via Schedule.Service COM:
- Logon trigger for current user
- Bounded RestartOnFailure policy (3 attempts, 1 minute interval)
- Single-instance policy (IgnoreNew)
- Windowless execution using pythonw.exe
- Explicit working directory and launcher entrypoint
- Idempotent install, reinstall, and uninstall
- Safe controller takeover from unmanaged/manual host
"""

import logging
import os
import shutil
import sys
import time
from typing import Any, Dict, Optional, Tuple

import shutil
import sys
import time
from typing import Any, Dict, Optional, Tuple

from bridge_core.contract import DEFAULT_LOCAL_IPC_PORT
from .cli import send_ipc_command

logger = logging.getLogger(__name__)

DEFAULT_TASK_NAME = "desk-audio-bridge"


def _check_pywin32_dependency() -> None:
    """Checks whether pywin32 is installed and raises actionable RuntimeError if absent."""
    try:
        import win32api
        import win32security
        import win32com.client
    except ImportError as exc:
        raise RuntimeError(
            "Missing Windows lifecycle dependency 'pywin32'. "
            "Please install dependencies with 'pip install -r requirements.txt' "
            "(or 'pip install pywin32') to use Task Scheduler lifecycle commands."
        ) from exc


def get_current_user_sid() -> str:
    """Resolves current user token SID as string."""
    _check_pywin32_dependency()
    import ntsecuritycon
    import win32api
    import win32security

    tok = win32security.OpenProcessToken(win32api.GetCurrentProcess(), ntsecuritycon.TOKEN_QUERY)
    try:
        sid, _ = win32security.GetTokenInformation(tok, ntsecuritycon.TokenUser)
        return win32security.ConvertSidToStringSid(sid)
    finally:
        tok.Close()


def resolve_windowless_python() -> str:
    """Resolves verified pythonw.exe executable. Fails with RuntimeError if missing."""
    py_dir = os.path.dirname(sys.executable)
    candidates = [
        os.path.join(py_dir, "pythonw.exe"),
        os.path.join(py_dir, "Scripts", "pythonw.exe"),
    ]
    which_pyw = shutil.which("pythonw.exe")
    if which_pyw:
        candidates.append(which_pyw)

    for cand in candidates:
        if os.path.isfile(cand):
            return os.path.abspath(cand)

    raise RuntimeError(
        "pythonw.exe not found in Python environment. "
        "A windowless Python executable is required to run background controller without console popup."
    )


def get_project_repo_root() -> str:
    """Returns the absolute path to the repository root."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_launcher_path() -> str:
    """Returns the absolute path to windows/launcher.py."""
    return os.path.join(get_project_repo_root(), "windows", "launcher.py")


def _get_scheduler_folder():
    """Connects to Windows Task Scheduler COM service and returns root folder."""
    _check_pywin32_dependency()
    import win32com.client
    ts = win32com.client.Dispatch("Schedule.Service")
    ts.Connect()
    return ts.GetFolder("\\")


def is_scheduled_task_installed(task_name: str = DEFAULT_TASK_NAME) -> bool:
    """Checks whether the scheduled task is currently registered."""
    try:
        folder = _get_scheduler_folder()
        folder.GetTask(task_name)
        return True
    except Exception:
        return False


def get_scheduled_task_info(task_name: str = DEFAULT_TASK_NAME) -> Optional[Dict[str, Any]]:
    """Returns status and configuration dictionary for the registered task, or None if absent."""
    try:
        folder = _get_scheduler_folder()
        task = folder.GetTask(task_name)
        definition = task.Definition
        settings = definition.Settings
        actions = definition.Actions
        triggers = definition.Triggers

        action_cmd = ""
        action_args = ""
        action_workdir = ""
        if actions.Count > 0:
            act = actions.Item(1)
            action_cmd = getattr(act, "Path", "")
            action_args = getattr(act, "Arguments", "")
            action_workdir = getattr(act, "WorkingDirectory", "")

        trigger_type = None
        trigger_user = ""
        trigger_delay = ""
        if triggers.Count > 0:
            trig = triggers.Item(1)
            trigger_type = getattr(trig, "Type", None)
            trigger_user = getattr(trig, "UserId", "")
            trigger_delay = getattr(trig, "Delay", "")

        principal_user = definition.Principal.UserId
        logon_type = getattr(definition.Principal, "LogonType", None)

        return {
            "task_name": task_name,
            "enabled": task.Enabled,
            "state": task.State,
            "run_as_user": principal_user,
            "logon_type": logon_type,
            "restart_count": settings.RestartCount,
            "restart_interval": settings.RestartInterval,
            "multiple_instances": settings.MultipleInstances,
            "start_when_available": settings.StartWhenAvailable,
            "disallow_start_if_on_batteries": settings.DisallowStartIfOnBatteries,
            "stop_if_going_on_batteries": settings.StopIfGoingOnBatteries,
            "trigger_type": trigger_type,
            "trigger_user": trigger_user,
            "trigger_delay": trigger_delay,
            "command": action_cmd,
            "arguments": action_args,
            "working_directory": action_workdir,
        }
    except Exception:
        return None


def register_scheduled_task(task_name: str = DEFAULT_TASK_NAME, force: bool = True) -> Tuple[bool, str]:
    """Registers the current-user logon task with bounded restart-on-failure and IgnoreNew policy."""
    try:
        pythonw_path = resolve_windowless_python()
    except RuntimeError as exc:
        return False, str(exc)

    repo_root = get_project_repo_root()
    launcher_path = get_launcher_path()
    user_sid = get_current_user_sid()

    try:
        import win32com.client
        ts = win32com.client.Dispatch("Schedule.Service")
        ts.Connect()
        folder = ts.GetFolder("\\")

        td = ts.NewTask(0)
        td.RegistrationInfo.Description = "desk-audio-bridge Windows background audio controller"
        td.RegistrationInfo.Author = "desk-audio-bridge"

        # Principal: current user logon session
        td.Principal.UserId = user_sid
        td.Principal.LogonType = 3  # TASK_LOGON_INTERACTIVE_TOKEN

        # Settings
        settings = td.Settings
        settings.Enabled = True
        settings.StartWhenAvailable = True
        settings.DisallowStartIfOnBatteries = False
        settings.StopIfGoingOnBatteries = False
        settings.ExecutionTimeLimit = "PT0S"  # Indefinite execution
        settings.MultipleInstances = 2  # TASK_INSTANCES_IGNORE_NEW
        settings.RestartCount = 3  # Bounded restarts: max 3 attempts
        settings.RestartInterval = "PT1M"  # 1 minute interval

        # Trigger: Logon trigger for current user with bounded delay for session readiness
        trigger = td.Triggers.Create(9)  # TASK_TRIGGER_LOGON
        trigger.Enabled = True
        trigger.UserId = user_sid
        trigger.Delay = "PT2S"  # Bounded delay ensuring user desktop, shell, and audio endpoints are ready

        # Action: Exec pythonw.exe launcher.py with WorkingDirectory = repo_root
        action = td.Actions.Create(0)  # TASK_ACTION_EXEC
        action.Path = pythonw_path
        action.Arguments = f'"{launcher_path}"'
        action.WorkingDirectory = repo_root

        folder.RegisterTaskDefinition(task_name, td, 6, None, None, 3)
        return True, f"Scheduled task '{task_name}' registered successfully"
    except Exception as exc:
        return False, f"Failed to register scheduled task '{task_name}': {exc}"


def terminate_verified_controller_host(port: int = DEFAULT_LOCAL_IPC_PORT, timeout_sec: float = 3.0) -> bool:
    """Gracefully stops and terminates only the verified controller host process holding local IPC."""
    import psutil
    status = send_ipc_command("status", port=port)
    if not status:
        return True

    owner_pid = status.get("owner_pid")
    if not owner_pid:
        return True

    try:
        proc = psutil.Process(owner_pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True

    send_ipc_command("shutdown", port=port)

    start = time.time()
    while time.time() - start < timeout_sec:
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return True
        time.sleep(0.1)

    try:
        proc.terminate()
        proc.wait(timeout=2.0)
    except Exception:
        pass

    return not proc.is_running()


def install_scheduled_task(task_name: str = DEFAULT_TASK_NAME, start_service: bool = True) -> Tuple[bool, str]:
    """Idempotently installs or updates the Scheduled Task with safe manual host handoff."""
    if not terminate_verified_controller_host():
        return False, "Failed to terminate pre-existing unmanaged controller host"

    ok, msg = register_scheduled_task(task_name=task_name, force=True)
    if not ok:
        return False, msg

    if start_service:
        try:
            folder = _get_scheduler_folder()
            task = folder.GetTask(task_name)
            task.Run(None)
        except Exception as exc:
            return False, f"Scheduled task '{task_name}' was registered, but immediate trigger failed: {exc}"

        # Bounded verification window: verify lifecycle-managed controller becomes responsive
        start_wait = time.time()
        controller_active = False
        while time.time() - start_wait < 5.0:
            status = send_ipc_command("status")
            if status is not None and status.get("owner_pid") is not None:
                controller_active = True
                break
            time.sleep(0.2)

        if not controller_active:
            return False, (
                f"Scheduled task '{task_name}' registered successfully, but background controller "
                "did not become responsive on IPC within 5.0s verification window"
            )

    return True, msg


def reinstall_scheduled_task(task_name: str = DEFAULT_TASK_NAME) -> Tuple[bool, str]:
    """Reinstalls the Scheduled Task, preserving existing user desired state."""
    return install_scheduled_task(task_name=task_name, start_service=True)


def uninstall_scheduled_task(task_name: str = DEFAULT_TASK_NAME, cleanup_state: bool = True) -> Tuple[bool, str]:
    """Completely uninstalls scheduled task and cleans up controller runtime and state."""
    from .controller import DEFAULT_STATE_FILE

    # 1. Stop media pipelines
    send_ipc_command("stop")

    # 2. Terminate verified controller host
    if not terminate_verified_controller_host():
        return False, "Failed to terminate running controller host during uninstall"

    # 3. Delete Scheduled Task if present
    try:
        folder = _get_scheduler_folder()
        try:
            folder.DeleteTask(task_name, 0)
        except Exception:
            pass
    except Exception as exc:
        return False, f"Failed to access Task Scheduler: {exc}"

    # 4. Remove project-owned runtime state file
    if cleanup_state and os.path.exists(DEFAULT_STATE_FILE):
        try:
            os.remove(DEFAULT_STATE_FILE)
        except Exception as exc:
            return False, f"Failed to remove runtime state file {DEFAULT_STATE_FILE}: {exc}"

    # 5. Verify postconditions
    if is_scheduled_task_installed(task_name):
        return False, f"Scheduled task '{task_name}' still exists after deletion"

    if send_ipc_command("status") is not None:
        return False, "Controller host IPC is still responding after uninstall"

    return True, f"Scheduled task '{task_name}' uninstalled cleanly"
