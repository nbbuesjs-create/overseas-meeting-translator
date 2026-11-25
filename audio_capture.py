"""Cross-platform audio capture (Windows WASAPI Loopback & macOS/Linux sounddevice) with VAD."""

from __future__ import annotations

import io
import queue
import sys
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import numpy as np

from config import AppConfig

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import pyaudiowpatch as pyaudio
else:
    import sounddevice as sd

TARGET_SAMPLE_RATE = 16000

_VIRTUAL_LOOPBACK_KEYWORDS = (
    "blackhole",
    "loopback",
    "soundflower",
    "vb-cable",
    "background music",
    "existential audio",
    "monitor of",
    "virtual",
)


@dataclass
class AudioDeviceInfo:
    index: int
    name: str
    channels: int
    sample_rate: int
    is_loopback: bool
    is_default: bool = False

    @property
    def display_name(self) -> str:
        prefix = "[默认] " if self.is_default else ""
        return f"{prefix}{self.name} ({self.sample_rate}Hz, {self.channels}ch)"


@dataclass
class AudioSegment:
    channel_id: str  # "system" | "mic"
    channel_label: str  # "系统" | "麦克风"
    wav_bytes: bytes
    duration_sec: float
    timestamp: str


def pcm_float_to_wav_bytes(samples: np.ndarray, sample_rate: int = TARGET_SAMPLE_RATE) -> bytes:
    """Encode float32 [-1.0, 1.0] mono audio into 16-bit PCM WAV bytes."""
    clipped = np.clip(samples, -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


def resample_float_to_16k_mono(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Convert float32 1D or (frames, channels) audio array into 16kHz mono float32."""
    if samples.size == 0:
        return np.empty(0, dtype=np.float32)

    if samples.ndim > 1:
        mono = samples.mean(axis=1, dtype=np.float32)
    else:
        mono = samples.astype(np.float32, copy=False)

    if sample_rate != TARGET_SAMPLE_RATE and len(mono) > 1:
        target_len = int(round(len(mono) * TARGET_SAMPLE_RATE / sample_rate))
        if target_len <= 0:
            return np.empty(0, dtype=np.float32)
        x_old = np.linspace(0.0, 1.0, len(mono), endpoint=False)
        x_new = np.linspace(0.0, 1.0, target_len, endpoint=False)
        mono = np.interp(x_new, x_old, mono).astype(np.float32)

    return mono


def resample_to_16k_mono(raw_bytes: bytes, channels: int, sample_rate: int) -> np.ndarray:
    """Convert raw int16 interleaved audio bytes into 16kHz mono float32."""
    if not raw_bytes:
        return np.empty(0, dtype=np.float32)

    pcm = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        usable = (len(pcm) // channels) * channels
        if usable == 0:
            return np.empty(0, dtype=np.float32)
        mono = pcm[:usable].reshape(-1, channels).mean(axis=1)
    else:
        mono = pcm

    return resample_float_to_16k_mono(mono, sample_rate)


def _is_virtual_loopback_name(name: str) -> bool:
    low = name.lower()
    return any(kw in low for kw in _VIRTUAL_LOOPBACK_KEYWORDS)


def _list_audio_devices_windows() -> tuple[list[AudioDeviceInfo], list[AudioDeviceInfo]]:
    """Enumerate Windows WASAPI loopback devices and microphone input devices."""
    loopbacks: list[AudioDeviceInfo] = []
    mics: list[AudioDeviceInfo] = []

    p = pyaudio.PyAudio()
    try:
        wasapi_api_index = -1
        default_output_name = ""
        default_wasapi_input_index = -1
        default_global_input_index = -1

        try:
            wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            wasapi_api_index = int(wasapi_info["index"])
            default_out_idx = int(wasapi_info.get("defaultOutputDevice", -1))
            if default_out_idx >= 0:
                default_out_info = p.get_device_info_by_index(default_out_idx)
                default_output_name = str(default_out_info.get("name", ""))
            default_wasapi_input_index = int(wasapi_info.get("defaultInputDevice", -1))
        except Exception:
            pass

        try:
            default_in_info = p.get_default_input_device_info()
            default_global_input_index = int(default_in_info["index"])
        except Exception:
            pass

        try:
            for dev in p.get_loopback_device_info_generator():
                idx = int(dev["index"])
                name = str(dev["name"])
                ch = max(1, int(dev.get("maxInputChannels", 2)))
                sr = max(8000, int(dev.get("defaultSampleRate", 48000)))
                is_def = bool(default_output_name and default_output_name in name)
                loopbacks.append(
                    AudioDeviceInfo(
                        index=idx,
                        name=name,
                        channels=ch,
                        sample_rate=sr,
                        is_loopback=True,
                        is_default=is_def,
                    )
                )
        except Exception:
            pass

        loopbacks.sort(key=lambda d: (not d.is_default, d.name.lower()))

        wasapi_mics: list[AudioDeviceInfo] = []
        other_mics: list[AudioDeviceInfo] = []

        for i in range(p.get_device_count()):
            try:
                dev = p.get_device_info_by_index(i)
            except Exception:
                continue
            if dev.get("isLoopbackDevice", False):
                continue
            max_in = int(dev.get("maxInputChannels", 0))
            if max_in <= 0:
                continue

            name = str(dev.get("name", f"Input {i}")).strip()
            sr = max(8000, int(dev.get("defaultSampleRate", 48000)))
            host_api = int(dev.get("hostApi", -1))

            if host_api == wasapi_api_index:
                is_def = i == default_wasapi_input_index
                wasapi_mics.append(
                    AudioDeviceInfo(
                        index=i,
                        name=name,
                        channels=max_in,
                        sample_rate=sr,
                        is_loopback=False,
                        is_default=is_def,
                    )
                )
            else:
                is_def = i == default_global_input_index
                other_mics.append(
                    AudioDeviceInfo(
                        index=i,
                        name=name,
                        channels=max_in,
                        sample_rate=sr,
                        is_loopback=False,
                        is_default=is_def,
                    )
                )

        if wasapi_mics:
            mics = wasapi_mics
        else:
            dedup: list[AudioDeviceInfo] = []
            seen_other: set[str] = set()
            for m in other_mics:
                key = m.name.lower()
                if key not in seen_other:
                    seen_other.add(key)
                    dedup.append(m)
            mics = dedup

        mics.sort(key=lambda d: (not d.is_default, d.name.lower()))
    finally:
        p.terminate()

    return loopbacks, mics


def _list_audio_devices_sounddevice() -> tuple[list[AudioDeviceInfo], list[AudioDeviceInfo]]:
    """Enumerate virtual loopback devices (e.g. BlackHole) and microphones on macOS / Linux."""
    virtual_loopbacks: list[AudioDeviceInfo] = []
    other_inputs_as_sys: list[AudioDeviceInfo] = []
    normal_mics: list[AudioDeviceInfo] = []
    all_inputs: list[AudioDeviceInfo] = []

    default_in_idx = -1
    try:
        def_pair = sd.default.device
        if isinstance(def_pair, (list, tuple)) and len(def_pair) >= 1 and def_pair[0] is not None:
            default_in_idx = int(def_pair[0])
        elif isinstance(def_pair, int):
            default_in_idx = def_pair
    except Exception:
        pass

    devices = sd.query_devices()
    for idx, dev in enumerate(devices):
        max_in = int(dev.get("max_input_channels", 0))
        if max_in <= 0:
            continue
        name = str(dev.get("name", f"Input {idx}")).strip()
        sr = max(8000, int(round(float(dev.get("default_samplerate", 48000)))))
        is_virt = _is_virtual_loopback_name(name)
        is_def_in = idx == default_in_idx

        info_mic = AudioDeviceInfo(
            index=idx,
            name=name,
            channels=max_in,
            sample_rate=sr,
            is_loopback=False,
            is_default=is_def_in,
        )
        all_inputs.append(info_mic)

        if is_virt:
            virtual_loopbacks.append(
                AudioDeviceInfo(
                    index=idx,
                    name=name,
                    channels=max_in,
                    sample_rate=sr,
                    is_loopback=True,
                    is_default=False,
                )
            )
        else:
            normal_mics.append(info_mic)
            other_inputs_as_sys.append(
                AudioDeviceInfo(
                    index=idx,
                    name=name,
                    channels=max_in,
                    sample_rate=sr,
                    is_loopback=True,
                    is_default=False,
                )
            )

    if virtual_loopbacks:
        virtual_loopbacks.sort(key=lambda d: d.name.lower())
        virtual_loopbacks[0].is_default = True
    loopbacks = virtual_loopbacks + sorted(other_inputs_as_sys, key=lambda d: d.name.lower())

    mics = normal_mics if normal_mics else all_inputs
    mics.sort(key=lambda d: (not d.is_default, d.name.lower()))
    return loopbacks, mics


def list_audio_devices() -> tuple[list[AudioDeviceInfo], list[AudioDeviceInfo]]:
    """Enumerate system loopback devices and microphone input devices for the current OS."""
    if IS_WINDOWS:
        return _list_audio_devices_windows()
    return _list_audio_devices_sounddevice()


class _BaseVADWorker:
    """Shared VAD segmentation loop for both Windows WASAPI and macOS/Linux sounddevice."""

    def __init__(
        self,
        channel_id: str,
        channel_label: str,
        cfg: AppConfig,
        on_segment: Callable[[AudioSegment], None],
        on_level: Callable[[str, float], None],
        on_error: Callable[[str], None],
    ) -> None:
        self._channel_id = channel_id
        self._channel_label = channel_label
        self._cfg = cfg
        self._on_segment = on_segment
        self._on_level = on_level
        self._on_error = on_error
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _next_mono_chunk(self, timeout: float = 0.1) -> np.ndarray | None:
        raise NotImplementedError

    def _emit_frames(self, frames: list[np.ndarray]) -> None:
        if not frames:
            return
        audio = np.concatenate(frames)
        duration = len(audio) / float(TARGET_SAMPLE_RATE)
        if duration < self._cfg.min_speech_duration:
            return
        wav_bytes = pcm_float_to_wav_bytes(audio, TARGET_SAMPLE_RATE)
        seg = AudioSegment(
            channel_id=self._channel_id,
            channel_label=self._channel_label,
            wav_bytes=wav_bytes,
            duration_sec=duration,
            timestamp=datetime.now().strftime("%H:%M:%S"),
        )
        try:
            self._on_segment(seg)
        except Exception as exc:
            self._on_error(f"音频分片回调错误: {exc}")

    def _run_loop(self) -> None:
        pre_roll: deque[np.ndarray] = deque(maxlen=3)
        speech_frames: list[np.ndarray] = []
        speech_samples = 0
        is_speaking = False
        last_voice_ts = time.monotonic()

        silence_limit = max(0.35, float(self._cfg.silence_duration))
        max_samples = int(max(2.0, float(self._cfg.max_chunk_duration)) * TARGET_SAMPLE_RATE)
        overlap_samples = int(0.2 * TARGET_SAMPLE_RATE)

        try:
            while not self._stop_event.is_set():
                mono = self._next_mono_chunk(timeout=0.1)
                rms = (
                    float(np.sqrt(np.mean(mono * mono)))
                    if (mono is not None and len(mono) > 0)
                    else 0.0
                )

                self._on_level(self._channel_id, rms)
                now = time.monotonic()
                threshold = max(0.001, float(self._cfg.vad_threshold))

                if mono is not None and len(mono) > 0:
                    if rms >= threshold:
                        if not is_speaking:
                            is_speaking = True
                            speech_frames = list(pre_roll)
                            speech_samples = sum(len(f) for f in speech_frames)
                        speech_frames.append(mono)
                        speech_samples += len(mono)
                        last_voice_ts = now
                    else:
                        pre_roll.append(mono)
                        if is_speaking:
                            speech_frames.append(mono)
                            speech_samples += len(mono)

                if is_speaking:
                    silence_elapsed = now - last_voice_ts
                    if silence_elapsed >= silence_limit:
                        self._emit_frames(speech_frames)
                        speech_frames = []
                        speech_samples = 0
                        is_speaking = False
                    elif speech_samples >= max_samples:
                        full_audio = np.concatenate(speech_frames)
                        self._emit_frames([full_audio])
                        if len(full_audio) > overlap_samples:
                            tail = full_audio[-overlap_samples:]
                            speech_frames = [tail]
                            speech_samples = len(tail)
                        else:
                            speech_frames = []
                            speech_samples = 0
                        last_voice_ts = now

            if is_speaking and speech_frames:
                self._emit_frames(speech_frames)
        except Exception as exc:
            self._on_error(f"{self._channel_label}采集线程异常: {exc}")
        finally:
            self._on_level(self._channel_id, 0.0)


class _WindowsCaptureWorker(_BaseVADWorker):
    """Windows WASAPI loopback / microphone worker backed by PyAudioWPatch."""

    def __init__(
        self,
        pa: Any,
        device_info: dict,
        channel_id: str,
        channel_label: str,
        cfg: AppConfig,
        on_segment: Callable[[AudioSegment], None],
        on_level: Callable[[str, float], None],
        on_error: Callable[[str], None],
    ) -> None:
        super().__init__(channel_id, channel_label, cfg, on_segment, on_level, on_error)
        self._pa = pa
        self._device_info = device_info
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=250)
        self._stream: Any = None

        self._sample_rate = max(8000, int(device_info.get("defaultSampleRate", 48000)))
        max_ch = max(1, int(device_info.get("maxInputChannels", 1)))
        self._channels = max_ch if device_info.get("isLoopbackDevice", False) else min(max_ch, 2)

    def _next_mono_chunk(self, timeout: float = 0.1) -> np.ndarray | None:
        try:
            raw = self._queue.get(timeout=timeout)
            return resample_to_16k_mono(raw, self._channels, self._sample_rate)
        except queue.Empty:
            return None

    def start(self) -> None:
        frames_per_buffer = max(256, int(self._sample_rate * 0.1))
        dev_idx = int(self._device_info["index"])

        def _callback(in_data, frame_count, time_info, status_flags):
            if not self._stop_event.is_set() and in_data:
                try:
                    self._queue.put_nowait(in_data)
                except queue.Full:
                    try:
                        self._queue.get_nowait()
                        self._queue.put_nowait(in_data)
                    except Exception:
                        pass
            return (None, pyaudio.paContinue)

        open_errors: list[str] = []
        channel_candidates = [self._channels]
        if not self._device_info.get("isLoopbackDevice", False):
            native_ch = max(1, int(self._device_info.get("maxInputChannels", 1)))
            for c in (native_ch, 1):
                if c not in channel_candidates:
                    channel_candidates.append(c)

        for ch in channel_candidates:
            try:
                self._stream = self._pa.open(
                    format=pyaudio.paInt16,
                    channels=ch,
                    rate=self._sample_rate,
                    input=True,
                    input_device_index=dev_idx,
                    frames_per_buffer=frames_per_buffer,
                    stream_callback=_callback,
                )
                self._channels = ch
                break
            except Exception as exc:
                open_errors.append(f"{ch}ch: {exc}")
                self._stream = None

        if self._stream is None:
            raise RuntimeError(
                f"无法打开{self._channel_label}设备 [{self._device_info.get('name')}]: "
                + "; ".join(open_errors)
            )

        self._stream.start_stream()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"AudioVAD-{self._channel_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._stream is not None:
            try:
                if self._stream.is_active():
                    self._stream.stop_stream()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None


class _SoundDeviceCaptureWorker(_BaseVADWorker):
    """macOS / Linux audio capture worker backed by sounddevice."""

    def __init__(
        self,
        device_info: AudioDeviceInfo,
        channel_id: str,
        channel_label: str,
        cfg: AppConfig,
        on_segment: Callable[[AudioSegment], None],
        on_level: Callable[[str, float], None],
        on_error: Callable[[str], None],
    ) -> None:
        super().__init__(channel_id, channel_label, cfg, on_segment, on_level, on_error)
        self._device_info = device_info
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=250)
        self._stream: Any = None
        self._sample_rate = max(8000, int(device_info.sample_rate))
        self._channels = min(max(1, int(device_info.channels)), 2)

    def _next_mono_chunk(self, timeout: float = 0.1) -> np.ndarray | None:
        try:
            chunk = self._queue.get(timeout=timeout)
            return resample_float_to_16k_mono(chunk, self._sample_rate)
        except queue.Empty:
            return None

    def start(self) -> None:
        blocksize = max(256, int(self._sample_rate * 0.1))

        def _callback(indata, frames, time_info, status):
            if not self._stop_event.is_set() and indata is not None and indata.size > 0:
                copied = indata.copy()
                try:
                    self._queue.put_nowait(copied)
                except queue.Full:
                    try:
                        self._queue.get_nowait()
                        self._queue.put_nowait(copied)
                    except Exception:
                        pass

        open_errors: list[str] = []
        channel_candidates = [self._channels]
        for c in (self._device_info.channels, 1):
            if c >= 1 and c not in channel_candidates:
                channel_candidates.append(c)

        for ch in channel_candidates:
            try:
                self._stream = sd.InputStream(
                    device=self._device_info.index,
                    channels=ch,
                    samplerate=self._sample_rate,
                    dtype="float32",
                    blocksize=blocksize,
                    callback=_callback,
                )
                self._stream.start()
                self._channels = ch
                break
            except Exception as exc:
                open_errors.append(f"{ch}ch: {exc}")
                self._stream = None

        if self._stream is None:
            hint = ""
            if sys.platform == "darwin":
                hint = "\n提示：请在「系统设置 → 隐私与安全性 → 麦克风」中允许终端 / Python 访问麦克风。"
            raise RuntimeError(
                f"无法打开{self._channel_label}设备 [{self._device_info.name}]: "
                + "; ".join(open_errors)
                + hint
            )

        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"AudioVAD-{self._channel_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None


class AudioCaptureManager:
    """Manages one or two audio capture channels (system loopback and/or microphone)."""

    def __init__(
        self,
        on_segment: Callable[[AudioSegment], None],
        on_level: Callable[[str, float], None],
        on_error: Callable[[str], None],
    ) -> None:
        self._on_segment = on_segment
        self._on_level = on_level
        self._on_error = on_error
        self._pa: Any = None
        self._workers: list[Any] = []
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return len(self._workers) > 0

    def _resolve_win_loopback(self, pa: Any, cfg: AppConfig) -> dict:
        loopbacks = list(pa.get_loopback_device_info_generator())
        if not loopbacks:
            raise RuntimeError(
                "未检测到 Windows WASAPI 系统音频回环设备。请检查扬声器/耳机是否已连接并启用。"
            )

        if cfg.system_device_index >= 0:
            for dev in loopbacks:
                if int(dev["index"]) == cfg.system_device_index:
                    if not cfg.system_device_name or dev["name"] == cfg.system_device_name:
                        return dev

        if cfg.system_device_name:
            for dev in loopbacks:
                if dev["name"] == cfg.system_device_name:
                    return dev

        try:
            wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_out = pa.get_device_info_by_index(int(wasapi_info["defaultOutputDevice"]))
            if default_out.get("isLoopbackDevice", False):
                return default_out
            for dev in loopbacks:
                if default_out["name"] in dev["name"]:
                    return dev
        except Exception:
            pass

        return loopbacks[0]

    def _resolve_win_mic(self, pa: Any, cfg: AppConfig) -> dict:
        if cfg.mic_device_index >= 0:
            try:
                dev = pa.get_device_info_by_index(cfg.mic_device_index)
                if (
                    int(dev.get("maxInputChannels", 0)) > 0
                    and not dev.get("isLoopbackDevice", False)
                    and (not cfg.mic_device_name or dev["name"] == cfg.mic_device_name)
                ):
                    return dev
            except Exception:
                pass

        if cfg.mic_device_name:
            wasapi_idx = -1
            try:
                wasapi_idx = int(pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"])
            except Exception:
                pass
            fallback_match = None
            for i in range(pa.get_device_count()):
                try:
                    dev = pa.get_device_info_by_index(i)
                except Exception:
                    continue
                if (
                    int(dev.get("maxInputChannels", 0)) > 0
                    and not dev.get("isLoopbackDevice", False)
                    and dev.get("name") == cfg.mic_device_name
                ):
                    if int(dev.get("hostApi", -1)) == wasapi_idx:
                        return dev
                    fallback_match = dev
            if fallback_match is not None:
                return fallback_match

        try:
            wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_in_idx = int(wasapi_info.get("defaultInputDevice", -1))
            if default_in_idx >= 0:
                dev = pa.get_device_info_by_index(default_in_idx)
                if int(dev.get("maxInputChannels", 0)) > 0:
                    return dev
        except Exception:
            pass

        try:
            return pa.get_default_input_device_info()
        except Exception as exc:
            raise RuntimeError(f"未检测到可用麦克风设备，请检查 Windows 麦克风权限和连接: {exc}")

    def _resolve_sd_loopback(self, cfg: AppConfig) -> AudioDeviceInfo:
        loopbacks, _ = _list_audio_devices_sounddevice()
        if cfg.system_device_index >= 0:
            for dev in loopbacks:
                if dev.index == cfg.system_device_index:
                    if not cfg.system_device_name or dev.name == cfg.system_device_name:
                        return dev
        if cfg.system_device_name:
            for dev in loopbacks:
                if dev.name == cfg.system_device_name:
                    return dev

        for dev in loopbacks:
            if _is_virtual_loopback_name(dev.name):
                return dev

        if sys.platform == "darwin":
            raise RuntimeError(
                "macOS 抓取「系统声音」需要先安装免费开源虚拟声卡 BlackHole 2ch：\n"
                "1. 终端运行：brew install blackhole-2ch\n"
                "2. 打开「音频 MIDI 设置」新建「多输出设备」（勾选扬声器/耳机 + BlackHole 2ch）并设为系统输出。\n\n"
                "提示：线下「🎤 麦克风」收音无需安装虚拟声卡，直接点亮即可使用！"
            )
        raise RuntimeError(
            "未检测到系统音频回环设备。请在「设置 → 音频与检测」中手动选择虚拟回环输入设备。"
        )

    def _resolve_sd_mic(self, cfg: AppConfig) -> AudioDeviceInfo:
        _, mics = _list_audio_devices_sounddevice()
        if not mics:
            raise RuntimeError("未检测到可用麦克风设备，请检查系统麦克风权限与设备连接。")

        if cfg.mic_device_index >= 0:
            for dev in mics:
                if dev.index == cfg.mic_device_index:
                    if not cfg.mic_device_name or dev.name == cfg.mic_device_name:
                        return dev
        if cfg.mic_device_name:
            for dev in mics:
                if dev.name == cfg.mic_device_name:
                    return dev

        for dev in mics:
            if dev.is_default:
                return dev
        return mics[0]

    def start(self, cfg: AppConfig) -> list[str]:
        """Start capturing according to cfg.audio_mode. Returns active device descriptions."""
        self.stop()
        with self._lock:
            active_descriptions: list[str] = []
            started_workers: list[Any] = []

            if IS_WINDOWS:
                pa = pyaudio.PyAudio()
                self._pa = pa
                try:
                    if cfg.audio_mode in ("system", "both"):
                        sys_dev = self._resolve_win_loopback(pa, cfg)
                        worker = _WindowsCaptureWorker(
                            pa=pa,
                            device_info=sys_dev,
                            channel_id="system",
                            channel_label="系统",
                            cfg=cfg,
                            on_segment=self._on_segment,
                            on_level=self._on_level,
                            on_error=self._on_error,
                        )
                        worker.start()
                        started_workers.append(worker)
                        active_descriptions.append(f"系统: {sys_dev['name']}")

                    if cfg.audio_mode in ("mic", "both"):
                        mic_dev = self._resolve_win_mic(pa, cfg)
                        worker = _WindowsCaptureWorker(
                            pa=pa,
                            device_info=mic_dev,
                            channel_id="mic",
                            channel_label="麦克风",
                            cfg=cfg,
                            on_segment=self._on_segment,
                            on_level=self._on_level,
                            on_error=self._on_error,
                        )
                        worker.start()
                        started_workers.append(worker)
                        active_descriptions.append(f"麦克风: {mic_dev['name']}")

                    self._workers = started_workers
                    return active_descriptions
                except Exception:
                    for w in started_workers:
                        w.stop()
                    pa.terminate()
                    self._pa = None
                    self._workers = []
                    raise
            else:
                try:
                    if cfg.audio_mode in ("system", "both"):
                        sys_dev = self._resolve_sd_loopback(cfg)
                        worker = _SoundDeviceCaptureWorker(
                            device_info=sys_dev,
                            channel_id="system",
                            channel_label="系统",
                            cfg=cfg,
                            on_segment=self._on_segment,
                            on_level=self._on_level,
                            on_error=self._on_error,
                        )
                        worker.start()
                        started_workers.append(worker)
                        active_descriptions.append(f"系统: {sys_dev.name}")

                    if cfg.audio_mode in ("mic", "both"):
                        mic_dev = self._resolve_sd_mic(cfg)
                        worker = _SoundDeviceCaptureWorker(
                            device_info=mic_dev,
                            channel_id="mic",
                            channel_label="麦克风",
                            cfg=cfg,
                            on_segment=self._on_segment,
                            on_level=self._on_level,
                            on_error=self._on_error,
                        )
                        worker.start()
                        started_workers.append(worker)
                        active_descriptions.append(f"麦克风: {mic_dev.name}")

                    self._workers = started_workers
                    return active_descriptions
                except Exception:
                    for w in started_workers:
                        w.stop()
                    self._workers = []
                    raise

    def stop(self) -> None:
        with self._lock:
            workers = self._workers
            self._workers = []
            pa = self._pa
            self._pa = None

        for w in workers:
            try:
                w.stop()
            except Exception:
                pass
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass
