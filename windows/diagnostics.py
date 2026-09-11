"""Diagnostics and Recovery helper for Windows Cross-Desk Flow.

Provides:
- Network route classification (Ethernet / Wi-Fi / Fallback / Unknown)
- Task Scheduler autostart query (Installed / Not Installed / Unknown)
- Diagnostic report generation and sanitization
- Safe explorer opening for log directory
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from typing import Any, Dict, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from dataclasses import dataclass

from bridge_core.interface_classifier import InterfaceClassifier, InterfaceMedium


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


def get_log_directory() -> str:
    """Returns the canonical Windows log and state directory."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return os.path.join(local_app_data, "desk-audio-bridge")
    return os.path.join(os.path.expanduser("~"), "AppData", "Local", "desk-audio-bridge")


def open_log_directory() -> bool:
    """Opens the log directory in Windows File Explorer safely."""
    log_dir = get_log_directory()
    os.makedirs(log_dir, exist_ok=True)
    try:
        if hasattr(os, "startfile"):
            os.startfile(log_dir)
            return True
        subprocess.Popen(["explorer.exe", log_dir])
        return True
    except Exception:
        return False


def classify_network_path(local_bind: Optional[str], classifier: Optional[InterfaceClassifier] = None) -> str:
    """Classifies network route for local binding address.
    
    Returns one of: 'Ethernet', 'Wi-Fi', 'Fallback (other)', or 'Unknown'.
    """
    if not local_bind or local_bind in ("0.0.0.0", "127.0.0.1"):
        return "Unknown"
    cls = classifier or InterfaceClassifier()
    try:
        medium = cls.classify_interface(local_bind)
        if medium == InterfaceMedium.WIRED_ETHERNET:
            return "Ethernet"
        if medium == InterfaceMedium.WIFI:
            return "Wi-Fi"
        if medium == InterfaceMedium.OTHER:
            return "Fallback (other)"
    except Exception:
        pass
    return "Unknown"


def get_autostart_status() -> str:
    """Queries current-user Task Scheduler autostart status."""
    try:
        from windows.task_scheduler import is_scheduled_task_installed
        installed = is_scheduled_task_installed()
        return "Installed" if installed else "Not installed"
    except Exception:
        return "Unknown"


def get_deployed_sha(repo_root: Optional[str] = None) -> str:
    """Queries current deployed git commit SHA if repository exists."""
    target_dir = repo_root or REPO_ROOT
    try:
        res = subprocess.run(
            ["git", "-C", target_dir, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return "unknown"


def build_diagnostic_report(
    status_dict: Optional[Dict[str, Any]],
    repo_root: Optional[str] = None,
    classifier: Optional[InterfaceClassifier] = None,
) -> str:
    """Builds a human- and machine-readable plain text diagnostic report.
    
    Guarantees that credentials, tokens, sensitive paths, and raw hardware GUIDs
    are never included.
    """
    deployed_sha = get_deployed_sha(repo_root)
    platform_info = f"Windows {platform.release()} (Version {platform.version()})"
    autostart = get_autostart_status()

    if status_dict is None:
        return (
            "=== Cross-Desk Flow Diagnostic Report ===\n"
            f"Deployed SHA: {deployed_sha}\n"
            f"Platform: {platform_info}\n"
            "Controller: NOT RUNNING\n"
            "Desired State: UNKNOWN\n"
            "Peer State: NONE\n"
            "Network Path: Unknown\n"
            f"Auto Start: {autostart}\n"
            "Speaker Path: STOPPED\n"
            "Microphone Path: STOPPED\n"
            "Voice Input: Standby / Off\n"
            "Pack43 Readiness: Unprobed\n"
            "Last Actionable Error: Background service is not running\n"
            "========================================="
        )

    # Controller and desired state
    controller_state = status_dict.get("controller_state", "UNKNOWN")
    desired_state = status_dict.get("desired_state", "UNKNOWN")
    owner_pid = status_dict.get("owner_pid")
    controller_line = f"RUNNING (PID {owner_pid})" if owner_pid else controller_state

    # Peer connectivity
    peer_available = bool(status_dict.get("peer_available", False))
    peer_address = status_dict.get("peer_address")
    peer_line = f"Connected ({peer_address})" if (peer_available and peer_address) else ("Connected" if peer_available else "None")

    # Network route
    local_bind = status_dict.get("local_bind_address")
    network_path = classify_network_path(local_bind, classifier=classifier)

    # Audio paths
    speaker_state = status_dict.get("speaker_path_state", "IDLE")
    microphone_state = status_dict.get("microphone_path_state", "IDLE")

    # Mode & Voice input
    mode = (status_dict.get("mode") or "PLAYBACK").upper()
    voice_input_active = bool(status_dict.get("voice_input_active", False))
    if mode == "DICTATION" or voice_input_active:
        voice_input = "Active"
    elif desired_state == "STOPPED_BY_USER":
        voice_input = "Standby / Off"
    else:
        voice_input = "Automatic / Standby"

    # Pack43 readiness: tri-state
    pack43_avail = status_dict.get("pack43_available")
    if pack43_avail is True:
        pack43_readiness = "Available"
    elif pack43_avail is False:
        pack43_readiness = "Unavailable"
    elif microphone_state == "UNAVAILABLE":
        pack43_readiness = "Unavailable"
    elif microphone_state in ("RUNNING", "READY"):
        pack43_readiness = "Available"
    else:
        pack43_readiness = "Unprobed"

    # Last actionable error
    actionable_error = status_dict.get("last_actionable_error")
    mic_error = status_dict.get("last_actionable_microphone_error")
    error_parts = []
    if actionable_error:
        error_parts.append(str(actionable_error))
    if mic_error and mic_error != actionable_error:
        error_parts.append(str(mic_error))
    last_error_line = "; ".join(error_parts) if error_parts else "None"

    return (
        "=== Cross-Desk Flow Diagnostic Report ===\n"
        f"Deployed SHA: {deployed_sha}\n"
        f"Platform: {platform_info}\n"
        f"Controller: {controller_line}\n"
        f"Desired State: {desired_state}\n"
        f"Peer State: {peer_line}\n"
        f"Network Path: {network_path}\n"
        f"Auto Start: {autostart}\n"
        f"Speaker Path: {speaker_state}\n"
        f"Microphone Path: {microphone_state}\n"
        f"Voice Input: {voice_input}\n"
        f"Pack43 Readiness: {pack43_readiness}\n"
        f"Last Actionable Error: {last_error_line}\n"
        "========================================="
    )


def get_diagnostics_view_data(status_dict: Optional[Dict[str, Any]]) -> Dict[str, tuple[str, str]]:
    """Extracts display tuples (text, foreground_color) for diagnostics UI fields."""
    if status_dict is None:
        return {
            "service": ("Not running", "#b91c1c"),
            "peer": ("None", "#64748b"),
            "net": ("Unknown", "#64748b"),
            "autostart": (get_autostart_status(), "#334155"),
            "pack43": ("Unprobed", "#64748b"),
            "spk": ("STOPPED", "#64748b"),
            "mic": ("STOPPED", "#64748b"),
            "voice": ("Standby / Off", "#64748b"),
            "err": ("Background service is not running", "#b91c1c"),
        }

    # Background service
    owner_pid = status_dict.get("owner_pid")
    ctrl_state = status_dict.get("controller_state", "RUNNING")
    if owner_pid:
        service = (f"Running (PID {owner_pid})", "#15803d")
    else:
        service = (ctrl_state, "#15803d" if ctrl_state == "RUNNING" else "#b91c1c")

    # Peer connectivity
    peer_avail = bool(status_dict.get("peer_available", False))
    peer_addr = status_dict.get("peer_address")
    if peer_avail and peer_addr:
        peer = (f"Connected ({peer_addr})", "#15803d")
    elif peer_avail:
        peer = ("Connected", "#15803d")
    else:
        peer = ("None", "#b45309")

    # Network path
    local_bind = status_dict.get("local_bind_address")
    net_path = classify_network_path(local_bind)
    net = (net_path, "#15803d" if net_path == "Ethernet" else "#334155")

    # Auto Start
    autostart_val = get_autostart_status()
    autostart = (autostart_val, "#15803d" if autostart_val == "Installed" else "#64748b")

    # Pack43 readiness
    pack43_avail = status_dict.get("pack43_available")
    mic_state = status_dict.get("microphone_path_state", "IDLE")
    if pack43_avail is True or mic_state in ("RUNNING", "READY"):
        pack43 = ("Available", "#15803d")
    elif pack43_avail is False or mic_state == "UNAVAILABLE":
        pack43 = ("Unavailable", "#b91c1c")
    else:
        pack43 = ("Unprobed", "#64748b")

    # Speaker & Mic paths
    spk_state = status_dict.get("speaker_path_state", "IDLE")
    spk = (spk_state, "#15803d" if spk_state == "RUNNING" else "#334155")
    mic = (mic_state, "#15803d" if mic_state == "RUNNING" else "#334155")

    # Voice input
    mode = (status_dict.get("mode") or "").upper()
    voice_active = bool(status_dict.get("voice_input_active", False))
    if mode == "DICTATION" or voice_active:
        voice = ("Active", "#7c3aed")
    elif status_dict.get("desired_state") == "STOPPED_BY_USER":
        voice = ("Standby / Off", "#64748b")
    else:
        voice = ("Automatic / Standby", "#0369a1")

    # Last error
    actionable_err = status_dict.get("last_actionable_error")
    mic_err = status_dict.get("last_actionable_microphone_error")
    errs = [str(e) for e in (actionable_err, mic_err) if e and str(e) != "None"]
    err = ("; ".join(errs), "#b91c1c") if errs else ("None", "#15803d")

    return {
        "service": service,
        "peer": peer,
        "net": net,
        "autostart": autostart,
        "pack43": pack43,
        "spk": spk,
        "mic": mic,
        "voice": voice,
        "err": err,
    }
