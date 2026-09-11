"""macOS Speaker Relay Proxy with Zero-Restart Dynamic Volume Scaling.

Intercepts UDP RTP L16 packets on an external listen port (default 5004)
and forwards them to an internal loopback port (default 5005) served by
the canonical GStreamer osxaudiosink receiver.

Dynamically scales big-endian 16-bit linear PCM audio samples in-place
without restarting or interrupting the GStreamer media pipeline:
- Volume 1.0 (100%): Direct zero-copy passthrough.
- Volume 0.0 (0% / Mute): Zeros out PCM payload preserving RTP framing and headers without dropping packets.
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

    @property
    def is_running(self) -> bool:
        """Returns True if the relay thread and incoming socket are actively running."""
        return self._running and self._thread is not None and self._thread.is_alive()

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

            out_data = self.process_packet(data, v)
            try:
                out_sock.sendto(out_data, target_addr)
            except Exception:
                pass

    def process_packet(self, data: bytes, volume: Optional[float] = None) -> bytes:
        """Processes a single RTP packet applying volume scaling to L16 PCM payload.

        RFC 3550 Compliant:
        - Volume 1.0 (>= 0.999): Zero-copy direct passthrough.
        - Header length calculation includes CC (CSRC list) and X (Header Extension).
        - If P (Padding bit) == 1:
          - The final octet of the packet contains the padding count.
          - Validates 0 < padding_count <= (len(data) - header_len).
          - If malformed: fails safely by passing through original data without modifying or crashing.
          - If valid: scales/zeroes ONLY the L16 PCM payload preceding padding.
          - Original padding bytes (including the final count octet) and RTP header remain bit-exact.
        - Volume 0.0 (<= 0.001): Zeroes PCM payload without dropping packets or touching header/padding.
        """
        if volume is None:
            with self._lock:
                v = self._volume
        else:
            v = volume

        # Volume 1.0 -> zero-copy direct passthrough
        if v >= 0.999:
            return data

        header_len = self.parse_rtp_header_length(data)
        if header_len is None or header_len >= len(data):
            # Malformed or header-only packet: passthrough without touching payload
            return data

        # Check RTP Padding bit (P-bit, bit 2 of octet 0, mask 0x20)
        p_bit = (data[0] >> 5) & 0x01
        padding_len = 0
        if p_bit:
            pad_count = data[-1]
            # Validate padding count: must be > 0 and must not exceed payload section
            if pad_count == 0 or pad_count > (len(data) - header_len):
                # Malformed padding: fail safely by returning unmodified data
                return data
            padding_len = pad_count

        hdr = data[:header_len]
        pcm_end = len(data) - padding_len
        pcm_payload = data[header_len:pcm_end]
        padding_bytes = data[pcm_end:]

        # Volume 0.0 -> zero-out PCM payload, preserving RTP header and padding bit-for-bit
        if v <= 0.001:
            zeroed_pcm = b"\x00" * len(pcm_payload)
            return hdr + zeroed_pcm + padding_bytes

        # Scale RTP L16 PCM payload
        scaled_pcm = self._scale_l16_payload(pcm_payload, v)
        return hdr + scaled_pcm + padding_bytes

    @staticmethod
    def parse_rtp_header_length(data: bytes) -> Optional[int]:
        """Calculates exact RTP header length (12 + 4*CC + 4 + 4*ext_len) per RFC 3550.

        Returns None if packet is smaller than 12 bytes or malformed.
        """
        if len(data) < 12:
            return None
        b0 = data[0]
        # Version must be 2
        version = (b0 >> 6) & 0x03
        if version != 2:
            return None
        x_bit = (b0 >> 4) & 0x01
        cc = b0 & 0x0F

        offset = 12 + cc * 4
        if len(data) < offset:
            return None

        if x_bit:
            # Header extension present
            if len(data) < offset + 4:
                return None
            import struct
            _, ext_len = struct.unpack(">HH", data[offset:offset + 4])
            offset += 4 + ext_len * 4
            if len(data) < offset:
                return None

        return offset

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
