"""Pack43 (VB-Audio Virtual Cable) hardware and endpoint resolver on Windows.

Strict production resolver for Standard VB-CABLE Pack43:
- Enforces driver identity: Manufacturer "VB-Audio Software", DriverVersion "1.0.3.5", HardwareID "VBAudioVACWDM".
- Resolves Pack43 render endpoint: "CABLE Input (VB-Audio Virtual Cable)" for wasapisink render.
- Resolves Pack43 capture endpoint: "CABLE Output (VB-Audio Virtual Cable)" for verification/applications.
- Implements minimum caching contract:
  * First resolution when microphone requested.
  * Caches resolved endpoint ID.
  * Invalidates / re-enumerates on endpoint invalidation, pipeline failure, or stale evidence.
  * Pure status queries do NOT trigger expensive CIM/WMI enumeration.
  * Fail-closed: No fallback to generic microphone or default device.
"""

import logging
import os
import shutil
import subprocess
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

PACK43_HARDWARE_ID = "VBAudioVACWDM"
PACK43_EXPECTED_DRIVER_VERSION = "1.0.3.5"
PACK43_EXPECTED_MANUFACTURER = "VB-Audio Software"
PACK43_RENDER_NAME_SUBSTRING = "CABLE Input"
PACK43_CAPTURE_NAME_SUBSTRING = "CABLE Output"


class Pack43ResolutionResult(NamedTuple):
    render_endpoint_id: str
    capture_endpoint_id: Optional[str]
    driver_version: str


class Pack43Resolver:
    """Production resolver and identity verifier for VB-CABLE Pack43 on Windows."""

    def __init__(self, pwsh_path: Optional[str] = None):
        self._pwsh_path = pwsh_path
        self._cached_result: Optional[Pack43ResolutionResult] = None
        self._has_cached: bool = False

    def _get_powershell_executable(self) -> Optional[str]:
        if self._pwsh_path and os.path.exists(self._pwsh_path):
            return self._pwsh_path
        found = (
            shutil.which("pwsh.exe")
            or shutil.which("pwsh")
            or shutil.which(r"C:\Program Files\PowerShell\7\pwsh.exe")
            or shutil.which(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
            or shutil.which("powershell.exe")
        )
        return found

    def invalidate_cache(self) -> None:
        """Explicitly marks cached resolution as stale, forcing re-enumeration on next request."""
        self._has_cached = False
        self._cached_result = None

    @property
    def has_probed(self) -> bool:
        """Read-only check if Pack43 has been probed at least once since start or last invalidation."""
        return self._has_cached

    @property
    def is_cached_available(self) -> Optional[bool]:
        """Read-only tri-state check of Pack43 availability without triggering CIM/WMI:

        Returns:
          True  -> Probed and confirmed available.
          False -> Probed and confirmed unavailable / mismatched.
          None  -> Not probed yet (fresh / unprobed state).
        """
        if not self._has_cached:
            return None
        return self._cached_result is not None

    def resolve_pack43(self, force_refresh: bool = False) -> Optional[Pack43ResolutionResult]:
        """Resolves Pack43 render endpoint and validates driver identity.

        - Positive results remain cached.
        - Negative results remain cached and fail-closed; ordinary periodic reconcile()
          will NOT repeatedly re-enumerate WMI.
        - force_refresh=True or invalidate_cache() forces an immediate re-enumeration.
        - Returns Pack43ResolutionResult if found and valid; returns None (fail-closed) otherwise.
        """
        if not force_refresh and self._has_cached:
            return self._cached_result

        result = self._query_pack43()
        if result is not None:
            self._cached_result = result
            self._has_cached = True
            return result
        else:
            self._cached_result = None
            self._has_cached = True
            return None

    def resolve_for_explicit_demand(self) -> Optional[Pack43ResolutionResult]:
        """Resolves Pack43 specifically for an explicit user or dictation demand edge:

        - Positive cache -> reuse cached positive without WMI re-enumeration.
        - Fresh / unprobed -> ordinary probe.
        - Negative cache -> perform exactly one fresh resolution to recover from stale negative state,
          updating the cache with real result.
        """
        force_refresh = self.is_cached_available is False
        return self.resolve_pack43(force_refresh=force_refresh)

    def _query_pack43(self) -> Optional[Pack43ResolutionResult]:
        """Queries WMI/CIM via PowerShell for Pack43 driver and render endpoints."""
        pwsh = self._get_powershell_executable()
        if not pwsh:
            logger.warning("PowerShell executable not found for Pack43 enumeration")
            return None

        # Query driver and render endpoint via a single lightweight PowerShell script
        ps_script = (
            "$ErrorActionPreference = 'Stop'; "
            "$drv = Get-CimInstance Win32_PnPSignedDriver | "
            "Where-Object { $_.HardwareID -like '*" + PACK43_HARDWARE_ID + "*' } | "
            "Select-Object -First 1 DeviceName, Manufacturer, DriverVersion, HardwareID; "
            "if (-not $drv) { exit 1 }; "
            "$render = Get-CimInstance Win32_PnPEntity | "
            "Where-Object { $_.Name -like '*" + PACK43_RENDER_NAME_SUBSTRING + "*' } | "
            "Select-Object -First 1 Name, DeviceID; "
            "if (-not $render) { exit 2 }; "
            "$capture = Get-CimInstance Win32_PnPEntity | "
            "Where-Object { $_.Name -like '*" + PACK43_CAPTURE_NAME_SUBSTRING + "*' } | "
            "Select-Object -First 1 Name, DeviceID; "
            "$capId = if ($capture) { $capture.DeviceID } else { '' }; "
            "$drvVer = if ($drv.DriverVersion) { $drv.DriverVersion } else { '' }; "
            "$drvMan = if ($drv.Manufacturer) { $drv.Manufacturer } else { '' }; "
            "[Console]::Out.WriteLine($drvMan + ';;;' + $drvVer + ';;;' + $render.DeviceID + ';;;' + $capId)"
        )

        try:
            cmd = [pwsh, "-NoProfile", "-NonInteractive", "-Command", ps_script]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15.0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if res.returncode != 0:
                logger.debug("Pack43 enumeration failed with returncode %d: %s", res.returncode, res.stderr.strip())
                return None

            stdout = res.stdout.strip()
            parts = stdout.split(";;;")
            if len(parts) != 4:
                logger.warning("Unexpected Pack43 enumeration output: %s", stdout)
                return None

            manufacturer, driver_ver, render_dev_id, cap_dev_id = parts
            if PACK43_EXPECTED_MANUFACTURER.lower() not in manufacturer.lower():
                logger.warning("Pack43 manufacturer mismatch: got %s, expected %s", manufacturer, PACK43_EXPECTED_MANUFACTURER)
                return None

            if driver_ver.strip() != PACK43_EXPECTED_DRIVER_VERSION:
                logger.warning("Pack43 driver version mismatch: got %s, expected %s", driver_ver, PACK43_EXPECTED_DRIVER_VERSION)
                return None

            render_endpoint_id = render_dev_id
            if "{" in render_dev_id:
                render_endpoint_id = render_dev_id[render_dev_id.index("{"):]

            return Pack43ResolutionResult(
                render_endpoint_id=render_endpoint_id,
                capture_endpoint_id=cap_dev_id if cap_dev_id else None,
                driver_version=driver_ver.strip(),
            )

        except Exception as exc:
            logger.error("Exception during Pack43 enumeration: %s", exc)
            return None
