"""Peer discovery mechanism for desk-audio-bridge.

Implements multi-interface broadcast/handshake, source/bind resolution,
and multiple-responder ambiguity detection:
- Discovers candidate active IPv4 interfaces:
  - Must be up/active.
  - Must be IPv4 and non-loopback.
  - Must be on-link directly connected (has valid subnet mask / broadcast, not point-to-point /32).
  - Excludes benchmark/tunnel subnets (198.18.0.0/15) while accepting RFC 1918, link-local (169.254.0.0/16),
    and on-link LAN/Ethernet subnets.
- Preflights dependencies; reports explicit error on enumeration failure instead of silent global broadcast.
- Resolves route/bind deterministically via RouteResolver seam.
- Detects multiple distinct opposite-role responders in discovery window and enters AMBIGUOUS_PEER.
- Automatically recovers when secondary responders expire.
"""

import enum
import ipaddress
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from bridge_core.contract import (
    CONTROL_PROTOCOL_VERSION,
    DEFAULT_CONTROL_PORT,
    DEFAULT_SPEAKER_RTP_PORT,
    HandshakeAck,
    HandshakeHello,
    HostRole,
)

logger = logging.getLogger(__name__)


DEFAULT_HEARTBEAT_INTERVAL = 4.0


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



def is_eligible_onlink_ipv4(ip_str: str, netmask_str: Optional[str] = None) -> bool:
    """Verifies that an IPv4 address is an eligible local/on-link LAN interface.
    
    Principles:
    - Non-loopback.
    - Not unreserved global Internet routable without subnet or tunneled.
    - Excludes RFC 2544 benchmark / tunnel range (198.18.0.0/15) often used by virtual proxy adapters.
    - Accepts RFC 1918 private (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16).
    - Accepts RFC 3927 link-local (169.254.0.0/16).
    - Accepts directly-connected on-link LAN subnets (has netmask <= /24 and >= /30, and not /32 or /31 point-to-point).
    """
    try:
        ip = ipaddress.IPv4Address(ip_str)
        if ip.is_loopback or ip.is_unspecified:
            return False

        # Exclude RFC 2544 benchmark range (used by Meta Tunnel / Mihomo / etc.)
        if ip in ipaddress.IPv4Network("198.18.0.0/15"):
            return False

        # Exclude point-to-point host tunnels (/32, /31, or /30)
        if netmask_str:
            net = ipaddress.IPv4Network((ip_str, netmask_str), strict=False)
            if net.prefixlen >= 30:
                return False

        # Accepts RFC 1918 private or link-local
        if ip.is_private or ip.is_link_local:
            return True

        # For non-RFC1918 addresses configured on direct Ethernet/LAN:
        # If it has a standard local LAN subnet (/24, /23, /16 etc.), it qualifies as directly connected on-link
        if netmask_str:
            net = ipaddress.IPv4Network((ip_str, netmask_str), strict=False)
            if 8 <= net.prefixlen <= 28:
                return True

        return False
    except Exception:
        return False


class InterfaceEnumerator:
    """Enumerates candidate IPv4 local interfaces and their subnet broadcast addresses."""

    def get_candidate_interfaces(self) -> Tuple[bool, List[Tuple[str, str]], Optional[str]]:
        """Returns (success, candidates_list, error_message).
        
        candidates_list contains (local_ip, broadcast_ip) tuples.
        """
        try:
            import psutil
        except ImportError:
            return False, [], "Runtime dependency 'psutil' is missing; cannot enumerate network interfaces"

        candidates = []
        try:
            stats = psutil.net_if_stats()
            for iface_name, addrs in psutil.net_if_addrs().items():
                stat = stats.get(iface_name)
                # Must be up / active
                if stat and not stat.isup:
                    continue

                for addr in addrs:
                    if addr.family == socket.AF_INET:
                        ip = addr.address
                        netmask = addr.netmask
                        if not is_eligible_onlink_ipv4(ip, netmask):
                            continue

                        broadcast = None
                        if addr.broadcast:
                            broadcast = addr.broadcast
                        elif netmask:
                            try:
                                net = ipaddress.IPv4Network((ip, netmask), strict=False)
                                broadcast = str(net.broadcast_address)
                            except Exception:
                                pass
                        if not broadcast:
                            broadcast = "255.255.255.255"
                        candidates.append((ip, broadcast))
            return True, candidates, None
        except Exception as exc:
            return False, [], f"Interface enumeration failed: {exc}"


class RouteResolver:
    """Seam for determining the local binding IP used to route to a given destination IP."""

    def resolve_local_route(self, target_ip: str, port: int) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((target_ip, port))
            local_ip = s.getsockname()[0]
            s.close()
            return local_ip
        except Exception:
            return "0.0.0.0"


class PeerDiscoveryService:
    """Manages control plane discovery, route resolution, and ambiguity detection."""

    def __init__(
        self,
        local_role: HostRole = HostRole.WINDOWS,
        instance_id: str = "inst",
        control_port: int = DEFAULT_CONTROL_PORT,
        speaker_port: int = DEFAULT_SPEAKER_RTP_PORT,
        interface_enumerator: Optional[InterfaceEnumerator] = None,
        route_resolver: Optional[RouteResolver] = None,
        interface_classifier: Optional[InterfaceClassifier] = None,
        on_peer_discovered: Optional[Callable[[str, str, int, str], None]] = None,
        on_peer_lost: Optional[Callable[[], None]] = None,
        on_control_message: Optional[Callable[[dict, str], None]] = None,
        election_delay: float = 0.25,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    ):
        self.local_role = local_role
        self.target_role = (
            HostRole.MACOS if local_role == HostRole.WINDOWS else HostRole.WINDOWS
        )
        self.instance_id = instance_id
        self.control_port = control_port
        self.speaker_port = speaker_port
        self.election_delay = election_delay
        self.heartbeat_interval = heartbeat_interval
        self.enumerator = interface_enumerator or InterfaceEnumerator()
        self.route_resolver = route_resolver or RouteResolver()
        self.classifier = interface_classifier or InterfaceClassifier()
        self.on_peer_discovered = on_peer_discovered
        self.on_peer_lost = on_peer_lost
        self.on_control_message = on_control_message


        self._running = False
        self._listener_sock: Optional[socket.socket] = None
        self._recv_thread: Optional[threading.Thread] = None

        self._peer_address: Optional[str] = None
        self._local_bind_address: Optional[str] = None
        self._peer_speaker_port: int = speaker_port
        self._peer_instance_id: Optional[str] = None
        self._last_peer_seen: float = 0.0
        self._last_heartbeat_sent: float = 0.0
        self._last_enumeration_error: Optional[str] = None

        # Ambiguity tracking: map of peer_instance_id -> (peer_ip, last_seen)
        self._known_responders: dict[str, Tuple[str, float]] = {}
        # Multi-interface route candidates: peer_instance_id -> {peer_ip: (local_source_ip, last_seen, medium)}
        self._peer_candidates: dict[str, dict[str, Tuple[str, float, InterfaceMedium]]] = {}
        self._is_ambiguous = False
        self._settle_timer: Optional[threading.Timer] = None
        self._is_settling: bool = False
        self._lock = threading.RLock()

    @property
    def peer_available(self) -> bool:
        with self._lock:
            if self._is_ambiguous or not self._peer_address or self._is_settling:
                return False
            return (time.time() - self._last_peer_seen) < 15.0

    @property
    def is_ambiguous(self) -> bool:
        with self._lock:
            return self._is_ambiguous

    @property
    def last_enumeration_error(self) -> Optional[str]:
        with self._lock:
            return self._last_enumeration_error

    @property
    def peer_address(self) -> Optional[str]:
        with self._lock:
            if self._is_ambiguous:
                return None
            return self._peer_address

    @property
    def local_bind_address(self) -> Optional[str]:
        with self._lock:
            if self._is_ambiguous:
                return None
            return self._local_bind_address

    @property
    def peer_speaker_port(self) -> int:
        with self._lock:
            return self._peer_speaker_port

    def refresh_peer_state(self, now: Optional[float] = None) -> None:
        """Explicitly advances discovery state, prunes expired responders, and resolves ambiguity recovery.
        
        Must ONLY be called from mutating loops or reconciliation routines, NEVER from Status/getters.
        """
        with self._lock:
            current_time = now if now is not None else time.time()
            # 1. Prune responders expired > 15s
            self._known_responders = {
                k: v for k, v in self._known_responders.items() if (current_time - v[1]) < 15.0
            }
            # Prune peer route candidates expired > 15s
            for inst in list(self._peer_candidates.keys()):
                self._peer_candidates[inst] = {
                    pip: data
                    for pip, data in self._peer_candidates[inst].items()
                    if (current_time - data[1]) < 15.0
                }
                if not self._peer_candidates[inst]:
                    del self._peer_candidates[inst]

            # 2. Ambiguity recovery or transition
            if self._is_ambiguous:
                if len(self._known_responders) <= 1:
                    self._is_ambiguous = False
                    if len(self._known_responders) == 1:
                        sole_inst, (sole_ip, sole_time) = next(iter(self._known_responders.items()))
                        self._peer_instance_id = sole_inst
                        best_route = self._elect_best_route(sole_inst)
                        target_ip = best_route[0] if best_route else sole_ip
                        self._peer_address = target_ip
                        self._local_bind_address = self.route_resolver.resolve_local_route(target_ip, self.control_port)
                        self._last_peer_seen = best_route[2] if best_route else sole_time
                    else:
                        self._peer_instance_id = None
                        self._peer_address = None
                        self._local_bind_address = None
                        self._last_peer_seen = 0.0
            else:
                # If currently paired with a peer, verify if it expired
                if len(self._known_responders) == 0:
                    self._peer_instance_id = None
                    self._peer_address = None
                    self._local_bind_address = None
                    self._last_peer_seen = 0.0

            # 3. Steady-state control heartbeat: broadcast HELLO if due
            if self._running and (current_time - self._last_heartbeat_sent) >= self.heartbeat_interval:
                self.broadcast_hello()
                self._last_heartbeat_sent = current_time

    def _elect_best_route(self, peer_inst: str) -> Optional[Tuple[str, str, float]]:
        """Edicts the best route for peer_inst based on medium priority:
        WIRED_ETHERNET > WIFI > OTHER.
        
        Returns (peer_ip, local_bind_ip, last_seen) or None.
        """
        candidates = self._peer_candidates.get(peer_inst, {})
        if not candidates:
            return None

        def medium_priority(medium: InterfaceMedium) -> int:
            if medium == InterfaceMedium.WIRED_ETHERNET:
                return 0
            if medium == InterfaceMedium.WIFI:
                return 1
            return 2

        # Sort by medium priority ascending, then by most recently seen descending
        sorted_candidates = sorted(
            candidates.items(),
            key=lambda item: (medium_priority(item[1][2]), -item[1][1]),
        )
        best_ip, (local_ip, last_seen, _medium) = sorted_candidates[0]
        return best_ip, local_ip, last_seen

    def start(self) -> None:
        with self._lock:
            if self._running:
                return

            self._listener_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._listener_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._listener_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                self._listener_sock.bind(("", self.control_port))
            except Exception as exc:
                logger.error("Failed to bind discovery socket on port %d: %s", self.control_port, exc)
                self._listener_sock.close()
                self._listener_sock = None
                raise

            self._running = True
            self._last_heartbeat_sent = 0.0
            self._recv_thread = threading.Thread(
                target=self._listen_loop, daemon=True, name="PeerDiscoveryRecv"
            )
            self._recv_thread.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            if self._settle_timer:
                try:
                    self._settle_timer.cancel()
                except Exception:
                    pass
                self._settle_timer = None
            self._is_settling = False

            if self._listener_sock:
                try:
                    self._listener_sock.close()
                except Exception:
                    pass
                self._listener_sock = None
            if self._recv_thread and self._recv_thread.is_alive():
                self._recv_thread.join(timeout=1.0)
            self._recv_thread = None

    def broadcast_hello(self) -> None:
        """Broadcasts HandshakeHello across candidate interfaces.
        
        If enumeration fails, records an error state and refuses silent global broadcast.
        """
        if not self._running:
            return

        success, candidates, err = self.enumerator.get_candidate_interfaces()
        with self._lock:
            if not success or not candidates:
                self._last_enumeration_error = err or "No eligible on-link IPv4 interfaces found"
                return
            self._last_enumeration_error = None

        for local_ip, bcast_ip in candidates:
            hello = HandshakeHello(
                version=CONTROL_PROTOCOL_VERSION,
                role=self.local_role.value,
                instance_id=self.instance_id,
                speaker_port=self.speaker_port,
                source_ip=local_ip,
            )
            data = json.dumps(hello.to_dict()).encode("utf-8")

            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                try:
                    sock.bind((local_ip, 0))
                except Exception:
                    pass
                sock.sendto(data, (bcast_ip, self.control_port))
                if bcast_ip != "255.255.255.255":
                    sock.sendto(data, ("255.255.255.255", self.control_port))
            except Exception as exc:
                logger.debug("Failed broadcast on %s -> %s: %s", local_ip, bcast_ip, exc)
            finally:
                if sock:
                    sock.close()

    def send_control_message(self, peer_ip: str, message: dict) -> bool:
        """Sends a directed DICTATION control packet to elected peer on port 50100."""
        with self._lock:
            if not self._running:
                return False
            if peer_ip != self._peer_address:
                logger.warning("Cannot send control message: %s does not match elected peer %s", peer_ip, self._peer_address)
                return False
            sock = self._listener_sock
            if not sock:
                return False

        data = json.dumps(message).encode("utf-8")
        try:
            sock.sendto(data, (peer_ip, self.control_port))
            return True
        except Exception as exc:
            logger.warning("Failed to send control message to %s: %s", peer_ip, exc)
            return False

    def handle_peer_message(self, msg: Any, peer_ip: str) -> None:
        """Processes an incoming peer packet (used by both _listen_loop and automated tests)."""
        if isinstance(msg, (bytes, bytearray)):
            try:
                msg = json.loads(msg.decode("utf-8"))
            except Exception:
                return
        if not isinstance(msg, dict):
            return

        msg_role = msg.get("role")
        version = msg.get("version")
        if version != CONTROL_PROTOCOL_VERSION or msg_role != self.target_role.value:
            return

        msg_type = msg.get("type", "")
        if isinstance(msg_type, str) and msg_type.startswith("DICTATION_"):
            # 50100 control/discovery separation:
            # DICTATION control packets are branched BEFORE discovery logic.
            # They MUST NOT update _known_responders, _peer_candidates, route election,
            # discovery liveness, or trigger discovery ACKs.
            with self._lock:
                if self._is_ambiguous or not self._peer_address or not self._peer_instance_id:
                    logger.debug("Dropping DICTATION message: peer ambiguous or not yet elected")
                    return
                # Peer validation: verify source IP corresponds to current elected peer address
                if peer_ip != self._peer_address:
                    logger.warning("Dropping DICTATION message from non-elected IP %s (elected %s)", peer_ip, self._peer_address)
                    return
                # Peer validation: verify sender instance_id matches selected peer_instance_id
                sender_inst = msg.get("instance_id")
                if sender_inst != self._peer_instance_id:
                    logger.warning("Dropping DICTATION message from unverified instance %s (elected %s)", sender_inst, self._peer_instance_id)
                    return

            if self.on_control_message:
                try:
                    self.on_control_message(msg, peer_ip)
                except Exception as exc:
                    logger.warning("Error in on_control_message callback: %s", exc)
            return


        peer_inst = msg.get("instance_id", "")
        peer_spk_port = msg.get("speaker_port", DEFAULT_SPEAKER_RTP_PORT)

        now = time.time()
        # Resolve local route to peer
        local_source_ip = self.route_resolver.resolve_local_route(peer_ip, self.control_port)
        try:
            route_medium = self.classifier.classify_interface(local_source_ip)
        except Exception:
            route_medium = InterfaceMedium.OTHER

        with self._lock:
            # Prune responders expired > 15s
            self._known_responders = {
                k: v for k, v in self._known_responders.items() if (now - v[1]) < 15.0
            }
            self._known_responders[peer_inst] = (peer_ip, now)

            # Record candidate route under peer_inst
            if peer_inst not in self._peer_candidates:
                self._peer_candidates[peer_inst] = {}
            self._peer_candidates[peer_inst][peer_ip] = (local_source_ip, now, route_medium)

            # Prune peer_candidates
            for inst in list(self._peer_candidates.keys()):
                self._peer_candidates[inst] = {
                    pip: data
                    for pip, data in self._peer_candidates[inst].items()
                    if (now - data[1]) < 15.0
                }
                if not self._peer_candidates[inst]:
                    del self._peer_candidates[inst]

            if len(self._known_responders) > 1:
                logger.warning(
                    "Multiple opposite-role responders discovered (%s); entering ambiguous state",
                    list(self._known_responders.keys()),
                )
                self._is_ambiguous = True
                self._peer_address = None
                self._local_bind_address = None
                if self._settle_timer:
                    try:
                        self._settle_timer.cancel()
                    except Exception:
                        pass
                    self._settle_timer = None
                self._is_settling = False
                return

            self._is_ambiguous = False

        # Reply with ACK if this was a HELLO and listener socket is open
        if "peer_instance_id" not in msg and self._listener_sock:
            ack = HandshakeAck(
                version=CONTROL_PROTOCOL_VERSION,
                role=self.local_role.value,
                instance_id=self.instance_id,
                speaker_port=self.speaker_port,
                peer_instance_id=peer_inst,
                source_ip=local_source_ip,
            )
            ack_bytes = json.dumps(ack.to_dict()).encode("utf-8")
            try:
                self._listener_sock.sendto(ack_bytes, (peer_ip, self.control_port))
            except Exception as exc:
                logger.debug("Failed sending ACK to %s: %s", peer_ip, exc)

        with self._lock:
            was_avail = (
                (now - self._last_peer_seen) < 15.0
                and self._peer_address is not None
                and not self._is_ambiguous
                and not self._is_settling
            )

            # Route election:
            # 1. If currently paired on an active route, check if an upgrade to WIRED_ETHERNET is available
            #    (e.g., initial Wi-Fi packet arrived first, and now Ethernet arrives for the first time).
            # 2. If already on WIRED_ETHERNET, never drift when a Wi-Fi packet arrives.
            # 3. If not yet active or route has expired, elect the highest priority candidate (wired > wifi > other).
            current_medium = InterfaceMedium.OTHER
            if self._local_bind_address:
                try:
                    current_medium = self.classifier.classify_interface(self._local_bind_address)
                except Exception:
                    current_medium = InterfaceMedium.OTHER

            notify_peer_discovered = False
            notify_args: Optional[Tuple[str, str, int, str]] = None

            if was_avail and self._peer_instance_id == peer_inst:
                # Stable route preservation:
                # If current route is already WIRED_ETHERNET (or equal priority), do NOT flap/drift
                if current_medium == InterfaceMedium.WIRED_ETHERNET or route_medium != InterfaceMedium.WIRED_ETHERNET:
                    # Update liveness timestamp, but preserve selected route
                    self._last_peer_seen = now
                    self._peer_speaker_port = peer_spk_port
                else:
                    # Upgrade path: current route was WIFI/OTHER, but a WIRED_ETHERNET packet just arrived
                    self._peer_address = peer_ip
                    self._local_bind_address = local_source_ip
                    self._peer_speaker_port = peer_spk_port
                    self._last_peer_seen = now
                    if self._settle_timer:
                        try:
                            self._settle_timer.cancel()
                        except Exception:
                            pass
                        self._settle_timer = None
                    self._is_settling = False
                    notify_peer_discovered = True
                    notify_args = (self._peer_address, self._local_bind_address, peer_spk_port, peer_inst)
            else:
                # Brand new peer or peer previously expired/settling
                best_route = self._elect_best_route(peer_inst)
                if best_route:
                    best_ip, best_local, best_seen = best_route
                else:
                    best_ip, best_local, best_seen = peer_ip, local_source_ip, now

                try:
                    best_medium = self.classifier.classify_interface(best_local)
                except Exception:
                    best_medium = InterfaceMedium.OTHER

                self._peer_instance_id = peer_inst
                self._peer_speaker_port = peer_spk_port
                self._peer_address = best_ip
                self._local_bind_address = best_local
                self._last_peer_seen = best_seen

                if best_medium == InterfaceMedium.WIRED_ETHERNET or self.election_delay <= 0.0:
                    # Wired Ethernet available immediately or settling disabled: commit route directly
                    if self._settle_timer:
                        try:
                            self._settle_timer.cancel()
                        except Exception:
                            pass
                        self._settle_timer = None
                    self._is_settling = False
                    notify_peer_discovered = True
                    notify_args = (self._peer_address, self._local_bind_address, peer_spk_port, peer_inst)
                else:
                    # Non-wired candidate (WIFI or OTHER): start or maintain election window
                    # Delay committing to allow potential Ethernet packet to arrive first.
                    self._is_settling = True
                    if self._settle_timer is None:
                        def _on_settle_timeout():
                            cb = None
                            args = None
                            with self._lock:
                                self._settle_timer = None
                                if not self._running or self._is_ambiguous or not self._peer_address:
                                    self._is_settling = False
                                    return
                                self._is_settling = False
                                cb = self.on_peer_discovered
                                args = (
                                    self._peer_address,
                                    self._local_bind_address,
                                    self._peer_speaker_port,
                                    self._peer_instance_id,
                                )
                            if cb and args:
                                cb(*args)

                        self._settle_timer = threading.Timer(self.election_delay, _on_settle_timeout)
                        self._settle_timer.daemon = True
                        self._settle_timer.start()

        if notify_peer_discovered and self.on_peer_discovered and notify_args:
            self.on_peer_discovered(*notify_args)


    def _listen_loop(self) -> None:
        while self._running and self._listener_sock:
            try:
                data, addr = self._listener_sock.recvfrom(4096)
            except (OSError, socket.error):
                break

            try:
                msg = json.loads(data.decode("utf-8"))
            except Exception:
                continue

            self.handle_peer_message(msg, addr[0])
