"""macOS Speaker Relay Proxy with Zero-Restart Dynamic Volume Scaling.

Intercepts UDP RTP L16 packets on an external listen port (default 5004)
and forwards them to an internal loopback port (default 5005) served by
the canonical GStreamer osxaudiosink receiver.

Dynamically scales big-endian 16-bit linear PCM audio samples in-place
without restarting or interrupting the GStreamer media pipeline:
- Volume 1.0 (100%): Direct zero-copy passthrough.
- Volume 0.0 (0% / Mute): Drops packet, achieving absolute mute with zero CPU work.
- Volume 0.0 < V < 1.0: Vectorized sample scaling using numpy or struct.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Optional

try:
    import numpy as np
except ImportError:
    np = None

logger = logging.getLogger(__name__)


class SpeakerVolumeRelay:
    """Zero-restart UDP RTP volume scaling relay proxy."""

    def __init__(
        self,
        bind_ip: str,
        listen_port: int = 5004,
        target_port: int = 5005,
        target_ip: str = "127.0.0.1",
    ):
        self.bind_ip = bind_ip
        self.listen_port = listen_port
        self.target_ip = target_ip
        self.target_port = target_port

        self._volume: float = 1.0
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._in_sock: Optional[socket.socket] = None
        self._out_sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    @property
    def volume(self) -> float:
        with self._lock:
            return self._volume

    def set_volume(self, vol: float) -> None:
        """Sets volume factor between 0.0 and 1.0 atomically."""
        clamped = max(0.0, min(1.0, float(vol)))
        with self._lock:
            self._volume = clamped

    def start(self) -> bool:
        """Binds incoming socket and starts relay thread."""
        if self._running:
            return True

        try:
            s_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s_in.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s_in.bind((self.bind_ip, self.listen_port))
            s_in.settimeout(0.2)
            self._in_sock = s_in

            s_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._out_sock = s_out

            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._relay_loop,
                name="SpeakerVolumeRelay",
                daemon=True,
            )
            self._thread.start()
            logger.info(
                "SpeakerVolumeRelay started on %s:%d -> %s:%d",
                self.bind_ip,
                self.listen_port,
                self.target_ip,
                self.target_port,
            )
            return True
        except Exception as exc:
            logger.error("Failed to start SpeakerVolumeRelay: %s", exc)
            self.stop()
            return False

    def stop(self) -> None:
        """Stops relay thread and closes sockets."""
        self._running = False
        self._stop_event.set()

        if self._in_sock:
            try:
                self._in_sock.close()
            except Exception:
                pass
            self._in_sock = None

        if self._out_sock:
            try:
                self._out_sock.close()
            except Exception:
                pass
            self._out_sock = None

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        logger.info("SpeakerVolumeRelay stopped cleanly")

    def _relay_loop(self) -> None:
        target_addr = (self.target_ip, self.target_port)
        in_sock = self._in_sock
        out_sock = self._out_sock

        while not self._stop_event.is_set() and in_sock and out_sock:
            try:
                data, _ = in_sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            if not data:
                continue

            with self._lock:
                v = self._volume

            # Volume 1.0 -> zero-copy direct passthrough
            if v >= 0.999:
                try:
                    out_sock.sendto(data, target_addr)
                except Exception:
                    pass
                continue

            # Volume 0.0 -> full mute (drop packet)
            if v <= 0.001:
                continue

            # Scale RTP L16 payload
            # Standard RTP header length is at least 12 bytes
            if len(data) <= 12:
                try:
                    out_sock.sendto(data, target_addr)
                except Exception:
                    pass
                continue

            hdr, payload = data[:12], data[12:]
            scaled_payload = self._scale_l16_payload(payload, v)
            try:
                out_sock.sendto(hdr + scaled_payload, target_addr)
            except Exception:
                pass

    @staticmethod
    def _scale_l16_payload(payload: bytes, volume: float) -> bytes:
        """Scales big-endian 16-bit linear PCM audio bytes by volume factor."""
        if np is not None:
            arr = np.frombuffer(payload, dtype=">i2")
            return (arr * volume).astype(">i2").tobytes()

        # Fallback pure python struct unpacking if numpy is absent
        import struct
        count = len(payload) // 2
        shorts = struct.unpack(f">{count}h", payload[:count*2])
        scaled = [int(s * volume) for s in shorts]
        return struct.pack(f">{count}h", *scaled)
