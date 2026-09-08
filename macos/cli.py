"""Command line interface and IPC client for desk-audio-bridge controller on macOS."""

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
from .controller import MacBridgeController


def send_ipc_command(command: str, port: int = DEFAULT_LOCAL_IPC_PORT) -> Optional[Dict[str, Any]]:
    """Sends command to running controller owner via local loopback TCP socket."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
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
    """Spawns background controller host process if not already running on macOS."""
    res = send_ipc_command("status", port=port)
    if res is not None:
        return True

    python_exe = sys.executable
    cmd = [python_exe, "-m", "macos.cli", "run"]
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    start_time = time.time()
    while time.time() - start_time < 3.0:
        time.sleep(0.1)
        if send_ipc_command("status", port=port) is not None:
            return True
    return False


import signal


def run_host_service():
    """Runs the controller owner process in foreground/daemon mode on macOS.
    
    Uses controller.start_host() to preserve persisted user desired state (e.g. STOPPED_BY_USER).
    Fails closed with exit code 2 if another process holds the singleton lock.
    """
    controller = MacBridgeController()
    if not controller.start_host():
        print("Failed to start controller host: singleton lock held by another process", file=sys.stderr)
        sys.exit(2)

    def _sig_handler(signum, frame):
        # Exit the loop cleanly triggering finally block
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, _sig_handler)
        signal.signal(signal.SIGHUP, _sig_handler)
    except Exception:
        pass

    print(f"Controller host started (PID {os.getpid()})")
    try:
        while True:
            time.sleep(1.0)
            controller.reconcile()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        controller.shutdown()
        print("Controller host shutdown cleanly")


def main():
    parser = argparse.ArgumentParser(description="desk-audio-bridge macOS controller")
    parser.add_argument(
        "command",
        choices=["start", "stop", "status", "reconcile", "run", "install", "reinstall", "uninstall"],
        help="Action to execute",
    )
    parser.add_argument("--json", action="store_true", help="Output status in JSON format")

    args = parser.parse_args()

    if args.command == "run":
        run_host_service()
        return

    # Lifecycle management commands
    if args.command == "install":
        from .lifecycle import install_launch_agent
        if install_launch_agent():
            print("LaunchAgent installed and registered successfully")
        else:
            print("Failed to install or bootstrap LaunchAgent", file=sys.stderr)
            sys.exit(1)
        return

    elif args.command == "reinstall":
        from .lifecycle import reinstall_launch_agent
        if reinstall_launch_agent():
            print("LaunchAgent reinstalled successfully (persisted desired state preserved)")
        else:
            print("Failed to reinstall LaunchAgent", file=sys.stderr)
            sys.exit(1)
        return

    elif args.command == "uninstall":
        from .lifecycle import uninstall_launch_agent
        if uninstall_launch_agent():
            print("LaunchAgent uninstalled and controller stopped successfully")
        else:
            print("Failed to uninstall LaunchAgent", file=sys.stderr)
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
            ctrl = MacBridgeController()
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
            ctrl = MacBridgeController()
            status_dict = ctrl.get_status().to_dict()
            status_dict["controller_state"] = "STOPPED (HOST_NOT_RUNNING)"
            status_dict["owner_pid"] = None

        if args.json:
            print(json.dumps(status_dict, indent=2))
        else:
            print("=== desk-audio-bridge macOS Controller Status ===")
            print(f"Controller State:           {status_dict.get('controller_state')}")
            print(f"Desired State:              {status_dict.get('desired_state')}")
            print(f"Host Role:                  {status_dict.get('role')}")
            print(f"Owner PID:                  {status_dict.get('owner_pid')}")
            print(f"Peer Available:             {status_dict.get('peer_available')}")
            print(f"Peer Address:               {status_dict.get('peer_address') or 'None'}")
            print(f"Local Bind Address:         {status_dict.get('local_bind_address') or 'None'}")
            print(f"Speaker Path State:         {status_dict.get('speaker_path_state')}")
            print(f"Speaker Port:               {status_dict.get('speaker_target_port')}")
            print(f"Microphone Path State:      {status_dict.get('microphone_path_state')}")
            print(f"Microphone Port:            {status_dict.get('microphone_port')}")
            print(f"Owned Children Count:       {status_dict.get('owned_children_count')}")
            if status_dict.get("last_actionable_error"):
                print(f"Last Error:                 {status_dict.get('last_actionable_error')}")
            if status_dict.get("last_actionable_microphone_error"):
                print(f"Last Mic Error:             {status_dict.get('last_actionable_microphone_error')}")


if __name__ == "__main__":
    main()
