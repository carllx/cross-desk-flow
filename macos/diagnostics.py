"""Diagnostics and Recovery helper for macOS Cross-Desk Flow.

Provides:
- Network route classification (Ethernet / Wi-Fi / Fallback / Unknown)
- LaunchAgent autostart query (Installed / Not Installed / Disabled / Unknown)
- Diagnostic report generation and sanitization
- Safe Finder opening for log or data directory
- Dead-controller recovery via LaunchAgent lifecycle authority
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
import time
from typing import Any, Dict, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from bridge_core.interface_classifier import InterfaceClassifier, InterfaceMedium
from macos.lifecycle import (
    DEFAULT_PLIST_PATH,
    LAUNCH_AGENT_LABEL,
    LOG_DIR,
    get_current_uid,
    is_service_loaded,
    run_launchctl,
)

_PATH_REGEX = re.compile(r"/(?:Users|Volumes|private|var|tmp)/[^\s,;'\"]+")
_GUID_REGEX = re.compile(r"\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}?")
_CREDENTIAL_REGEX = re.compile(r"(?i)\b(password|token|secret|key|credential)\s*[:=]\s*[^\s,;'\"]+")


def sanitize_diagnostic_text(text: Optional[str]) -> str:
    """Sanitizes diagnostic text by redacting absolute paths, GUIDs, and credential key-values.

    Preserves meaningful diagnostic errors while preventing accidental leakage of
    local username paths, hardware GUIDs, or secret tokens.
    """
    if not text:
        return ""
    sanitized = _CREDENTIAL_REGEX.sub(r"\1=<redacted>", text)
    sanitized = _PATH_REGEX.sub("<path redacted>", sanitized)
    sanitized = _GUID_REGEX.sub("<guid redacted>", sanitized)
    return sanitized


_shared_classifier: Optional[InterfaceClassifier] = None
_cached_network_path: Dict[str, Tuple[str, float]] = {}
_cached_autostart: Tuple[str, float] = ("", 0.0)


def get_log_directory() -> str:
    """Returns the canonical macOS log directory for desk-audio-bridge."""
    return LOG_DIR


def get_data_directory() -> str:
    """Returns the canonical macOS application support data directory."""
    return os.path.expanduser("~/Library/Application Support/desk-audio-bridge")


def has_log_files() -> bool:
    """Checks whether any .log files exist in the log directory."""
    log_dir = get_log_directory()
    if not os.path.isdir(log_dir):
        return False
    try:
        return any(f.endswith(".log") for f in os.listdir(log_dir))
    except Exception:
        return False


def open_data_directory() -> bool:
    """Opens log directory (if logs exist) or data directory safely in macOS Finder."""
    target_dir = get_log_directory() if has_log_files() else get_data_directory()
    os.makedirs(target_dir, exist_ok=True)
    try:
        subprocess.Popen(["open", target_dir])
        return True
    except Exception:
        return False


def classify_network_path(
    local_bind: Optional[str],
    classifier: Optional[InterfaceClassifier] = None,
    force_probe: bool = False,
) -> str:
    """Classifies network route for local binding address with TTL caching.

    Returns one of: 'Ethernet', 'Wi-Fi', 'Fallback (other)', or 'Unknown'.
    """
    if not local_bind or local_bind in ("0.0.0.0", "127.0.0.1"):
        return "Unknown"

    now = time.time()
    if not force_probe and local_bind in _cached_network_path:
        val, ts = _cached_network_path[local_bind]
        if now - ts < 30.0:
            return val

    global _shared_classifier
    if classifier is not None:
        cls = classifier
    else:
        if _shared_classifier is None:
            _shared_classifier = InterfaceClassifier()
        cls = _shared_classifier

    result = "Unknown"
    try:
        medium = cls.classify_interface(local_bind)
        if medium == InterfaceMedium.WIRED_ETHERNET:
            result = "Ethernet"
        elif medium == InterfaceMedium.WIFI:
            result = "Wi-Fi"
        elif medium == InterfaceMedium.OTHER:
            result = "Fallback (other)"
    except Exception:
        result = "Unknown"

    _cached_network_path[local_bind] = (result, now)
    return result


def _query_launchagent_status(
    plist_path: Optional[str] = None,
    label: str = LAUNCH_AGENT_LABEL,
) -> str:
    """Queries LaunchAgent status: 'Installed', 'Disabled', 'Not installed', or 'Unknown'."""
    target_plist = plist_path or DEFAULT_PLIST_PATH

    # Check print-disabled first
    try:
        uid = get_current_uid()
        res_disabled = run_launchctl(["launchctl", "print-disabled", f"gui/{uid}"])
        if res_disabled.returncode == 0:
            for line in res_disabled.stdout.splitlines():
                if f'"{label}" => true' in line:
                    return "Disabled"
    except Exception:
        pass

    # Check if loaded via launchctl print gui/<uid>/<label>
    try:
        if is_service_loaded(label):
            return "Installed"
    except Exception:
        pass

    # If not loaded in launchd, check if plist exists
    if os.path.isfile(target_plist):
        return "Installed"

    return "Not installed"


def get_autostart_status(
    force_probe: bool = False,
    plist_path: Optional[str] = None,
    label: str = LAUNCH_AGENT_LABEL,
) -> str:
    """Queries LaunchAgent autostart status with TTL caching."""
    global _cached_autostart
    now = time.time()
    if not force_probe and _cached_autostart[0] and (now - _cached_autostart[1] < 30.0):
        return _cached_autostart[0]

    status = _query_launchagent_status(plist_path=plist_path, label=label)
    _cached_autostart = (status, now)
    return status


def start_controller_via_lifecycle(
    timeout_sec: float = 5.0,
    plist_path: Optional[str] = None,
    label: str = LAUNCH_AGENT_LABEL,
) -> bool:
    """Recovers an absent controller using existing LaunchAgent lifecycle authority.

    Strict rules:
    - If LaunchAgent plist or service is absent/disabled, DO NOT install or register.
    - Never directly spawns macos.cli run from the UI process.
    - Never creates a second lifecycle or broad-kills existing processes.
    - Uses launchctl kickstart or launchctl bootstrap to wake the existing LaunchAgent.
    - Waits boundedly for the controller to become responsive on IPC.
    """
    from macos.cli import send_ipc_command

    target_plist = plist_path or DEFAULT_PLIST_PATH
    autostart = _query_launchagent_status(plist_path=target_plist, label=label)
    if autostart not in ("Installed",):
        return False

    uid = get_current_uid()

    # If service is loaded in launchd, kickstart it
    if is_service_loaded(label):
        res = run_launchctl(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"])
        if res.returncode != 0:
            run_launchctl(["launchctl", "kickstart", f"gui/{uid}/{label}"])
    else:
        # Service plist exists but not currently bootstrapped: bootstrap it
        if not os.path.isfile(target_plist):
            return False
        res = run_launchctl(["launchctl", "bootstrap", f"gui/{uid}", target_plist])
        if res.returncode != 0:
            run_launchctl(["launchctl", "load", "-w", target_plist])

    start = time.time()
    while time.time() - start < timeout_sec:
        status = send_ipc_command("status")
        if status is not None and status.get("owner_pid") is not None:
            return True
        time.sleep(0.2)
    return False


def _read_git_metadata_sha(target_dir: str) -> Optional[str]:
    """Reads deployed commit SHA directly from Git repository metadata (.git dir or worktree)."""
    try:
        git_path = os.path.join(target_dir, ".git")
        if not os.path.exists(git_path):
            return None

        git_dir: Optional[str] = None
        git_common_dir: Optional[str] = None

        if os.path.isfile(git_path):
            with open(git_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content.startswith("gitdir:"):
                git_dir = content.split(":", 1)[1].strip()
                if not os.path.isabs(git_dir):
                    git_dir = os.path.normpath(os.path.join(target_dir, git_dir))
                commondir_file = os.path.join(git_dir, "commondir")
                if os.path.isfile(commondir_file):
                    with open(commondir_file, "r", encoding="utf-8") as cf:
                        cd_rel = cf.read().strip()
                    git_common_dir = os.path.normpath(os.path.join(git_dir, cd_rel))
                else:
                    git_common_dir = git_dir
            else:
                return None
        elif os.path.isdir(git_path):
            git_dir = git_path
            git_common_dir = git_path
        else:
            return None

        if not git_dir:
            return None

        head_file = os.path.join(git_dir, "HEAD")
        if not os.path.isfile(head_file):
            return None

        with open(head_file, "r", encoding="utf-8") as f:
            head_content = f.read().strip()

        hex_pattern = re.compile(r"^[0-9a-fA-F]{40}$")
        if hex_pattern.match(head_content):
            return head_content.lower()

        if head_content.startswith("ref:"):
            ref_part = head_content.split(":", 1)[1].strip()
            for base_dir in (git_dir, git_common_dir):
                if not base_dir:
                    continue
                ref_file = os.path.join(base_dir, ref_part.replace("/", os.sep))
                if os.path.isfile(ref_file):
                    with open(ref_file, "r", encoding="utf-8") as rf:
                        ref_content = rf.read().strip()
                    if hex_pattern.match(ref_content):
                        return ref_content.lower()

            if git_common_dir:
                packed_refs_file = os.path.join(git_common_dir, "packed-refs")
                if os.path.isfile(packed_refs_file):
                    with open(packed_refs_file, "r", encoding="utf-8") as pf:
                        for line in pf:
                            line = line.strip()
                            if line.startswith("#") or not line:
                                continue
                            parts = line.split(" ", 1)
                            if len(parts) == 2 and parts[1] == ref_part:
                                return parts[0].lower()
        return None
    except Exception:
        return None


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

    meta_sha = _read_git_metadata_sha(target_dir)
    if meta_sha:
        return meta_sha

    return "unknown"


def build_diagnostic_report(
    status_dict: Optional[Dict[str, Any]],
    repo_root: Optional[str] = None,
    classifier: Optional[InterfaceClassifier] = None,
) -> str:
    """Builds a human- and machine-readable plain text diagnostic report.

    Sanitizes credentials, tokens, sensitive paths, and raw hardware GUIDs.
    """
    deployed_sha = get_deployed_sha(repo_root)
    platform_info = f"macOS {platform.mac_ver()[0]} ({platform.machine()})"
    autostart = get_autostart_status()

    if status_dict is None:
        return (
            "=== Cross-Desk Flow Diagnostic Report (macOS) ===\n"
            f"Deployed SHA: {deployed_sha}\n"
            f"Platform: {platform_info}\n"
            "Controller: NOT RUNNING\n"
            "Desired State: UNKNOWN\n"
            "Peer State: NONE\n"
            "Network Path: Unknown\n"
            f"Auto Start / LaunchAgent: {autostart}\n"
            "Speaker Path: STOPPED\n"
            "Microphone Path: STOPPED\n"
            "Voice Input / Dictation: Standby / Off\n"
            "Last Actionable Error: Background service is not running\n"
            "================================================="
        )

    controller_state = status_dict.get("controller_state", "UNKNOWN")
    desired_state = status_dict.get("desired_state", "UNKNOWN")
    owner_pid = status_dict.get("owner_pid")
    controller_line = f"RUNNING (PID {owner_pid})" if owner_pid else controller_state

    peer_available = bool(status_dict.get("peer_available", False))
    peer_address = status_dict.get("peer_address")
    peer_line = f"Connected ({peer_address})" if (peer_available and peer_address) else ("Connected" if peer_available else "None")

    local_bind = status_dict.get("local_bind_address")
    network_path = classify_network_path(local_bind, classifier=classifier)

    speaker_state = status_dict.get("speaker_path_state", "IDLE")
    microphone_state = status_dict.get("microphone_path_state", "IDLE")

    if microphone_state == "RUNNING":
        voice_input = "Active (Dictation)"
    elif desired_state == "STOPPED_BY_USER":
        voice_input = "Standby / Off"
    else:
        voice_input = "Automatic / Standby"

    actionable_error = status_dict.get("last_actionable_error")
    mic_error = status_dict.get("last_actionable_microphone_error")
    error_parts = []
    if actionable_error:
        error_parts.append(sanitize_diagnostic_text(str(actionable_error)))
    if mic_error and mic_error != actionable_error:
        error_parts.append(sanitize_diagnostic_text(str(mic_error)))
    last_error_line = "; ".join(error_parts) if error_parts else "None"

    return (
        "=== Cross-Desk Flow Diagnostic Report (macOS) ===\n"
        f"Deployed SHA: {deployed_sha}\n"
        f"Platform: {platform_info}\n"
        f"Controller: {controller_line}\n"
        f"Desired State: {desired_state}\n"
        f"Peer State: {peer_line}\n"
        f"Network Path: {network_path}\n"
        f"Auto Start / LaunchAgent: {autostart}\n"
        f"Speaker Path: {speaker_state}\n"
        f"Microphone Path: {microphone_state}\n"
        f"Voice Input / Dictation: {voice_input}\n"
        f"Last Actionable Error: {last_error_line}\n"
        "================================================="
    )


def get_diagnostics_view_data(status_dict: Optional[Dict[str, Any]]) -> Dict[str, Tuple[str, str]]:
    """Extracts display tuples (text, foreground_color) for diagnostics UI fields.

    Maintains clean user-facing presentation without raw PIDs or raw IPs in the UI grid.
    """
    if status_dict is None:
        return {
            "service": ("Not running", "#b91c1c"),
            "peer": ("None", "#64748b"),
            "net": ("Unknown", "#64748b"),
            "autostart": (get_autostart_status(), "#334155"),
            "spk": ("STOPPED", "#64748b"),
            "mic": ("STOPPED", "#64748b"),
            "voice": ("Standby / Off", "#64748b"),
            "err": ("Background service is not running", "#b91c1c"),
        }

    ctrl_state = status_dict.get("controller_state", "RUNNING")
    if ctrl_state in ("RUNNING", "ACTIVE") or status_dict.get("owner_pid"):
        service = ("Running", "#15803d")
    elif ctrl_state == "STOPPED":
        service = ("Stopped", "#475569")
    else:
        service = (ctrl_state, "#b91c1c")

    peer_avail = bool(status_dict.get("peer_available", False))
    peer = ("Connected", "#15803d") if peer_avail else ("None", "#b45309")

    local_bind = status_dict.get("local_bind_address")
    net_path = classify_network_path(local_bind)
    net = (net_path, "#15803d" if net_path == "Ethernet" else "#334155")

    autostart_val = get_autostart_status()
    autostart = (autostart_val, "#15803d" if autostart_val == "Installed" else "#64748b")

    spk_state = status_dict.get("speaker_path_state", "IDLE")
    mic_state = status_dict.get("microphone_path_state", "IDLE")
    spk = (spk_state, "#15803d" if spk_state == "RUNNING" else "#334155")
    mic = (mic_state, "#15803d" if mic_state == "RUNNING" else "#334155")

    if mic_state == "RUNNING":
        voice = ("Active (Dictation)", "#7c3aed")
    elif status_dict.get("desired_state") == "STOPPED_BY_USER":
        voice = ("Standby / Off", "#64748b")
    else:
        voice = ("Automatic / Standby", "#0369a1")

    actionable_err = status_dict.get("last_actionable_error")
    mic_err = status_dict.get("last_actionable_microphone_error")
    errs = [str(e) for e in (actionable_err, mic_err) if e and str(e) != "None"]
    err = ("; ".join(errs), "#b91c1c") if errs else ("None", "#15803d")

    return {
        "service": service,
        "peer": peer,
        "net": net,
        "autostart": autostart,
        "spk": spk,
        "mic": mic,
        "voice": voice,
        "err": err,
    }
