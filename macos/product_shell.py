"""Cross-Desk Flow macOS Product MVP Shell.

Provides a clean, lightweight native Tkinter window displaying overall status,
audio stream direction states (PC -> Mac Speaker, Mac -> PC Microphone),
actionable error summaries, and Start/Stop/Refresh control buttons.

Acts strictly as an IPC controller client without owning or spawning background
services or GStreamer pipelines.
"""

from dataclasses import dataclass
import sys
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, Optional

from macos.cli import send_ipc_command

# User-facing state mapping constants
STATUS_CONNECTED = "Connected"
STATUS_WAITING_PC = "Waiting for PC"
STATUS_DEGRADED = "Degraded"
STATUS_STOPPED = "Stopped"
STATUS_ACTION_REQUIRED = "Action required"

DIR_ACTIVE = "Active"
DIR_WAITING = "Waiting"
DIR_STOPPED = "Stopped"
DIR_PROBLEM = "Problem"


@dataclass
class UIState:
    """Parsed UI state derived from controller status payload."""
    overall_status: str
    overall_detail: str
    speaker_status: str
    microphone_status: str
    action_required_message: Optional[str] = None


def map_controller_status_to_ui(status_payload: Optional[Dict[str, Any]]) -> UIState:
    """Maps raw controller status payload to clean user-facing state.
    
    Lowest semantic requirements:
    - Host absent / None -> Action required: Background service is not running
    - STOPPED_BY_USER -> Stopped
    - Actionable error or unresolved failure -> Action required
    - Peer unavailable -> Waiting for PC
    - Speaker + Mic RUNNING -> Connected
    - One direction FAILED / UNAVAILABLE -> Degraded
    - Any other degraded or waiting conditions
    """
    if status_payload is None:
        return UIState(
            overall_status=STATUS_ACTION_REQUIRED,
            overall_detail="Background service is not running",
            speaker_status=DIR_STOPPED,
            microphone_status=DIR_STOPPED,
            action_required_message="Background service is not running",
        )

    desired_state = status_payload.get("desired_state", "")
    controller_state = status_payload.get("controller_state", "")
    peer_available = bool(status_payload.get("peer_available", False))
    speaker_path = status_payload.get("speaker_path_state", "IDLE")
    mic_path = status_payload.get("microphone_path_state", "IDLE")
    last_error = status_payload.get("last_actionable_error")
    last_mic_error = status_payload.get("last_actionable_microphone_error")

    # Map directions
    def map_path(path_state: str) -> str:
        if path_state == "RUNNING":
            return DIR_ACTIVE
        elif path_state in ("FAILED", "UNAVAILABLE"):
            return DIR_PROBLEM
        elif path_state == "STOPPED":
            return DIR_STOPPED
        elif path_state in ("STARTING", "READY", "IDLE"):
            return DIR_WAITING
        return DIR_PROBLEM

    spk_ui = map_path(speaker_path)
    mic_ui = map_path(mic_path)

    # 1. STOPPED_BY_USER
    if desired_state == "STOPPED_BY_USER":
        return UIState(
            overall_status=STATUS_STOPPED,
            overall_detail="Audio bridge is stopped by user",
            speaker_status=DIR_STOPPED,
            microphone_status=DIR_STOPPED,
            action_required_message=None,
        )

    # 2. Host error / Action required
    if controller_state == "ERROR" or (speaker_path == "FAILED" and mic_path == "FAILED"):
        err_detail = last_error or last_mic_error or "Audio bridge encountered an unrecoverable problem"
        return UIState(
            overall_status=STATUS_ACTION_REQUIRED,
            overall_detail=err_detail,
            speaker_status=spk_ui,
            microphone_status=mic_ui,
            action_required_message=err_detail,
        )

    # 3. Peer unavailable
    if not peer_available:
        return UIState(
            overall_status=STATUS_WAITING_PC,
            overall_detail="Waiting for Windows PC on local network",
            speaker_status=spk_ui,
            microphone_status=mic_ui,
            action_required_message=None,
        )

    # 4. Both speaker + mic active -> Connected
    if speaker_path == "RUNNING" and mic_path == "RUNNING":
        return UIState(
            overall_status=STATUS_CONNECTED,
            overall_detail="Two-way audio bridge active",
            speaker_status=DIR_ACTIVE,
            microphone_status=DIR_ACTIVE,
            action_required_message=None,
        )

    # 5. One direction failed / unavailable / starting -> Degraded
    if (
        speaker_path in ("FAILED", "UNAVAILABLE")
        or mic_path in ("FAILED", "UNAVAILABLE")
        or (speaker_path == "RUNNING" and mic_path != "RUNNING")
        or (mic_path == "RUNNING" and speaker_path != "RUNNING")
    ):
        detail = "One or more audio paths degraded"
        if speaker_path in ("FAILED", "UNAVAILABLE"):
            detail = f"Speaker error: {last_error or 'unavailable'}"
        elif mic_path in ("FAILED", "UNAVAILABLE"):
            detail = f"Microphone error: {last_mic_error or 'unavailable'}"
        return UIState(
            overall_status=STATUS_DEGRADED,
            overall_detail=detail,
            speaker_status=spk_ui,
            microphone_status=mic_ui,
            action_required_message=None,
        )

    # 6. Default waiting state
    return UIState(
        overall_status=STATUS_WAITING_PC,
        overall_detail="Connecting audio paths...",
        speaker_status=spk_ui,
        microphone_status=mic_ui,
        action_required_message=None,
    )


class ProductShellApp:
    """Tkinter application shell for Cross-Desk Flow on macOS."""

    def __init__(
        self,
        root: tk.Tk,
        ipc_client: Callable[[str], Optional[Dict[str, Any]]] = send_ipc_command,
        auto_refresh_ms: int = 1000,
    ):
        self.root = root
        self.ipc_client = ipc_client
        self.auto_refresh_ms = auto_refresh_ms
        self._refresh_timer_id: Optional[str] = None

        self.root.title("Cross-Desk Flow")
        self.root.geometry("460x340")
        self.root.minsize(420, 300)

        # Style configuration
        self._setup_style()
        self._build_ui()

        # Initial fetch
        self.refresh()

        # Start periodic refresh loop
        self._schedule_auto_refresh()

    def _setup_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

    def _build_ui(self):
        container = ttk.Frame(self.root, padding="20 20 20 20")
        container.pack(fill=tk.BOTH, expand=True)

        # 1. Overall Status Card
        card = ttk.LabelFrame(container, text=" System Status ", padding="12 10 12 10")
        card.pack(fill=tk.X, pady=(0, 15))

        self.lbl_overall = ttk.Label(
            card,
            text=STATUS_ACTION_REQUIRED,
            font=("System", 16, "bold"),
        )
        self.lbl_overall.pack(anchor=tk.W)

        self.lbl_overall_detail = ttk.Label(
            card,
            text="",
            font=("System", 11),
            foreground="#555555",
            wraplength=390,
        )
        self.lbl_overall_detail.pack(anchor=tk.W, pady=(4, 0))

        # 2. Audio Direction Section
        dir_frame = ttk.LabelFrame(container, text=" Audio Streams ", padding="12 10 12 10")
        dir_frame.pack(fill=tk.X, pady=(0, 15))

        # Row 1: PC -> Mac Speaker
        row1 = ttk.Frame(dir_frame)
        row1.pack(fill=tk.X, pady=4)
        lbl_spk_title = ttk.Label(row1, text="PC → Mac Speaker", font=("System", 12))
        lbl_spk_title.pack(side=tk.LEFT)
        self.lbl_spk_status = ttk.Label(row1, text="", font=("System", 12, "bold"))
        self.lbl_spk_status.pack(side=tk.RIGHT)

        # Row 2: Mac -> PC Microphone
        row2 = ttk.Frame(dir_frame)
        row2.pack(fill=tk.X, pady=4)
        lbl_mic_title = ttk.Label(row2, text="Mac → PC Microphone", font=("System", 12))
        lbl_mic_title.pack(side=tk.LEFT)
        self.lbl_mic_status = ttk.Label(row2, text="", font=("System", 12, "bold"))
        self.lbl_mic_status.pack(side=tk.RIGHT)

        # 3. Action / Error Banner (visible when action required)
        self.lbl_action_banner = ttk.Label(
            container,
            text="",
            font=("System", 11),
            foreground="#D32F2F",
            wraplength=410,
        )
        self.lbl_action_banner.pack(fill=tk.X, pady=(0, 10))

        # 4. Control Buttons (Start, Stop, Refresh)
        btn_frame = ttk.Frame(container)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)

        self.btn_start = ttk.Button(btn_frame, text="Start", command=self.on_start)
        self.btn_start.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_stop = ttk.Button(btn_frame, text="Stop", command=self.on_stop)
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_refresh = ttk.Button(btn_frame, text="Refresh", command=self.on_refresh)
        self.btn_refresh.pack(side=tk.RIGHT)

    def _schedule_auto_refresh(self):
        if self.auto_refresh_ms > 0:
            self._refresh_timer_id = self.root.after(self.auto_refresh_ms, self._auto_refresh_tick)

    def _auto_refresh_tick(self):
        self.refresh()
        self._schedule_auto_refresh()

    def refresh(self):
        """Fetches status via IPC and updates UI labels."""
        try:
            status_data = self.ipc_client("status")
        except Exception:
            status_data = None

        state = map_controller_status_to_ui(status_data)
        self._apply_ui_state(state)

    def _apply_ui_state(self, state: UIState):
        self.lbl_overall.config(text=state.overall_status)
        self.lbl_overall_detail.config(text=state.overall_detail)

        color_map = {
            STATUS_CONNECTED: "#2E7D32",       # Green
            STATUS_WAITING_PC: "#ED6C02",      # Orange
            STATUS_DEGRADED: "#ED6C02",        # Orange
            STATUS_STOPPED: "#757575",         # Grey
            STATUS_ACTION_REQUIRED: "#D32F2F", # Red
        }
        self.lbl_overall.config(foreground=color_map.get(state.overall_status, "#000000"))

        self.lbl_spk_status.config(text=state.speaker_status)
        self.lbl_mic_status.config(text=state.microphone_status)

        dir_color_map = {
            DIR_ACTIVE: "#2E7D32",
            DIR_WAITING: "#ED6C02",
            DIR_STOPPED: "#757575",
            DIR_PROBLEM: "#D32F2F",
        }
        self.lbl_spk_status.config(foreground=dir_color_map.get(state.speaker_status, "#000000"))
        self.lbl_mic_status.config(foreground=dir_color_map.get(state.microphone_status, "#000000"))

        if state.action_required_message:
            self.lbl_action_banner.config(text=f"Action required — {state.action_required_message}")
        else:
            self.lbl_action_banner.config(text="")

    def on_start(self):
        """Dispatches 'start' command strictly through IPC."""
        try:
            self.ipc_client("start")
        except Exception:
            pass
        self.refresh()

    def on_stop(self):
        """Dispatches 'stop' command strictly through IPC."""
        try:
            self.ipc_client("stop")
        except Exception:
            pass
        self.refresh()

    def on_refresh(self):
        """Manual refresh button handler."""
        self.refresh()


def main():
    root = tk.Tk()
    app = ProductShellApp(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
