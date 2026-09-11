"""Cross-Desk Flow macOS Product Shell.

Provides a clean native Tkinter window displaying overall status,
audio stream direction states (PC -> Mac Speaker, Mac -> PC Microphone),
actionable error summaries, Diagnostics & Recovery panel, and control buttons.

Acts strictly as an IPC controller client without owning background services
or GStreamer pipelines. For dead-controller recovery, delegates strictly to
existing LaunchAgent lifecycle authority.
"""

from dataclasses import dataclass
import sys
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, Optional

from macos.cli import send_ipc_command
from macos.diagnostics import (
    build_diagnostic_report,
    get_diagnostics_view_data,
    has_log_files,
    open_data_directory,
    start_controller_via_lifecycle,
)

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
DIR_OFF = "Off"


@dataclass
class UIState:
    """Parsed UI state derived from controller status payload."""
    overall_status: str
    overall_detail: str
    speaker_status: str
    microphone_status: str
    action_required_message: Optional[str] = None
    raw_status: Optional[Dict[str, Any]] = None


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
            raw_status=None,
        )

    desired_state = status_payload.get("desired_state", "")
    controller_state = status_payload.get("controller_state", "")
    peer_available = bool(status_payload.get("peer_available", False))
    speaker_path = status_payload.get("speaker_path_state", "IDLE")
    mic_path = status_payload.get("microphone_path_state", "IDLE")
    last_error = status_payload.get("last_actionable_error")
    last_mic_error = status_payload.get("last_actionable_microphone_error")

    def map_path(path_state: str, is_mic: bool = False) -> str:
        if path_state == "RUNNING":
            return DIR_ACTIVE
        elif path_state in ("FAILED", "UNAVAILABLE"):
            return DIR_PROBLEM
        elif path_state == "STOPPED":
            return DIR_STOPPED
        elif path_state == "IDLE":
            return DIR_OFF if is_mic else DIR_STOPPED
        elif path_state in ("STARTING", "READY"):
            return DIR_WAITING
        return DIR_PROBLEM

    spk_ui = map_path(speaker_path, is_mic=False)
    mic_ui = map_path(mic_path, is_mic=True)

    # 1. STOPPED_BY_USER
    if desired_state == "STOPPED_BY_USER":
        return UIState(
            overall_status=STATUS_STOPPED,
            overall_detail="Audio bridge is stopped by user",
            speaker_status=DIR_STOPPED,
            microphone_status=DIR_STOPPED,
            action_required_message=None,
            raw_status=status_payload,
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
            raw_status=status_payload,
        )

    # 3. Peer unavailable
    if not peer_available:
        return UIState(
            overall_status=STATUS_WAITING_PC,
            overall_detail="Waiting for Windows PC on local network",
            speaker_status=spk_ui,
            microphone_status=mic_ui,
            action_required_message=None,
            raw_status=status_payload,
        )

    # 4. Both speaker + mic active -> Connected
    if speaker_path == "RUNNING" and mic_path == "RUNNING":
        return UIState(
            overall_status=STATUS_CONNECTED,
            overall_detail="Two-way audio bridge active",
            speaker_status=DIR_ACTIVE,
            microphone_status=DIR_ACTIVE,
            action_required_message=None,
            raw_status=status_payload,
        )

    # 4b. Playback mode: speaker RUNNING and mic IDLE / STOPPED -> Connected
    if speaker_path == "RUNNING" and mic_path in ("IDLE", "STOPPED", "READY"):
        return UIState(
            overall_status=STATUS_CONNECTED,
            overall_detail="Speaker active (Playback mode)",
            speaker_status=DIR_ACTIVE,
            microphone_status=DIR_OFF,
            action_required_message=None,
            raw_status=status_payload,
        )

    # 4c. Dictation mode: mic RUNNING and speaker STOPPED / IDLE -> Connected
    if mic_path == "RUNNING" and speaker_path in ("IDLE", "STOPPED", "READY"):
        return UIState(
            overall_status=STATUS_CONNECTED,
            overall_detail="Microphone active (Dictation mode)",
            speaker_status=DIR_STOPPED,
            microphone_status=DIR_ACTIVE,
            action_required_message=None,
            raw_status=status_payload,
        )

    # 5. One direction failed / unavailable -> Degraded
    if (
        speaker_path in ("FAILED", "UNAVAILABLE")
        or mic_path in ("FAILED", "UNAVAILABLE")
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
            raw_status=status_payload,
        )

    # 6. Default waiting state
    return UIState(
        overall_status=STATUS_WAITING_PC,
        overall_detail="Connecting audio paths...",
        speaker_status=spk_ui,
        microphone_status=mic_ui,
        action_required_message=None,
        raw_status=status_payload,
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
        self._diagnostics_expanded: bool = False
        self._last_raw_status: Optional[Dict[str, Any]] = None

        self.root.title("Cross-Desk Flow")
        self.root.geometry("460x360")
        self.root.minsize(420, 320)

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
        self.container = ttk.Frame(self.root, padding="20 20 20 20")
        self.container.pack(fill=tk.BOTH, expand=True)

        # 1. Overall Status Card
        card = ttk.LabelFrame(self.container, text=" System Status ", padding="12 10 12 10")
        card.pack(fill=tk.X, pady=(0, 12))

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
        dir_frame = ttk.LabelFrame(self.container, text=" Audio Streams ", padding="12 10 12 10")
        dir_frame.pack(fill=tk.X, pady=(0, 10))

        # Row 1: PC -> Mac Speaker
        row1 = ttk.Frame(dir_frame)
        row1.pack(fill=tk.X, pady=3)
        lbl_spk_title = ttk.Label(row1, text="PC → Mac Speaker", font=("System", 12))
        lbl_spk_title.pack(side=tk.LEFT)
        self.lbl_spk_status = ttk.Label(row1, text="", font=("System", 12, "bold"))
        self.lbl_spk_status.pack(side=tk.RIGHT)

        # Row 2: Mac -> PC Microphone
        row2 = ttk.Frame(dir_frame)
        row2.pack(fill=tk.X, pady=3)
        lbl_mic_title = ttk.Label(row2, text="Mac → PC Microphone", font=("System", 12))
        lbl_mic_title.pack(side=tk.LEFT)
        self.lbl_mic_status = ttk.Label(row2, text="", font=("System", 12, "bold"))
        self.lbl_mic_status.pack(side=tk.RIGHT)

        # 3. Action / Error Banner
        self.lbl_action_banner = ttk.Label(
            self.container,
            text="",
            font=("System", 11),
            foreground="#D32F2F",
            wraplength=410,
        )
        self.lbl_action_banner.pack(fill=tk.X, pady=(0, 8))

        # 4. Diagnostics & Recovery Frame (expandable)
        self.diag_frame = ttk.LabelFrame(self.container, text=" Diagnostics & Recovery ", padding="10 8 10 8")

        # Diagnostics grid (key-value labels)
        grid_frame = ttk.Frame(self.diag_frame)
        grid_frame.pack(fill=tk.X, pady=(0, 8))

        fields = [
            ("Background Service:", "diag_service_val"),
            ("Peer:", "diag_peer_val"),
            ("Network Path:", "diag_net_val"),
            ("Auto Start / LaunchAgent:", "diag_autostart_val"),
            ("Speaker:", "diag_spk_val"),
            ("Microphone:", "diag_mic_val"),
            ("Voice / Mode:", "diag_voice_val"),
            ("Last Error:", "diag_err_val"),
        ]

        for idx, (label_text, attr_name) in enumerate(fields):
            row_f = ttk.Frame(grid_frame)
            row_f.pack(fill=tk.X, pady=1)
            lbl_k = ttk.Label(row_f, text=label_text, font=("System", 10), foreground="#555555")
            lbl_k.pack(side=tk.LEFT)
            lbl_v = ttk.Label(row_f, text="—", font=("System", 10, "bold"), foreground="#222222")
            lbl_v.pack(side=tk.RIGHT)
            setattr(self, attr_name, lbl_v)

        # Diagnostics action buttons: Restart / Reconcile, Open Logs, Copy Report
        diag_btn_frame = ttk.Frame(self.diag_frame)
        diag_btn_frame.pack(fill=tk.X, pady=(4, 0))

        self.btn_reconcile = ttk.Button(
            diag_btn_frame,
            text="Restart / Reconcile",
            command=self.on_reconcile,
        )
        self.btn_reconcile.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_open_logs = ttk.Button(
            diag_btn_frame,
            text="Open Logs",
            command=self.on_open_logs,
        )
        self.btn_open_logs.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_copy_report = ttk.Button(
            diag_btn_frame,
            text="Copy Report",
            command=self.on_copy_report,
        )
        self.btn_copy_report.pack(side=tk.RIGHT)

        # 5. Primary Control Buttons (Start, Stop, Diagnostics Toggle, Refresh)
        btn_frame = ttk.Frame(self.container)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)

        self.btn_start = ttk.Button(btn_frame, text="Start", command=self.on_start)
        self.btn_start.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_stop = ttk.Button(btn_frame, text="Stop", command=self.on_stop)
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_diag_toggle = ttk.Button(
            btn_frame,
            text="Diagnostics ▼",
            command=self.on_toggle_diagnostics,
        )
        self.btn_diag_toggle.pack(side=tk.LEFT, padx=(0, 6))

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

        self._last_raw_status = status_data
        state = map_controller_status_to_ui(status_data)
        self._apply_ui_state(state)

        if self._diagnostics_expanded:
            self._update_diagnostics_view(status_data)

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
            DIR_OFF: "#757575",
        }
        self.lbl_spk_status.config(foreground=dir_color_map.get(state.speaker_status, "#000000"))
        self.lbl_mic_status.config(foreground=dir_color_map.get(state.microphone_status, "#000000"))

        if state.action_required_message:
            self.lbl_action_banner.config(text=f"Action required — {state.action_required_message}")
        else:
            self.lbl_action_banner.config(text="")

    def _update_diagnostics_view(self, status_dict: Optional[Dict[str, Any]]):
        """Updates diagnostic key-value fields from raw controller status."""
        data = get_diagnostics_view_data(status_dict)
        self.diag_service_val.config(text=data["service"][0], foreground=data["service"][1])
        self.diag_peer_val.config(text=data["peer"][0], foreground=data["peer"][1])
        self.diag_net_val.config(text=data["net"][0], foreground=data["net"][1])
        self.diag_autostart_val.config(text=data["autostart"][0], foreground=data["autostart"][1])
        self.diag_spk_val.config(text=data["spk"][0], foreground=data["spk"][1])
        self.diag_mic_val.config(text=data["mic"][0], foreground=data["mic"][1])
        self.diag_voice_val.config(text=data["voice"][0], foreground=data["voice"][1])
        self.diag_err_val.config(text=data["err"][0], foreground=data["err"][1])
        self.btn_open_logs.config(text="Open Logs" if has_log_files() else "Open Data Folder")

    def on_toggle_diagnostics(self):
        """Toggles visibility of the Diagnostics & Recovery panel."""
        self._diagnostics_expanded = not self._diagnostics_expanded
        if self._diagnostics_expanded:
            self.btn_diag_toggle.config(text="Diagnostics ▲")
            self.diag_frame.pack(fill=tk.X, pady=(0, 10), before=self.btn_start.master)
            self.root.geometry("460x600")
            self._update_diagnostics_view(self._last_raw_status)
        else:
            self.btn_diag_toggle.config(text="Diagnostics ▼")
            self.diag_frame.pack_forget()
            self.root.geometry("460x360")

    def on_reconcile(self):
        """Sends reconcile to controller or recovers dead controller via LaunchAgent."""
        if self._last_raw_status is not None:
            try:
                self.ipc_client("reconcile")
            except Exception:
                pass
        else:
            # Controller host is dead: use existing LaunchAgent lifecycle authority seam
            self.btn_reconcile.config(text="Starting...")
            try:
                self.root.update_idletasks()
            except Exception:
                pass
            start_controller_via_lifecycle(timeout_sec=5.0)
            self.btn_reconcile.config(text="Restart / Reconcile")
        self.refresh()

    def on_open_logs(self):
        """Opens logs or data directory in macOS Finder."""
        open_data_directory()

    def on_copy_report(self):
        """Generates sanitized diagnostic report and copies to system clipboard."""
        report = build_diagnostic_report(self._last_raw_status)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(report)
            old_text = self.btn_copy_report.cget("text")
            self.btn_copy_report.config(text="Copied!")
            self.root.after(1500, lambda: self.btn_copy_report.config(text=old_text))
        except Exception:
            pass

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
