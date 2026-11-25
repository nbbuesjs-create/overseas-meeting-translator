"""Configuration management for Meeting Subtitle Translator."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
TRANSCRIPTS_DIR = APP_DIR / "transcripts"


PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "gemini": {
        "label": "Google Gemini API（推荐 · 单步直出低延迟）",
        "base_url": "https://generativelanguage.googleapis.com",
        "model": "gemini-3.8-flash",
    },
    "openai": {
        "label": "OpenAI / 兼容接口（Whisper 转录 + LLM 翻译）",
        "base_url": "https://api.openai.com/v1",
        "stt_model": "gpt-4o-mini-transcribe",
        "llm_model": "gpt-4o-mini",
    },
    "openai_audio": {
        "label": "OpenAI Audio 单步直出（gpt-4o-mini-audio-preview）",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini-audio-preview",
    },
}

COMPAT_QUICK_PRESETS: dict[str, dict[str, str]] = {
    "OpenAI 官方": {
        "base_url": "https://api.openai.com/v1",
        "stt_model": "gpt-4o-mini-transcribe",
        "llm_model": "gpt-4o-mini",
    },
    "Groq 超快引擎（Whisper + Llama）": {
        "base_url": "https://api.groq.com/openai/v1",
        "stt_model": "whisper-large-v3-turbo",
        "llm_model": "llama-3.3-70b-versatile",
    },
    "硅基流动 SiliconFlow（国内直连）": {
        "base_url": "https://api.siliconflow.cn/v1",
        "stt_model": "FunAudioLLM/SenseVoiceSmall",
        "llm_model": "Qwen/Qwen2.5-7B-Instruct",
    },
}

TRANSLATION_DIRECTIONS: list[tuple[str, str]] = [
    ("auto_to_zh", "自动检测多语种 → 中文"),
    ("bi_en_zh", "中英双向自动互译（英⇄中）"),
    ("en_to_zh", "英语 → 中文"),
    ("zh_to_en", "中文 → 英语"),
    ("auto_to_en", "自动检测多语种 → 英语"),
]

AUDIO_MODES: list[tuple[str, str]] = [
    ("system", "仅系统声音（线上会议对方）"),
    ("mic", "仅麦克风（线下会议/环境人声）"),
    ("both", "系统声音 + 麦克风（双通道）"),
]


@dataclass
class AppConfig:
    # Provider: "gemini" | "openai" | "openai_audio"
    provider: str = "gemini"

    # Gemini settings
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    gemini_model: str = "gemini-3.8-flash"
    gemini_http_proxy: str = ""

    # OpenAI / OpenAI-compatible settings
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_stt_model: str = "gpt-4o-mini-transcribe"
    openai_llm_model: str = "gpt-4o-mini"
    openai_audio_model: str = "gpt-4o-mini-audio-preview"
    openai_http_proxy: str = ""

    # Optional separate LLM endpoint (e.g. Groq Whisper STT + DeepSeek LLM)
    use_separate_llm: bool = False
    separate_llm_api_key: str = ""
    separate_llm_base_url: str = "https://api.deepseek.com/v1"
    separate_llm_model: str = "deepseek-chat"

    # Legacy shared proxy (kept for backwards compatibility) & timeout
    http_proxy: str = ""
    request_timeout: int = 25

    # Audio capture settings
    audio_mode: str = "system"  # "system" | "mic" | "both"
    system_device_index: int = -1  # -1 = auto default WASAPI loopback
    system_device_name: str = ""
    mic_device_index: int = -1  # -1 = auto default microphone
    mic_device_name: str = ""

    # VAD & chunking parameters
    vad_threshold: float = 0.012
    silence_duration: float = 0.8
    min_speech_duration: float = 0.45
    max_chunk_duration: float = 7.0

    # Translation & context
    direction: str = "auto_to_zh"
    glossary: str = ""
    context_turns: int = 4

    # UI preferences
    show_original: bool = True
    font_size: int = 16
    window_opacity: float = 0.88
    max_visible_items: int = 40
    window_x: int = -1
    window_y: int = -1
    window_width: int = 860
    window_height: int = 260

    def active_proxy(self) -> str:
        """Return the independent HTTP proxy configured for the active provider."""
        if self.provider == "gemini":
            return self.gemini_http_proxy.strip()
        return self.openai_http_proxy.strip()

    def openai_brand_label(self) -> str:
        """Short display name for the configured OpenAI-compatible endpoint."""
        url = self.openai_base_url.lower()
        if "siliconflow" in url:
            return "硅基流动"
        if "groq" in url:
            return "Groq"
        if "deepseek" in url:
            return "DeepSeek"
        if "openai.com" in url:
            return "OpenAI"
        return "兼容接口"

    def engine_quick_label(self, provider: str | None = None) -> str:
        """Compact label for the floating bar 1-click engine switcher."""
        p = provider or self.provider
        if p == "gemini":
            net = "代理" if self.gemini_http_proxy.strip() else "直连"
            return f"🌐 Gemini·{net}"
        if p == "openai_audio":
            net = "代理" if self.openai_http_proxy.strip() else "直连"
            return f"🎧 Audio·{net}"
        brand = self.openai_brand_label()
        net = "代理" if self.openai_http_proxy.strip() else "直连"
        icon = "🇨🇳" if brand == "硅基流动" else ("⚡" if brand == "Groq" else "🤖")
        return f"{icon} {brand}·{net}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        cfg = cls(**filtered)
        # Migrate legacy single http_proxy cleanly to per-engine proxy fields
        if "gemini_http_proxy" not in data and cfg.http_proxy:
            cfg.gemini_http_proxy = cfg.http_proxy
        if "openai_http_proxy" not in data and cfg.http_proxy and cfg.openai_api_key:
            if "siliconflow" not in cfg.openai_base_url.lower():
                cfg.openai_http_proxy = cfg.http_proxy
        return cfg

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "AppConfig":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return cls.from_dict(raw)
        except Exception:
            pass
        return cls()
