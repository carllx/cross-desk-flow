"""Windows Product MVP Shell for desk-audio-bridge / cross-desk-flow.

Provides a compact desktop GUI displaying overall and per-direction bridge status,
interacting strictly via IPC with the existing background controller service.
Zero lifecycle authority or direct process management.
"""

from dataclasses import dataclass
import os
import sys
import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, Optional

# Set up repository root on sys.path if not present
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Ensure stdout and stderr do not raise errors when launched under pythonw.exe
if sys.stdout is None:
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass

if sys.stderr is None:
    try:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass

from bridge_core.contract import DEFAULT_LOCAL_IPC_PORT
from windows.cli import send_ipc_command
from windows.diagnostics import (
    DIRECTION_ACTIVE,
    DIRECTION_OFF,
    DIRECTION_PAUSED_VOICE,
    DIRECTION_PROBLEM,
    DIRECTION_STANDBY,
    DIRECTION_STOPPED,
    DIRECTION_WAITING,
    MODE_DICTATION,
    MODE_PLAYBACK,
    OVERALL_ACTION_REQUIRED,
    OVERALL_CONNECTED,
    OVERALL_DEGRADED,
    OVERALL_STOPPED,
    OVERALL_WAITING_FOR_MAC,
    ShellState,
    VOICE_INPUT_ACTIVE,
    VOICE_INPUT_OFF,
    VOICE_INPUT_STANDBY,
    build_diagnostic_report,
    classify_network_path,
    get_autostart_status,
    get_diagnostics_view_data,
    map_ui_state,
    open_log_directory,
)




class ProductShellClient:
    """IPC client adapter for the product shell.
    
    Strictly queries or sends commands via loopback TCP socket.
    Never spawns, kills, or manages background services directly.
    """

    def __init__(self, port: int = DEFAULT_LOCAL_IPC_PORT):
        self.port = port

    def get_status(self) -> Optional[Dict[str, Any]]:
        return send_ipc_command("status", port=self.port, timeout=1.0)

    def start(self) -> Optional[Dict[str, Any]]:
        return send_ipc_command("start", port=self.port, timeout=2.0)

    def stop(self) -> Optional[Dict[str, Any]]:
        return send_ipc_command("stop", port=self.port, timeout=2.0)

    def start_dictation(self) -> Optional[Dict[str, Any]]:
        return send_ipc_command("dictation-start", port=self.port, timeout=4.0)

    def end_dictation(self) -> Optional[Dict[str, Any]]:
        return send_ipc_command("dictation-end", port=self.port, timeout=3.0)

    def reconcile(self) -> Optional[Dict[str, Any]]:
        return send_ipc_command("reconcile", port=self.port, timeout=3.0)


class ProductShellApp:
    """Tkinter Desktop Window for Cross-Desk Flow."""

    def __init__(self, root: tk.Tk, client: Optional[ProductShellClient] = None):
        self.root = root
        self.client = client or ProductShellClient()
        self._timer_id: Optional[str] = None
        self._diagnostics_expanded: bool = False
        self._last_raw_status: Optional[Dict[str, Any]] = None

        self._setup_window()
        self._build_ui()
        self.refresh()
        self._schedule_refresh()

    def _setup_window(self):
        self.root.title("Cross-Desk Flow")
        self.root.geometry("480x420")
        self.root.minsize(440, 390)
        self.root.configure(bg="#f8fafc")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Style configuration
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

    def _build_ui(self):
        main_container = tk.Frame(self.root, bg="#f8fafc", padx=20, pady=16)
        main_container.pack(fill=tk.BOTH, expand=True)

        # Overall Status Card
        status_card = tk.Frame(
            main_container,
            bg="#ffffff",
            bd=1,
            relief=tk.SOLID,
            highlightbackground="#e2e8f0",
            highlightthickness=1,
            padx=14,
            pady=12,
        )
        status_card.pack(fill=tk.X, pady=(0, 14))

        status_header = tk.Label(
            status_card,
            text="STATUS",
            font=("Segoe UI", 9, "bold"),
            fg="#64748b",
            bg="#ffffff",
            anchor="w",
        )
        status_header.pack(fill=tk.X)

        self.overall_label = tk.Label(
            status_card,
            text="Checking...",
            font=("Segoe UI", 16, "bold"),
            fg="#0f172a",
            bg="#ffffff",
            anchor="w",
        )
        self.overall_label.pack(fill=tk.X, pady=(2, 0))

        self.mode_label = tk.Label(
            status_card,
            text="Mode: Playback",
            font=("Segoe UI", 10, "bold"),
            fg="#2563eb",
            bg="#ffffff",
            anchor="w",
        )
        self.mode_label.pack(fill=tk.X, pady=(2, 0))

        # Direction Rows Card
        directions_card = tk.Frame(
            main_container,
            bg="#ffffff",
            bd=1,
            relief=tk.SOLID,
            highlightbackground="#e2e8f0",
            highlightthickness=1,
            padx=14,
            pady=12,
        )
        directions_card.pack(fill=tk.X, pady=(0, 10))

        # Row: PC -> Mac Speaker
        spk_frame = tk.Frame(directions_card, bg="#ffffff")
        spk_frame.pack(fill=tk.X, pady=4)

        spk_title = tk.Label(
            spk_frame,
            text="PC → Mac Speaker",
            font=("Segoe UI", 10, "normal"),
            fg="#1e293b",
            bg="#ffffff",
            anchor="w",
        )
        spk_title.pack(side=tk.LEFT)

        self.spk_status_label = tk.Label(
            spk_frame,
            text="—",
            font=("Segoe UI", 10, "bold"),
            fg="#475569",
            bg="#f1f5f9",
            padx=8,
            pady=2,
        )
        self.spk_status_label.pack(side=tk.RIGHT)

        # Separator line
        sep = tk.Frame(directions_card, height=1, bg="#e2e8f0")
        sep.pack(fill=tk.X, pady=6)

        # Row: Mac -> PC Microphone
        mic_frame = tk.Frame(directions_card, bg="#ffffff")
        mic_frame.pack(fill=tk.X, pady=4)

        mic_title = tk.Label(
            mic_frame,
            text="Mac → PC Microphone",
            font=("Segoe UI", 10, "normal"),
            fg="#1e293b",
            bg="#ffffff",
            anchor="w",
        )
        mic_title.pack(side=tk.LEFT)

        self.mic_status_label = tk.Label(
            mic_frame,
            text="—",
            font=("Segoe UI", 10, "bold"),
            fg="#475569",
            bg="#f1f5f9",
            padx=8,
            pady=2,
        )
        self.mic_status_label.pack(side=tk.RIGHT)

        # Separator line
        sep2 = tk.Frame(directions_card, height=1, bg="#e2e8f0")
        sep2.pack(fill=tk.X, pady=6)

        # Row: Voice Input (Automatic Dictation)
        voice_frame = tk.Frame(directions_card, bg="#ffffff")
        voice_frame.pack(fill=tk.X, pady=4)

        voice_title = tk.Label(
            voice_frame,
            text="Voice Input",
            font=("Segoe UI", 10, "normal"),
            fg="#1e293b",
            bg="#ffffff",
            anchor="w",
        )
        voice_title.pack(side=tk.LEFT)

        self.voice_status_label = tk.Label(
            voice_frame,
            text="—",
            font=("Segoe UI", 10, "bold"),
            fg="#475569",
            bg="#f1f5f9",
            padx=8,
            pady=2,
        )
        self.voice_status_label.pack(side=tk.RIGHT)

        # Actionable Error Notice Box
        self.error_frame = tk.Frame(
            main_container,
            bg="#fef2f2",
            bd=1,
            relief=tk.SOLID,
            highlightbackground="#fecaca",
            highlightthickness=1,
            padx=10,
            pady=6,
        )
        self.error_label = tk.Label(
            self.error_frame,
            text="",
            font=("Segoe UI", 9),
            fg="#b91c1c",
            bg="#fef2f2",
            justify=tk.LEFT,
            wraplength=380,
            anchor="w",
        )
        self.error_label.pack(fill=tk.X)

        # Action Buttons (Start, Stop, Diagnostics toggle, Refresh)
        btn_frame = tk.Frame(main_container, bg="#f8fafc")
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM, pady=(8, 0))

        self.start_btn = tk.Button(
            btn_frame,
            text="Start",
            font=("Segoe UI", 9, "bold"),
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            padx=12,
            pady=6,
            cursor="hand2",
            command=self.on_start,
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.stop_btn = tk.Button(
            btn_frame,
            text="Stop",
            font=("Segoe UI", 9),
            bg="#e2e8f0",
            fg="#1e293b",
            activebackground="#cbd5e1",
            activeforeground="#1e293b",
            relief=tk.FLAT,
            padx=12,
            pady=6,
            cursor="hand2",
            command=self.on_stop,
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.diag_toggle_btn = tk.Button(
            btn_frame,
            text="Diagnostics ▼",
            font=("Segoe UI", 9),
            bg="#f1f5f9",
            fg="#334155",
            activebackground="#e2e8f0",
            activeforeground="#1e293b",
            relief=tk.FLAT,
            padx=10,
            pady=6,
            cursor="hand2",
            command=self.on_toggle_diagnostics,
        )
        self.diag_toggle_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.refresh_btn = tk.Button(
            btn_frame,
            text="Refresh",
            font=("Segoe UI", 9),
            bg="#f1f5f9",
            fg="#475569",
            activebackground="#e2e8f0",
            activeforeground="#1e293b",
            relief=tk.FLAT,
            padx=10,
            pady=6,
            cursor="hand2",
            command=self.on_refresh,
        )
        self.refresh_btn.pack(side=tk.RIGHT)

        # Diagnostics Collapsible Container
        self.diag_frame = tk.Frame(
            main_container,
            bg="#ffffff",
            bd=1,
            relief=tk.SOLID,
            highlightbackground="#cbd5e1",
            highlightthickness=1,
            padx=12,
            pady=10,
        )
        # Note: diag_frame is packed/unpacked dynamically via on_toggle_diagnostics

        diag_header = tk.Label(
            self.diag_frame,
            text="DIAGNOSTICS & RECOVERY",
            font=("Segoe UI", 8, "bold"),
            fg="#64748b",
            bg="#ffffff",
            anchor="w",
        )
        diag_header.pack(fill=tk.X, pady=(0, 6))

        # Grid of diagnostic items
        diag_grid = tk.Frame(self.diag_frame, bg="#ffffff")
        diag_grid.pack(fill=tk.X, pady=(0, 8))
        diag_grid.columnconfigure(1, weight=1)

        def make_row(parent, row_idx, label_text):
            lbl = tk.Label(
                parent,
                text=label_text,
                font=("Segoe UI", 8, "normal"),
                fg="#475569",
                bg="#ffffff",
                anchor="w",
            )
            lbl.grid(row=row_idx, column=0, sticky="w", pady=1)
            val = tk.Label(
                parent,
                text="—",
                font=("Segoe UI", 8, "bold"),
                fg="#0f172a",
                bg="#ffffff",
                anchor="e",
            )
            val.grid(row=row_idx, column=1, sticky="e", pady=1)
            return val

        self.diag_service_val = make_row(diag_grid, 0, "Background Service:")
        self.diag_peer_val = make_row(diag_grid, 1, "Peer Connectivity:")
        self.diag_net_val = make_row(diag_grid, 2, "Network Path:")
        self.diag_autostart_val = make_row(diag_grid, 3, "Auto Start:")
        self.diag_pack43_val = make_row(diag_grid, 4, "Pack43 Readiness:")
        self.diag_spk_val = make_row(diag_grid, 5, "Speaker Path:")
        self.diag_mic_val = make_row(diag_grid, 6, "Microphone Path:")
        self.diag_voice_val = make_row(diag_grid, 7, "Voice Input:")
        self.diag_err_val = make_row(diag_grid, 8, "Last Error:")

        # Diagnostic Action Buttons
        diag_actions = tk.Frame(self.diag_frame, bg="#ffffff")
        diag_actions.pack(fill=tk.X, pady=(4, 0))

        self.reconcile_btn = tk.Button(
            diag_actions,
            text="Restart / Reconcile",
            font=("Segoe UI", 8, "bold"),
            bg="#e0f2fe",
            fg="#0369a1",
            activebackground="#bae6fd",
            activeforeground="#0369a1",
            relief=tk.FLAT,
            padx=6,
            pady=4,
            cursor="hand2",
            command=self.on_reconcile,
        )
        self.reconcile_btn.pack(side=tk.LEFT, padx=(0, 4))

        self.open_logs_btn = tk.Button(
            diag_actions,
            text="Open Logs",
            font=("Segoe UI", 8),
            bg="#f1f5f9",
            fg="#334155",
            activebackground="#e2e8f0",
            activeforeground="#1e293b",
            relief=tk.FLAT,
            padx=6,
            pady=4,
            cursor="hand2",
            command=self.on_open_logs,
        )
        self.open_logs_btn.pack(side=tk.LEFT, padx=(0, 4))

        self.copy_report_btn = tk.Button(
            diag_actions,
            text="Copy Report",
            font=("Segoe UI", 8),
            bg="#f1f5f9",
            fg="#334155",
            activebackground="#e2e8f0",
            activeforeground="#1e293b",
            relief=tk.FLAT,
            padx=6,
            pady=4,
            cursor="hand2",
            command=self.on_copy_report,
        )
        self.copy_report_btn.pack(side=tk.LEFT, padx=(0, 4))

        # Advanced / Manual Dictation override inside Diagnostics
        self.dictation_btn = tk.Button(
            diag_actions,
            text="Start Dictation",
            font=("Segoe UI", 8, "bold"),
            bg="#f1f5f9",
            fg="#0284c7",
            activebackground="#e0f2fe",
            activeforeground="#0369a1",
            relief=tk.FLAT,
            padx=6,
            pady=4,
            cursor="hand2",
            command=self.on_dictation_toggle,
        )
        self.dictation_btn.pack(side=tk.RIGHT)

    def _get_badge_colors(self, status: str):
        """Returns (foreground, background) for a given status string."""
        if status in (OVERALL_CONNECTED, DIRECTION_ACTIVE, VOICE_INPUT_ACTIVE):
            return "#15803d", "#dcfce7"  # Green
        elif status in (OVERALL_WAITING_FOR_MAC, DIRECTION_WAITING):
            return "#b45309", "#fef3c7"  # Amber
        elif status == OVERALL_DEGRADED:
            return "#c2410c", "#ffedd5"  # Orange/Amber
        elif status in (OVERALL_STOPPED, DIRECTION_STOPPED, DIRECTION_OFF, VOICE_INPUT_OFF):
            return "#475569", "#f1f5f9"  # Slate/Gray
        elif status in (DIRECTION_STANDBY, VOICE_INPUT_STANDBY):
            return "#0369a1", "#e0f2fe"  # Sky blue
        elif status in (DIRECTION_PAUSED_VOICE,):
            return "#6d28d9", "#ede9fe"  # Purple
        elif status in (OVERALL_ACTION_REQUIRED, DIRECTION_PROBLEM):
            return "#b91c1c", "#fee2e2"  # Red
        return "#475569", "#f1f5f9"

    def refresh(self):
        """Fetches latest status from IPC client and updates the surface."""
        raw_status = self.client.get_status()
        self._last_raw_status = raw_status
        state = map_ui_state(raw_status)

        # Update overall status
        fg, bg = self._get_badge_colors(state.overall)
        self.overall_label.config(text=state.overall, fg=fg)

        # Update mode label
        self.mode_label.config(
            text=f"Mode: {state.mode}",
            fg="#7c3aed" if state.mode == MODE_DICTATION else "#2563eb",
        )

        # Update PC -> Mac Speaker row
        spk_fg, spk_bg = self._get_badge_colors(state.speaker)
        self.spk_status_label.config(text=state.speaker, fg=spk_fg, bg=spk_bg)

        # Update Mac -> PC Microphone row
        mic_fg, mic_bg = self._get_badge_colors(state.microphone)
        self.mic_status_label.config(text=state.microphone, fg=mic_fg, bg=mic_bg)

        # Update Voice Input row
        voice_fg, voice_bg = self._get_badge_colors(state.voice_input)
        self.voice_status_label.config(text=state.voice_input, fg=voice_fg, bg=voice_bg)

        # Update dictation button
        if state.mode == MODE_DICTATION:
            self.dictation_btn.config(
                text="End Dictation",
                bg="#fef2f2",
                fg="#b91c1c",
                activebackground="#fee2e2",
                activeforeground="#991b1b",
            )
        else:
            self.dictation_btn.config(
                text="Start Dictation",
                bg="#f1f5f9",
                fg="#0284c7",
                activebackground="#e0f2fe",
                activeforeground="#0369a1",
            )

        # Update actionable error notice
        if state.actionable_error:
            self.error_label.config(text=state.actionable_error)
            self.error_frame.pack(fill=tk.X, pady=(0, 8), before=self.start_btn.master)
        else:
            self.error_frame.pack_forget()

        # Update diagnostics panel if expanded
        if self._diagnostics_expanded:
            self._update_diagnostics_view(raw_status)

    def _update_diagnostics_view(self, status_dict: Optional[Dict[str, Any]]):
        """Updates diagnostic key-value fields from raw controller status."""
        data = get_diagnostics_view_data(status_dict)
        self.diag_service_val.config(text=data["service"][0], fg=data["service"][1])
        self.diag_peer_val.config(text=data["peer"][0], fg=data["peer"][1])
        self.diag_net_val.config(text=data["net"][0], fg=data["net"][1])
        self.diag_autostart_val.config(text=data["autostart"][0], fg=data["autostart"][1])
        self.diag_pack43_val.config(text=data["pack43"][0], fg=data["pack43"][1])
        self.diag_spk_val.config(text=data["spk"][0], fg=data["spk"][1])
        self.diag_mic_val.config(text=data["mic"][0], fg=data["mic"][1])
        self.diag_voice_val.config(text=data["voice"][0], fg=data["voice"][1])
        self.diag_err_val.config(text=data["err"][0], fg=data["err"][1])

    def on_toggle_diagnostics(self):
        """Toggles visibility of the Diagnostics & Recovery panel."""
        self._diagnostics_expanded = not self._diagnostics_expanded
        if self._diagnostics_expanded:
            self.diag_toggle_btn.config(text="Diagnostics ▲")
            self.diag_frame.pack(fill=tk.X, pady=(0, 10), before=self.start_btn.master)
            self.root.geometry("480x680")
            self._update_diagnostics_view(self._last_raw_status)
        else:
            self.diag_toggle_btn.config(text="Diagnostics ▼")
            self.diag_frame.pack_forget()
            self.root.geometry("480x420")

    def on_reconcile(self):
        """Sends reconcile or start command to safely restore/restart bridge paths."""
        if self._last_raw_status is not None:
            self.client.reconcile()
        else:
            self.client.start()
        self.refresh()

    def on_open_logs(self):
        """Opens log directory in Windows File Explorer."""
        open_log_directory()

    def on_copy_report(self):
        """Generates sanitized diagnostic report and copies to system clipboard."""
        report = build_diagnostic_report(self._last_raw_status)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(report)
            old_text = self.copy_report_btn.cget("text")
            self.copy_report_btn.config(text="Copied!")
            self.root.after(1500, lambda: self.copy_report_btn.config(text=old_text))
        except Exception:
            pass

    def on_dictation_toggle(self):
        """IPC dictation start/end toggle callback."""
        btn_text = self.dictation_btn.cget("text")
        if btn_text == "End Dictation":
            self.client.end_dictation()
        else:
            self.client.start_dictation()
        self.refresh()


    def _schedule_refresh(self):
        """Schedules auto-refresh approximately every 1 second."""
        self._timer_id = self.root.after(1000, self._on_timer)

    def _on_timer(self):
        self.refresh()
        self._schedule_refresh()

    def on_start(self):
        """IPC start command callback."""
        self.client.start()
        self.refresh()

    def on_stop(self):
        """IPC stop command callback."""
        self.client.stop()
        self.refresh()

    def on_refresh(self):
        """Manual refresh callback."""
        self.refresh()

    def _on_close(self):
        """Clean shutdown of UI window and timers."""
        if self._timer_id:
            try:
                self.root.after_cancel(self._timer_id)
            except Exception:
                pass
            self._timer_id = None
        self.root.destroy()


def main():
    root = tk.Tk()
    app = ProductShellApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
