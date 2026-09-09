"""Network Interface Classifier seam for bridge_core discovery."""

from __future__ import annotations

import enum
import logging
import os
import platform
import shutil
import socket
import subprocess
from typing import Dict, Optional

logger = logging.getLogger("bridge_core.interface_classifier")


class InterfaceMedium(str, enum.Enum):
    """Network interface medium classification."""

    WIRED_ETHERNET = "WIRED_ETHERNET"
    WIFI = "WIFI"
    OTHER = "OTHER"


class InterfaceClassifier:
    """Seam for classifying local IP addresses by underlying network medium."""

    def __init__(self) -> None:
        self._positive_cache: Dict[str, InterfaceMedium] = {}

    def _resolve_powershell_cmd(self) -> Optional[str]:
        """Resolves PowerShell executable via SystemRoot, pwsh, or PATH."""
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        built_in = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if os.path.isfile(built_in):
            return built_in
        pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
        if pwsh:
            return pwsh
        ps = shutil.which("powershell.exe") or shutil.which("powershell")
        if ps:
            return ps
        return None

    def classify_interface(self, ip_str: str) -> InterfaceMedium:
        """Classifies the interface owning ip_str into InterfaceMedium.
        
        Uses Darwin (system_profiler / networksetup) or Windows (PowerShell Get-NetAdapter) OS metadata.
        Falls back safely to InterfaceMedium.OTHER if not explicitly confirmed as Ethernet or Wi-Fi.
        """
        if not ip_str or ip_str in ("0.0.0.0", "127.0.0.1"):
            return InterfaceMedium.OTHER

        cached = self._positive_cache.get(ip_str)
        if cached is not None:
            return cached

        classified = InterfaceMedium.OTHER
        try:
            sys_name = platform.system()
            if sys_name == "Windows":
                classified = self._classify_windows(ip_str)
            elif sys_name == "Darwin":
                import psutil
                target_iface = None
                for iface_name, addrs in psutil.net_if_addrs().items():
                    for addr in addrs:
                        if addr.family == socket.AF_INET and addr.address == ip_str:
                            target_iface = iface_name
                            break
                    if target_iface:
                        break

                if target_iface:
                    classified = self._classify_darwin(target_iface)
            else:
                classified = InterfaceMedium.OTHER
        except Exception as exc:
            logger.debug("Interface classification failed for %s: %s", ip_str, exc)
            classified = InterfaceMedium.OTHER

        if classified in (InterfaceMedium.WIRED_ETHERNET, InterfaceMedium.WIFI):
            self._positive_cache[ip_str] = classified

        return classified

    def _classify_darwin(self, iface_name: str) -> InterfaceMedium:
        """Classifies macOS interface using system_profiler SPNetworkDataType and networksetup."""
        try:
            # 1. Primary: system_profiler SPNetworkDataType exposes authoritative BSD Device Name -> Type
            cmd = ["system_profiler", "SPNetworkDataType"]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=3.0)
            current_type = ""
            current_dev = ""
            for line in out.splitlines():
                stripped = line.strip()
                if stripped.startswith("Type:"):
                    current_type = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("BSD Device Name:"):
                    current_dev = stripped.split(":", 1)[1].strip()
                    if current_dev == iface_name:
                        type_lower = current_type.lower()
                        if "ethernet" in type_lower:
                            return InterfaceMedium.WIRED_ETHERNET
                        if "airport" in type_lower or "wi-fi" in type_lower or "wireless" in type_lower:
                            return InterfaceMedium.WIFI
                        return InterfaceMedium.OTHER
        except Exception:
            pass

        try:
            # 2. Secondary fallback: networksetup -listallhardwareports
            cmd = ["networksetup", "-listallhardwareports"]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=2.0)
            current_port = ""
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("Hardware Port:"):
                    current_port = line.split(":", 1)[1].strip()
                elif line.startswith("Device:"):
                    dev = line.split(":", 1)[1].strip()
                    if dev == iface_name:
                        port_lower = current_port.lower()
                        if "wi-fi" in port_lower or "airport" in port_lower:
                            return InterfaceMedium.WIFI
                        if "ethernet" in port_lower or "lan" in port_lower or "thunderbolt bridge" in port_lower:
                            return InterfaceMedium.WIRED_ETHERNET
                        return InterfaceMedium.OTHER
        except Exception:
            pass

        return InterfaceMedium.OTHER

    def _classify_windows(self, ip_str: str) -> InterfaceMedium:
        """Classifies Windows interface using PowerShell Get-NetAdapter PhysicalMediaType by IP."""
        try:
            ps_exe = self._resolve_powershell_cmd()
            if not ps_exe:
                return InterfaceMedium.OTHER

            cmd = [
                ps_exe,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-NetAdapter -InterfaceIndex (Get-NetIPAddress -IPAddress {ip_str} -ErrorAction SilentlyContinue).InterfaceIndex -ErrorAction SilentlyContinue).PhysicalMediaType",
            ]
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            out = subprocess.check_output(
                cmd,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=15.0,
                creationflags=creationflags,
            ).strip()
            out_lower = out.lower()
            if "802.3" in out_lower or "ethernet" in out_lower:
                return InterfaceMedium.WIRED_ETHERNET
            if "native 802.11" in out_lower or "wireless" in out_lower or "wi-fi" in out_lower:
                return InterfaceMedium.WIFI
        except Exception:
            pass
        return InterfaceMedium.OTHER
