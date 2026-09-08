"""Command line interface and IPC client for desk-audio-bridge controller on Windows."""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from typing import Any, Dict, Optional

from bridge_core.contract import (
    DEFAULT_LOCAL_IPC_PORT,
    DEFAULT_SINGLETON_PORT,
    ControllerStatus,
    DesiredState,
)
from .controller import WindowsBridgeController


def send_ipc_command(
    command: str,
    port: int = DEFAULT_LOCAL_IPC_PORT,
    timeout: float = 15.0,
) -> Optional[Dict[str, Any]]:
    """Sends command to running controller owner via local loopback TCP socket."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(("127.0.0.1", port))
        req = json.dumps({"command": command})
        s.sendall(req.encode("utf-8"))
        data = s.recv(8192)
        s.close()
        if data:
            return json.loads(data.decode("utf-8"))
    except (ConnectionRefusedError, socket.timeout, OSError):
        return None
    return None


def ensure_controller_host_running(port: int = DEFAULT_LOCAL_IPC_PORT) -> bool:
    """Spawns background controller host process if not already running."""
    res = send_ipc_command("status", port=port)
    if res is not None:
        return True

    python_exe = sys.executable
    cmd = [python_exe, "-m", "windows.cli", "run"]
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    )
    start_time = time.time()
    while time.time() - start_time < 3.0:
        time.sleep(0.1)
        if send_ipc_command("status", port=port) is not None:
            return True
    return False


def run_host_service():
    """Runs the controller owner process in foreground/daemon mode.
    
    Fails closed with exit code 2 if another process holds the singleton lock.
    """
    controller = WindowsBridgeController()
    if not controller.start_host():
        print("Failed to start controller host: singleton lock held by another process", file=sys.stderr)
        sys.exit(2)

    print(f"Controller host started (PID {os.getpid()})")
    try:
        while not controller.is_shutdown_requested:
            time.sleep(0.5)
            if controller.is_shutdown_requested:
                break
            controller.reconcile()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        controller.shutdown_host()
        print("Controller host shutdown cleanly")


def main():
    parser = argparse.ArgumentParser(description="desk-audio-bridge Windows controller")
    parser.add_argument(
        "command",
        choices=["start", "stop", "status", "reconcile", "run", "install", "reinstall", "uninstall", "mic-enable", "mic-disable"],
        help="Action to execute",
    )
    parser.add_argument("--json", action="store_true", help="Output status in JSON format")

    args = parser.parse_args()

    if args.command == "run":
        run_host_service()
        return

    if args.command == "mic-enable":
        res = send_ipc_command("mic-enable")
        if res and res.get("success"):
            print("Microphone enabled (RUNNING)")
        else:
            state = res.get("microphone_path_state") if res else "UNKNOWN"
            print(f"Failed to enable microphone (state: {state})", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "mic-disable":
        res = send_ipc_command("mic-disable")
        if res and res.get("success"):
            print("Microphone disabled (STOPPED)")
        else:
            print("Failed to disable microphone", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "install":
        from .task_scheduler import install_scheduled_task
        ok, msg = install_scheduled_task()
        if ok:
            print(f"Install successful: {msg}")
        else:
            print(f"Install failed: {msg}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "reinstall":
        from .task_scheduler import reinstall_scheduled_task
        ok, msg = reinstall_scheduled_task()
        if ok:
            print(f"Reinstall successful: {msg}")
        else:
            print(f"Reinstall failed: {msg}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "uninstall":
        from .task_scheduler import uninstall_scheduled_task
        ok, msg = uninstall_scheduled_task()
        if ok:
            print(f"Uninstall successful: {msg}")
        else:
            print(f"Uninstall failed: {msg}", file=sys.stderr)
            sys.exit(1)
        return

    # For Start: Ensure controller host is running, then send start command
    if args.command == "start":
        if not ensure_controller_host_running():
            print("Failed to start or connect to controller host", file=sys.stderr)
            sys.exit(1)
        res = send_ipc_command("start")
        if res and res.get("success"):
            print("Controller enabled")
        else:
            print(f"Failed to enable controller: {res}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "stop":
        res = send_ipc_command("stop")
        if res is not None:
            print("Controller stopped (STOPPED_BY_USER)")
        else:
            ctrl = WindowsBridgeController()
            ctrl.stop()
            print("Controller stopped (no host running, persisted STOPPED_BY_USER)")

    elif args.command == "reconcile":
        res = send_ipc_command("reconcile")
        if res is not None:
            print("Reconciliation triggered on active host")
        else:
            print("No active controller host to reconcile")

    elif args.command == "status":
        res = send_ipc_command("status")
        if res is not None:
            status_dict = res
        else:
            ctrl = WindowsBridgeController()
            status_dict = ctrl.get_status().to_dict()
            status_dict["controller_state"] = "STOPPED (HOST_NOT_RUNNING)"
            status_dict["owner_pid"] = None

        if args.json:
            print(json.dumps(status_dict, indent=2))
        else:
            print("=== desk-audio-bridge Controller Status ===")
            print(f"Controller State:      {status_dict.get('controller_state')}")
            print(f"Desired State:         {status_dict.get('desired_state')}")
            print(f"Host Role:             {status_dict.get('role')}")
            print(f"Owner PID:             {status_dict.get('owner_pid')}")
            print(f"Peer Available:        {status_dict.get('peer_available')}")
            print(f"Peer Address:          {status_dict.get('peer_address') or 'None'}")
            print(f"Local Bind Address:    {status_dict.get('local_bind_address') or 'None'}")
            print(f"Speaker Path State:    {status_dict.get('speaker_path_state')}")
            print(f"Speaker Port:          {status_dict.get('speaker_target_port')}")
            print(f"Owned Children Count:  {status_dict.get('owned_children_count')}")
            try:
                from .task_scheduler import is_scheduled_task_installed
                task_installed = is_scheduled_task_installed()
                task_str = "Installed" if task_installed else "Not Installed"
            except Exception:
                task_str = "Unknown (pywin32 unavailable)"
            print(f"Scheduled Task:        {task_str}")
            if status_dict.get("last_actionable_error"):
                print(f"Last Error:            {status_dict.get('last_actionable_error')}")


if __name__ == "__main__":
    main()
