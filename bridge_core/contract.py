"""Shared cross-platform controller contract for desk-audio-bridge.

Defines protocol constants, state enums, status structures, and handshake models
shared by both Windows and macOS controller implementations.
"""

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, Optional

# Control plane protocol constants
CONTROL_PROTOCOL_VERSION = 1
DEFAULT_CONTROL_PORT = 50100
DEFAULT_SPEAKER_RTP_PORT = 5004
DEFAULT_SPEAKER_INTERNAL_RTP_PORT = 5005
DEFAULT_MIC_RTP_PORT = 5006

# Local IPC control surface
DEFAULT_LOCAL_IPC_PORT = 50106
DEFAULT_SINGLETON_PORT = 50105

# RTP L16 canonical audio parameters
CANONICAL_SAMPLE_RATE = 48000
CANONICAL_CHANNELS = 2
CANONICAL_PAYLOAD_TYPE = 96
CANONICAL_ENCODING_NAME = "L16"

# RTP L16 canonical audio parameters - Microphone (Mac mic -> Windows)
CANONICAL_MIC_SAMPLE_RATE = 48000
CANONICAL_MIC_CHANNELS = 1
CANONICAL_MIC_PAYLOAD_TYPE = 97
CANONICAL_MIC_ENCODING_NAME = "L16"


class HostRole(str, Enum):
    WINDOWS = "windows"
    MACOS = "macos"


class DesiredState(str, Enum):
    ENABLED = "ENABLED"
    STOPPED_BY_USER = "STOPPED_BY_USER"


class LifecycleState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    DISCOVERING = "DISCOVERING"
    READY = "READY"
    ACTIVE = "ACTIVE"
    AMBIGUOUS_PEER = "AMBIGUOUS_PEER"
    ERROR = "ERROR"


class PathState(str, Enum):
    IDLE = "IDLE"
    READY = "READY"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


@dataclass
class HandshakeHello:
    version: int
    role: str
    instance_id: str
    speaker_port: int
    source_ip: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HandshakeAck:
    version: int
    role: str
    instance_id: str
    speaker_port: int
    peer_instance_id: str
    source_ip: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ControllerStatus:
    controller_state: str
    desired_state: str
    role: str
    peer_available: bool
    peer_address: Optional[str]
    local_bind_address: Optional[str]
    speaker_path_state: str
    speaker_target_port: int
    last_actionable_error: Optional[str]
    owned_children_count: int
    owner_pid: Optional[int] = None
    microphone_path_state: str = PathState.IDLE.value
    microphone_port: int = DEFAULT_MIC_RTP_PORT
    pack43_available: Optional[bool] = None
    last_actionable_microphone_error: Optional[str] = None
    mode: str = "PLAYBACK"
    duck_level: Optional[int] = None
    local_voice_active: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Dictation Control Plane Protocol Constants (Port 50100)
DICTATION_START = "DICTATION_START"
DICTATION_START_ACK = "DICTATION_START_ACK"
DICTATION_STOP = "DICTATION_STOP"
DICTATION_STOP_ACK = "DICTATION_STOP_ACK"

