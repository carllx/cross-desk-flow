"""CoreAudio device resolver for macOS.

Resolves MacBook built-in speakers stably by inspecting:
- CoreAudio Transport Type == "bltn" (Built-in)
- Actual output channel capacity > 0 (parsed from AudioBufferList output stream configuration)
- Stable CoreAudio Device UID
- Diagnostic name preference (e.g. "Speakers", "内置扬声器", "Built-in Output") and default output fallback

Maps stable identity to runtime AudioDeviceID without hardcoding temporary integers or UIDs into Git.
"""

import ctypes
import json
import logging
import os
import struct
from typing import List, NamedTuple, Optional

logger = logging.getLogger(__name__)

DEFAULT_MIC_UID_HINT_FILE = os.path.expanduser(
    "~/Library/Application Support/desk-audio-bridge/microphone_uid_hint.json"
)

# FourCC helper
def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode("ascii"))[0]

# CoreAudio constants
kAudioObjectSystemObject = 1
kAudioHardwarePropertyDevices = _fourcc("dev#")
kAudioHardwarePropertyDefaultOutputDevice = _fourcc("dOut")
kAudioHardwarePropertyDefaultInputDevice = _fourcc("dIn ")
kAudioObjectPropertyScopeGlobal = _fourcc("glob")
kAudioObjectPropertyScopeInput = _fourcc("inpt")
kAudioObjectPropertyScopeOutput = _fourcc("outp")
kAudioObjectPropertyElementMain = 0

kAudioObjectPropertyName = _fourcc("lnam")
kAudioDevicePropertyDeviceUID = _fourcc("uid ")
kAudioDevicePropertyTransportType = _fourcc("tran")
kAudioDevicePropertyStreamConfiguration = _fourcc("slay")
kAudioDeviceTransportTypeBuiltIn = _fourcc("bltn")


class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


class ResolvedAudioDevice(NamedTuple):
    device_id: int
    device_uid: str
    device_name: str
    is_builtin: bool
    output_channels: int
    is_default: bool
    input_channels: int = 0


class MacCoreAudioDeviceResolver:
    """Resolves the macOS built-in speakers endpoint dynamically via CoreAudio C API."""

    def __init__(self, mic_hint_file: Optional[str] = None):
        self.mic_hint_file = mic_hint_file or DEFAULT_MIC_UID_HINT_FILE
        try:
            self._coreaudio = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
            )
            self._cf = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
            self._cf.CFStringGetCString.argtypes = [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_long,
                ctypes.c_uint32,
            ]
            self._cf.CFStringGetCString.restype = ctypes.c_bool
            self._cf.CFRelease.argtypes = [ctypes.c_void_p]
            self._initialized = True
        except Exception as exc:
            logger.warning("CoreAudio library initialization failed: %s", exc)
            self._initialized = False

    def _get_string_property(self, dev_id: int, selector: int) -> str:
        if not self._initialized:
            return ""
        val_ref = ctypes.c_void_p()
        val_size = ctypes.c_uint32(ctypes.sizeof(val_ref))
        addr = AudioObjectPropertyAddress(
            selector, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain
        )
        st = self._coreaudio.AudioObjectGetPropertyData(
            dev_id, ctypes.byref(addr), 0, None, ctypes.byref(val_size), ctypes.byref(val_ref)
        )
        if st == 0 and val_ref.value:
            buf = ctypes.create_string_buffer(512)
            # 0x08000100 = kCFStringEncodingUTF8
            if self._cf.CFStringGetCString(val_ref, buf, 512, 0x08000100):
                res = buf.value.decode("utf-8", errors="replace")
            else:
                res = ""
            self._cf.CFRelease(val_ref)
            return res
        return ""

    def _get_transport_type(self, dev_id: int) -> int:
        if not self._initialized:
            return 0
        tran = ctypes.c_uint32(0)
        tran_size = ctypes.c_uint32(ctypes.sizeof(tran))
        addr = AudioObjectPropertyAddress(
            kAudioDevicePropertyTransportType,
            kAudioObjectPropertyScopeGlobal,
            kAudioObjectPropertyElementMain,
        )
        st = self._coreaudio.AudioObjectGetPropertyData(
            dev_id, ctypes.byref(addr), 0, None, ctypes.byref(tran_size), ctypes.byref(tran)
        )
        if st == 0:
            return tran.value
        return 0

    def _get_output_channels(self, dev_id: int) -> int:
        if not self._initialized:
            return 0
        addr = AudioObjectPropertyAddress(
            kAudioDevicePropertyStreamConfiguration,
            kAudioObjectPropertyScopeOutput,
            kAudioObjectPropertyElementMain,
        )
        buf_size = ctypes.c_uint32(0)
        st = self._coreaudio.AudioObjectGetPropertyDataSize(
            dev_id, ctypes.byref(addr), 0, None, ctypes.byref(buf_size)
        )
        if st != 0 or buf_size.value == 0:
            return 0

        raw_buf = ctypes.create_string_buffer(buf_size.value)
        st = self._coreaudio.AudioObjectGetPropertyData(
            dev_id, ctypes.byref(addr), 0, None, ctypes.byref(buf_size), raw_buf
        )
        if st != 0:
            return 0

        # AudioBufferList on 64-bit macOS:
        # UInt32 mNumberBuffers (4 bytes)
        # 4 bytes padding (aligning mBuffers to 8 bytes boundary)
        # Array of AudioBuffer (16 bytes each: UInt32 channels, UInt32 size, void* data)
        try:
            num_buffers = struct.unpack_from("<I", raw_buf.raw, 0)[0]
            total_channels = 0
            offset = 8
            for _ in range(num_buffers):
                if offset + 4 <= buf_size.value:
                    channels = struct.unpack_from("<I", raw_buf.raw, offset)[0]
                    total_channels += channels
                    offset += 16
            return total_channels
        except Exception:
            return 0

    def _get_input_channels(self, dev_id: int) -> int:
        if not self._initialized:
            return 0
        addr = AudioObjectPropertyAddress(
            kAudioDevicePropertyStreamConfiguration,
            kAudioObjectPropertyScopeInput,
            kAudioObjectPropertyElementMain,
        )
        buf_size = ctypes.c_uint32(0)
        st = self._coreaudio.AudioObjectGetPropertyDataSize(
            dev_id, ctypes.byref(addr), 0, None, ctypes.byref(buf_size)
        )
        if st != 0 or buf_size.value == 0:
            return 0

        raw_buf = ctypes.create_string_buffer(buf_size.value)
        st = self._coreaudio.AudioObjectGetPropertyData(
            dev_id, ctypes.byref(addr), 0, None, ctypes.byref(buf_size), raw_buf
        )
        if st != 0:
            return 0

        try:
            num_buffers = struct.unpack_from("<I", raw_buf.raw, 0)[0]
            total_channels = 0
            offset = 8
            for _ in range(num_buffers):
                if offset + 4 <= buf_size.value:
                    channels = struct.unpack_from("<I", raw_buf.raw, offset)[0]
                    total_channels += channels
                    offset += 16
            return total_channels
        except Exception:
            return 0

    def _get_default_output_device_id(self) -> int:
        if not self._initialized:
            return 0
        addr = AudioObjectPropertyAddress(
            kAudioHardwarePropertyDefaultOutputDevice,
            kAudioObjectPropertyScopeGlobal,
            kAudioObjectPropertyElementMain,
        )
        default_dev = ctypes.c_uint32(0)
        size = ctypes.c_uint32(ctypes.sizeof(default_dev))
        st = self._coreaudio.AudioObjectGetPropertyData(
            kAudioObjectSystemObject,
            ctypes.byref(addr),
            0,
            None,
            ctypes.byref(size),
            ctypes.byref(default_dev),
        )
        if st == 0:
            return default_dev.value
        return 0

    def _get_default_input_device_id(self) -> int:
        if not self._initialized:
            return 0
        addr = AudioObjectPropertyAddress(
            kAudioHardwarePropertyDefaultInputDevice,
            kAudioObjectPropertyScopeGlobal,
            kAudioObjectPropertyElementMain,
        )
        default_dev = ctypes.c_uint32(0)
        size = ctypes.c_uint32(ctypes.sizeof(default_dev))
        st = self._coreaudio.AudioObjectGetPropertyData(
            kAudioObjectSystemObject,
            ctypes.byref(addr),
            0,
            None,
            ctypes.byref(size),
            ctypes.byref(default_dev),
        )
        if st == 0:
            return default_dev.value
        return 0

    def enumerate_output_devices(self) -> List[ResolvedAudioDevice]:
        if not self._initialized:
            return []

        addr = AudioObjectPropertyAddress(
            kAudioHardwarePropertyDevices,
            kAudioObjectPropertyScopeGlobal,
            kAudioObjectPropertyElementMain,
        )
        size = ctypes.c_uint32(0)
        st = self._coreaudio.AudioObjectGetPropertyDataSize(
            kAudioObjectSystemObject, ctypes.byref(addr), 0, None, ctypes.byref(size)
        )
        if st != 0 or size.value == 0:
            return []

        num_devices = size.value // ctypes.sizeof(ctypes.c_uint32)
        dev_ids = (ctypes.c_uint32 * num_devices)()
        st = self._coreaudio.AudioObjectGetPropertyData(
            kAudioObjectSystemObject,
            ctypes.byref(addr),
            0,
            None,
            ctypes.byref(size),
            ctypes.byref(dev_ids),
        )
        if st != 0:
            return []

        default_id = self._get_default_output_device_id()
        devices = []
        for dev_id in dev_ids:
            uid = self._get_string_property(dev_id, kAudioDevicePropertyDeviceUID)
            name = self._get_string_property(dev_id, kAudioObjectPropertyName)
            tran = self._get_transport_type(dev_id)
            channels = self._get_output_channels(dev_id)
            is_builtin = (tran == kAudioDeviceTransportTypeBuiltIn)
            is_default = (dev_id == default_id)

            if channels > 0:
                devices.append(
                    ResolvedAudioDevice(
                        device_id=dev_id,
                        device_uid=uid,
                        device_name=name,
                        is_builtin=is_builtin,
                        output_channels=channels,
                        is_default=is_default,
                    )
                )
        return devices

    def resolve_builtin_speaker_device(self) -> Optional[ResolvedAudioDevice]:
        """Stably resolves the MacBook built-in speaker output device.
        
        Priority:
        1. Built-in transport (`bltn`) + output channels > 0 + name contains speaker keywords
        2. Built-in transport (`bltn`) + output channels > 0 + is default output device
        3. Any Built-in transport (`bltn`) with output channels > 0
        
        Refuses to fallback to non-built-in devices (virtual drivers, HDMI, etc.).
        """
        all_outputs = self.enumerate_output_devices()
        builtin_outputs = [d for d in all_outputs if d.is_builtin and d.output_channels > 0]
        if not builtin_outputs:
            logger.warning("No built-in output devices found with active output channels")
            return None

        # Priority 1: Builtin with speaker in name
        speaker_keywords = ("speaker", "扬声器", "built-in output", "internal speaker")
        for d in builtin_outputs:
            lower_name = d.device_name.lower()
            if any(kw in lower_name for kw in speaker_keywords):
                return d

        # Priority 2: Builtin default
        for d in builtin_outputs:
            if d.is_default:
                return d

        # Priority 3: First available builtin output
        return builtin_outputs[0]

    def enumerate_input_devices(self) -> List[ResolvedAudioDevice]:
        if not self._initialized:
            return []

        addr = AudioObjectPropertyAddress(
            kAudioHardwarePropertyDevices,
            kAudioObjectPropertyScopeGlobal,
            kAudioObjectPropertyElementMain,
        )
        size = ctypes.c_uint32(0)
        st = self._coreaudio.AudioObjectGetPropertyDataSize(
            kAudioObjectSystemObject, ctypes.byref(addr), 0, None, ctypes.byref(size)
        )
        if st != 0 or size.value == 0:
            return []

        num_devices = size.value // ctypes.sizeof(ctypes.c_uint32)
        dev_ids = (ctypes.c_uint32 * num_devices)()
        st = self._coreaudio.AudioObjectGetPropertyData(
            kAudioObjectSystemObject,
            ctypes.byref(addr),
            0,
            None,
            ctypes.byref(size),
            ctypes.byref(dev_ids),
        )
        if st != 0:
            return []

        default_id = self._get_default_input_device_id()
        devices = []
        for dev_id in dev_ids:
            uid = self._get_string_property(dev_id, kAudioDevicePropertyDeviceUID)
            name = self._get_string_property(dev_id, kAudioObjectPropertyName)
            tran = self._get_transport_type(dev_id)
            channels = self._get_input_channels(dev_id)
            is_builtin = (tran == kAudioDeviceTransportTypeBuiltIn)
            is_default = (dev_id == default_id)

            if channels > 0:
                devices.append(
                    ResolvedAudioDevice(
                        device_id=dev_id,
                        device_uid=uid,
                        device_name=name,
                        is_builtin=is_builtin,
                        output_channels=0,
                        is_default=is_default,
                        input_channels=channels,
                    )
                )
        return devices

    def _load_mic_uid_hint(self) -> Optional[str]:
        """读取本地缓存的麦克风 CoreAudio Device UID 提示。"""
        if not self.mic_hint_file or not os.path.exists(self.mic_hint_file):
            return None
        try:
            with open(self.mic_hint_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                uid = data.get("microphone_uid")
                if isinstance(uid, str) and uid.strip():
                    return uid.strip()
        except Exception as exc:
            logger.debug("读取麦克风 UID 提示失败: %s", exc)
        return None

    def _persist_mic_uid_hint(self, uid: str) -> None:
        """持久化麦克风 CoreAudio Device UID 提示到本地（绝不记录临时 DeviceID）。"""
        clean_uid = uid.strip() if isinstance(uid, str) else ""
        if not self.mic_hint_file or not clean_uid:
            return
        try:
            os.makedirs(os.path.dirname(self.mic_hint_file), exist_ok=True)
            with open(self.mic_hint_file, "w", encoding="utf-8") as f:
                json.dump({"microphone_uid": clean_uid}, f)
        except Exception as exc:
            logger.warning("持久化麦克风 UID 提示失败: %s", exc)

    def resolve_builtin_microphone_device(self) -> Optional[ResolvedAudioDevice]:
        """稳定解析 MacBook 内置麦克风输入设备。

        解析与身份定位流程：
        1. 枚举当前 CoreAudio 输入设备；
        2. 过滤有效内置候选：transport 为内置 (bltn)、输入通道数 > 0 且拥有非空稳定 Device UID；
        3. 若本地存在缓存的 UID hint 且命中当前有效候选，直接返回其当前运行时 AudioDeviceID；
        4. 若缓存 UID 过期或不存在：在有效内置设备中执行匹配（名称关键字 -> 默认设备 -> 首个设备），
           将其非空 Device UID 作为新 hint 持久化并返回其当前运行时 AudioDeviceID；
        5. 若无任何有效内置麦克风设备，返回 None（严格 fail-closed，绝不回退至非内置设备）。
        """
        all_inputs = self.enumerate_input_devices()
        builtin_inputs = [
            d
            for d in all_inputs
            if d.is_builtin and d.input_channels > 0 and d.device_uid and d.device_uid.strip()
        ]
        if not builtin_inputs:
            logger.warning("未发现具备有效输入通道及非空 UID 的内置输入设备")
            return None

        # 检查本地缓存的 UID hint 是否命中当前有效内置麦克风
        cached_uid = self._load_mic_uid_hint()
        if cached_uid:
            for d in builtin_inputs:
                if d.device_uid == cached_uid:
                    return d
            logger.debug("缓存的麦克风 UID %s 已失效或未匹配到当前设备", cached_uid)

        # 缓存缺失或失效：在有效候选设备中进行重新发现
        selected: Optional[ResolvedAudioDevice] = None
        mic_keywords = ("microphone", "麦克风", "mic", "built-in input", "internal mic")
        for d in builtin_inputs:
            lower_name = d.device_name.lower()
            if any(kw in lower_name for kw in mic_keywords):
                selected = d
                break

        # 降级尝试：系统默认输入设备
        if not selected:
            for d in builtin_inputs:
                if d.is_default:
                    selected = d
                    break

        # 降级尝试：首个可用内置输入设备
        if not selected:
            selected = builtin_inputs[0]

        # 必须存在有效非空 Device UID 才能作为稳定设备返回
        if selected and selected.device_uid and selected.device_uid.strip():
            self._persist_mic_uid_hint(selected.device_uid.strip())
            return selected

        return None


def check_microphone_authorization() -> int:
    """Probes macOS microphone authorization status via AVFoundation.

    Return values map to AVAuthorizationStatus:
      0: NotDetermined
      1: Restricted
      2: Denied
      3: Authorized
      -1: Unknown / probe error
    Does NOT request permission or trigger UI prompts.
    """
    try:
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_msgSend.restype = ctypes.c_long
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]

        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        cls = objc.objc_getClass(b"AVCaptureDevice")
        sel = objc.sel_registerName(b"authorizationStatusForMediaType:")
        # 'soun' corresponds to AVMediaTypeAudio
        media_type = cf.CFStringCreateWithCString(None, b"soun", 0x08000100)
        try:
            status = objc.objc_msgSend(cls, sel, media_type)
            return int(status)
        finally:
            cf.CFRelease(media_type)
    except Exception as exc:
        logger.debug("Microphone authorization check probe error: %s", exc)
        return -1

