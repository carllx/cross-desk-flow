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


# Overall status constants
OVERALL_CONNECTED = "Connected"
OVERALL_WAITING_FOR_MAC = "Waiting for Mac"
OVERALL_DEGRADED = "Degraded"
OVERALL_STOPPED = "Stopped"
OVERALL_ACTION_REQUIRED = "Action required"

# Mode constants
MODE_PLAYBACK = "Playback"
MODE_DICTATION = "Dictation"

# Voice input row constants
VOICE_INPUT_STANDBY = "Automatic / Standby"
VOICE_INPUT_ACTIVE = "Active"
VOICE_INPUT_OFF = "Standby / Off"

# Direction row status constants
DIRECTION_ACTIVE = "Active"
DIRECTION_WAITING = "Waiting"
DIRECTION_STOPPED = "Stopped"
DIRECTION_PROBLEM = "Problem"
DIRECTION_OFF = "Off"
DIRECTION_STANDBY = "Standby"
DIRECTION_PAUSED_VOICE = "Paused for voice input"


@dataclass
class ShellState:
    """Represents user-facing mapped status for the Product Shell."""
    overall: str
    speaker: str
    microphone: str
    mode: str = MODE_PLAYBACK
    voice_input: str = VOICE_INPUT_STANDBY
    actionable_error: Optional[str] = None
    raw_status: Optional[Dict[str, Any]] = None


def map_ui_state(status_dict: Optional[Dict[str, Any]]) -> ShellState:
    """Pure mapping function translating controller status into user-facing UI state.
    
    Guarantees that raw internal enums/PIDs/IPs are never exposed directly,
    and maps failure/unavailable states into clear semantic indicators.
    """
    if status_dict is None:
        return ShellState(
            overall=OVERALL_ACTION_REQUIRED,
            speaker=DIRECTION_STOPPED,
            microphone=DIRECTION_STOPPED,
            mode=MODE_PLAYBACK,
            voice_input=VOICE_INPUT_OFF,
            actionable_error="Background service is not running",
            raw_status=None,
        )

    desired_state = status_dict.get("desired_state")
    controller_state = status_dict.get("controller_state")
    peer_available = bool(status_dict.get("peer_available", False))
    spk_state = status_dict.get("speaker_path_state")
    mic_state = status_dict.get("microphone_path_state")

    # Mode determination
    has_explicit_mode = "mode" in status_dict
    raw_mode = (status_dict.get("mode") or "").upper()
    if raw_mode == "DICTATION" or (not has_explicit_mode and mic_state == "RUNNING" and spk_state != "RUNNING"):
        current_mode = MODE_DICTATION
    else:
        current_mode = MODE_PLAYBACK

    # Collect actionable errors
    spk_err = status_dict.get("last_actionable_error")
    mic_err = status_dict.get("last_actionable_microphone_error")
    errors = [e for e in [spk_err, mic_err] if e]
    actionable_error = "\n".join(errors) if errors else None

    # Explicit user stop overrides active state
    if desired_state == "STOPPED_BY_USER" or controller_state == "STOPPED":
        return ShellState(
            overall=OVERALL_STOPPED,
            speaker=DIRECTION_STOPPED,
            microphone=DIRECTION_STOPPED,
            mode=MODE_PLAYBACK,
            voice_input=VOICE_INPUT_OFF,
            actionable_error=actionable_error,
            raw_status=status_dict,
        )

    # Voice input status
    if current_mode == MODE_DICTATION:
        voice_input = VOICE_INPUT_ACTIVE
    else:
        voice_input = VOICE_INPUT_STANDBY

    # Map speaker direction row
    if spk_state in ("UNAVAILABLE", "FAILED"):
        speaker_row = DIRECTION_PROBLEM
    elif current_mode == MODE_DICTATION and mic_state == "RUNNING":
        speaker_row = DIRECTION_PAUSED_VOICE
    elif spk_state == "RUNNING":
        speaker_row = DIRECTION_ACTIVE
    elif spk_state == "STOPPED":
        speaker_row = DIRECTION_STOPPED
    else:
        speaker_row = DIRECTION_WAITING

    # Map microphone direction row
    if current_mode == MODE_DICTATION:
        if mic_state == "RUNNING":
            microphone_row = DIRECTION_ACTIVE
        elif mic_state in ("UNAVAILABLE", "FAILED"):
            microphone_row = DIRECTION_PROBLEM
        elif mic_state == "STOPPED":
            microphone_row = DIRECTION_STOPPED
        else:
            microphone_row = DIRECTION_WAITING
    else:
        # In Playback mode: Mac -> PC Microphone is Standby
        if mic_state in ("UNAVAILABLE", "FAILED"):
            microphone_row = DIRECTION_PROBLEM
        elif mic_state == "RUNNING":
            microphone_row = DIRECTION_ACTIVE
        elif not has_explicit_mode and not peer_available:
            microphone_row = DIRECTION_WAITING
        else:
            microphone_row = DIRECTION_STANDBY

    # Determine overall status
    if controller_state == "ERROR" and speaker_row not in (DIRECTION_ACTIVE, DIRECTION_PAUSED_VOICE) and microphone_row != DIRECTION_ACTIVE:
        overall = OVERALL_ACTION_REQUIRED
    elif not peer_available:
        overall = OVERALL_WAITING_FOR_MAC
    elif (speaker_row in (DIRECTION_ACTIVE, DIRECTION_PAUSED_VOICE)) and microphone_row == DIRECTION_ACTIVE:
        overall = OVERALL_CONNECTED
    elif (speaker_row in (DIRECTION_ACTIVE, DIRECTION_PAUSED_VOICE) and microphone_row == DIRECTION_PROBLEM) or \
         (microphone_row == DIRECTION_ACTIVE and speaker_row == DIRECTION_PROBLEM):
        overall = OVERALL_DEGRADED
    elif current_mode == MODE_PLAYBACK and speaker_row == DIRECTION_ACTIVE:
        overall = OVERALL_CONNECTED
    elif current_mode == MODE_DICTATION and microphone_row == DIRECTION_ACTIVE:
        overall = OVERALL_CONNECTED
    elif speaker_row == DIRECTION_PROBLEM or microphone_row == DIRECTION_PROBLEM:
        overall = OVERALL_ACTION_REQUIRED
    elif speaker_row == DIRECTION_WAITING or microphone_row == DIRECTION_WAITING:
        overall = OVERALL_WAITING_FOR_MAC
    else:
        overall = OVERALL_STOPPED

    return ShellState(
        overall=overall,
        speaker=speaker_row,
        microphone=microphone_row,
        mode=current_mode,
        voice_input=voice_input,
        actionable_error=actionable_error,
        raw_status=status_dict,
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


class ProductShellApp:
    """Tkinter Desktop Window for Cross-Desk Flow."""

    def __init__(self, root: tk.Tk, client: Optional[ProductShellClient] = None):
        self.root = root
        self.client = client or ProductShellClient()
        self._timer_id: Optional[str] = None

        self._setup_window()
        self._build_ui()
        self.refresh()
        self._schedule_refresh()

    def _setup_window(self):
        self.root.title("Cross-Desk Flow")
        self.root.geometry("460x400")
        self.root.minsize(420, 370)
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

        # Action Buttons (Start, Stop, Refresh)
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

        self.dictation_btn = tk.Button(
            btn_frame,
            text="Start Dictation",
            font=("Segoe UI", 9, "bold"),
            bg="#0284c7",
            fg="#ffffff",
            activebackground="#0369a1",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            padx=14,
            pady=6,
            cursor="hand2",
            command=self.on_dictation_toggle,
        )
        self.dictation_btn.pack(side=tk.LEFT, padx=(0, 6))

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
                bg="#dc2626",
                activebackground="#b91c1c",
            )
        else:
            self.dictation_btn.config(
                text="Start Dictation",
                bg="#0284c7",
                activebackground="#0369a1",
            )

        # Update actionable error notice
        if state.actionable_error:
            self.error_label.config(text=state.actionable_error)
            self.error_frame.pack(fill=tk.X, pady=(0, 8), before=self.start_btn.master)
        else:
            self.error_frame.pack_forget()

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
