"""Cloud API speech recognition & real-time meeting translation engines."""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from audio_capture import AudioSegment
from config import AppConfig

# Common Whisper hallucinations on brief background noise or silence
_WHISPER_HALLUCINATIONS = {
    "thank you",
    "thank you.",
    "thanks.",
    "thanks for watching",
    "thanks for watching.",
    "thank you for watching",
    "thank you for watching.",
    "thank you for listening.",
    "please subscribe.",
    "bye.",
    "bye-bye.",
    "you",
    "...",
    "字幕由amara.org社区提供",
    "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目",
    "谢谢观看",
    "谢谢收看",
    "感谢收看",
    "字幕制作",
}


@dataclass
class TranslationResult:
    seq_id: int
    channel_id: str
    channel_label: str
    timestamp: str
    source_lang: str = ""
    original: str = ""
    translation: str = ""
    speech_detected: bool = True
    latency_ms: int = 0
    error: str = ""


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_DOMESTIC_DIRECT_HOSTS = (
    "siliconflow.cn",
    "deepseek.com",
    "dashscope.aliyuncs.com",
    "bigmodel.cn",
    "moonshot.cn",
    "volces.com",
    "xf-yun.com",
    "baidubce.com",
)


def _build_proxies(cfg: AppConfig) -> dict[str, str] | None:
    proxy = cfg.active_proxy()
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    return {"http": proxy, "https": proxy}


def _should_bypass_system_proxy(url: str, proxies: dict[str, str] | None) -> bool:
    """When no explicit proxy is configured for domestic APIs, bypass Windows system proxy."""
    if proxies:
        return False
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith(f".{d}") for d in _DOMESTIC_DIRECT_HOSTS)


def _make_session(url: str, proxies: dict[str, str] | None) -> requests.Session:
    sess = requests.Session()
    if _should_bypass_system_proxy(url, proxies):
        sess.trust_env = False
    return sess


def _post_with_retry(
    url: str,
    *,
    headers: dict[str, str],
    proxies: dict[str, str] | None,
    timeout: int,
    json_body: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
    max_retries: int = 2,
) -> requests.Response:
    """POST with brief automatic backoff retry on transient 429/5xx spikes."""
    delays = (0.8, 1.8, 3.0)
    last_resp: requests.Response | None = None
    with _make_session(url, proxies) as sess:
        for attempt in range(max_retries + 1):
            resp = sess.post(
                url,
                headers=headers,
                json=json_body,
                data=data,
                files=files,
                proxies=proxies,
                timeout=timeout,
            )
            last_resp = resp
            if resp.status_code not in _RETRYABLE_STATUS or attempt >= max_retries:
                return resp
            time.sleep(delays[min(attempt, len(delays) - 1)])
    assert last_resp is not None
    return last_resp


def _direction_instruction(direction: str) -> str:
    mapping = {
        "auto_to_zh": (
            "Auto-detect the spoken language. Transcribe the exact spoken words in `original`, "
            "and translate them into natural, fluent Simplified Chinese in `translation`. "
            "If the speaker is already speaking Chinese, transcribe in Simplified Chinese and "
            "provide a clean, concise Simplified Chinese subtitle in `translation`."
        ),
        "bi_en_zh": (
            "Auto-detect whether the speaker is speaking Chinese or a foreign language (especially English). "
            "If the speaker is speaking English or any non-Chinese language, translate into natural, "
            "fluent Simplified Chinese. "
            "If the speaker is speaking Chinese, translate into natural, professional business English."
        ),
        "en_to_zh": (
            "The speaker is primarily speaking English. Transcribe accurately in English in `original`, "
            "and translate into natural, concise business Simplified Chinese in `translation`."
        ),
        "zh_to_en": (
            "The speaker is primarily speaking Chinese. Transcribe accurately in Chinese in `original`, "
            "and translate into natural, professional business English in `translation`."
        ),
        "auto_to_en": (
            "Auto-detect the spoken language. Transcribe the exact spoken words in `original`, "
            "and translate into natural, professional business English in `translation`."
        ),
    }
    return mapping.get(direction, mapping["auto_to_zh"])


def _extract_json_object(text: str) -> dict:
    """Safely parse JSON even if the model wrapped it in markdown fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    raise ValueError(f"模型返回非标准 JSON: {cleaned[:200]}")


def _is_hallucination(text: str) -> bool:
    norm = re.sub(r"\s+", " ", text.strip().lower())
    if not norm:
        return True
    if norm in _WHISPER_HALLUCINATIONS:
        return True
    # Pure punctuation / symbols
    if not re.search(r"[a-zA-Z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", norm):
        return True
    return False


class TranslationEngine:
    """Thread-safe translation engine supporting Gemini, OpenAI, and OpenAI-compatible APIs."""

    def __init__(self) -> None:
        self._ctx_lock = threading.Lock()
        self._recent_context: deque[tuple[str, str]] = deque(maxlen=6)

    def clear_context(self) -> None:
        with self._ctx_lock:
            self._recent_context.clear()

    def _get_context_block(self, max_turns: int) -> str:
        if max_turns <= 0:
            return ""
        with self._ctx_lock:
            items = list(self._recent_context)[-max_turns:]
        if not items:
            return ""
        lines = ["Recent meeting context (for pronoun & terminology consistency only; DO NOT re-translate):"]
        for orig, trans in items:
            lines.append(f"- {orig} => {trans}")
        return "\n".join(lines)

    def _record_context(self, original: str, translation: str) -> None:
        if not original.strip():
            return
        with self._ctx_lock:
            self._recent_context.append((original.strip(), translation.strip()))

    def _build_system_prompt(self, cfg: AppConfig, for_audio: bool) -> str:
        glossary_block = ""
        if cfg.glossary.strip():
            glossary_block = (
                "\nDomain / Meeting Glossary & Background (prefer these exact terms and spellings):\n"
                f"{cfg.glossary.strip()}\n"
            )

        if for_audio:
            return (
                "You are a real-time simultaneous interpreter for international business meetings.\n"
                f"{_direction_instruction(cfg.direction)}\n"
                f"{glossary_block}"
                "Rules:\n"
                "1. If the audio contains only silence, keyboard noise, coughing, music, or unintelligible noise with no human speech, "
                'return {"speech_detected": false, "source_lang": "", "original": "", "translation": ""}.\n'
                "2. Keep subtitles concise, accurate, and natural for live reading.\n"
                "3. Preserve brand names, technical acronyms, numbers, and currencies accurately.\n"
                "4. Output ONLY a valid JSON object with keys: "
                '`speech_detected` (bool), `source_lang` (short ISO code like "EN", "ZH", "JA", "DE", "FR", "ES"), '
                "`original` (string), `translation` (string)."
            )

        return (
            "You are a real-time simultaneous interpreter for international business meetings.\n"
            f"{_direction_instruction(cfg.direction)}\n"
            f"{glossary_block}"
            "Rules:\n"
            "1. Keep the translation concise, idiomatic, and immediately readable as live subtitles.\n"
            "2. Preserve brand names, technical acronyms, numbers, and currencies accurately.\n"
            '3. Output ONLY a valid JSON object with keys: `source_lang` (short code like "EN", "ZH", "JA"), '
            "`translation` (string)."
        )

    def translate_segment(self, seq_id: int, segment: AudioSegment, cfg: AppConfig) -> TranslationResult:
        t0 = time.monotonic()
        try:
            if cfg.provider == "gemini":
                res = self._translate_via_gemini(seq_id, segment, cfg)
            elif cfg.provider == "openai_audio":
                res = self._translate_via_openai_audio(seq_id, segment, cfg)
            else:
                res = self._translate_via_openai_pipeline(seq_id, segment, cfg)
            res.latency_ms = int((time.monotonic() - t0) * 1000)
            if res.speech_detected and not res.error and res.original:
                self._record_context(res.original, res.translation)
            return res
        except requests.exceptions.Timeout:
            return TranslationResult(
                seq_id=seq_id,
                channel_id=segment.channel_id,
                channel_label=segment.channel_label,
                timestamp=segment.timestamp,
                error="请求超时，请检查网络连接或代理设置。",
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except requests.exceptions.ProxyError as exc:
            return TranslationResult(
                seq_id=seq_id,
                channel_id=segment.channel_id,
                channel_label=segment.channel_label,
                timestamp=segment.timestamp,
                error=f"代理连接失败，请检查代理地址: {exc}",
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as exc:
            return TranslationResult(
                seq_id=seq_id,
                channel_id=segment.channel_id,
                channel_label=segment.channel_label,
                timestamp=segment.timestamp,
                error=str(exc),
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

    def _translate_via_gemini(
        self, seq_id: int, segment: AudioSegment, cfg: AppConfig
    ) -> TranslationResult:
        api_key = cfg.gemini_api_key.strip()
        if not api_key:
            raise ValueError("尚未配置 Gemini API Key，请点击右上角 ⚙ 设置填写。")

        base_url = cfg.gemini_base_url.strip().rstrip("/") or "https://generativelanguage.googleapis.com"
        model = cfg.gemini_model.strip() or "gemini-3.8-flash"
        url = f"{base_url}/v1beta/models/{model}:generateContent"

        audio_b64 = base64.b64encode(segment.wav_bytes).decode("ascii")
        context_block = self._get_context_block(cfg.context_turns)
        user_prompt = (
            f"{context_block}\n\n"
            "Transcribe and translate the attached meeting audio clip now. Return strict JSON."
        ).strip()

        gen_cfg: dict = {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        }
        if model.lower().startswith("gemini-3"):
            gen_cfg["thinkingConfig"] = {"thinkingLevel": "low"}

        payload = {
            "system_instruction": {
                "parts": [{"text": self._build_system_prompt(cfg, for_audio=True)}]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": user_prompt},
                        {
                            "inline_data": {
                                "mime_type": "audio/wav",
                                "data": audio_b64,
                            }
                        },
                    ],
                }
            ],
            "generationConfig": gen_cfg,
        }

        resp = _post_with_retry(
            url,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            json_body=payload,
            proxies=_build_proxies(cfg),
            timeout=cfg.request_timeout,
        )

        # Fallback if a specific model/proxy does not accept thinkingConfig
        if resp.status_code == 400 and "thinking" in resp.text.lower() and "thinkingConfig" in gen_cfg:
            gen_cfg.pop("thinkingConfig", None)
            resp = _post_with_retry(
                url,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                },
                json_body=payload,
                proxies=_build_proxies(cfg),
                timeout=cfg.request_timeout,
            )

        if resp.status_code != 200:
            detail = _extract_http_error(resp)
            raise RuntimeError(f"Gemini API 错误 ({resp.status_code}): {detail}")

        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini 未返回内容（可能被安全过滤或网络异常）。")

        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(
            p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought")
        )
        parsed = _extract_json_object(text)

        speech_detected = bool(parsed.get("speech_detected", True))
        original = str(parsed.get("original", "")).strip()
        translation = str(parsed.get("translation", "")).strip()
        source_lang = str(parsed.get("source_lang", "")).strip().upper()[:8]

        if not speech_detected or _is_hallucination(original):
            return TranslationResult(
                seq_id=seq_id,
                channel_id=segment.channel_id,
                channel_label=segment.channel_label,
                timestamp=segment.timestamp,
                speech_detected=False,
            )

        return TranslationResult(
            seq_id=seq_id,
            channel_id=segment.channel_id,
            channel_label=segment.channel_label,
            timestamp=segment.timestamp,
            source_lang=source_lang or "AUTO",
            original=original,
            translation=translation or original,
            speech_detected=True,
        )

    def _translate_via_openai_audio(
        self, seq_id: int, segment: AudioSegment, cfg: AppConfig
    ) -> TranslationResult:
        api_key = cfg.openai_api_key.strip()
        if not api_key:
            raise ValueError("尚未配置 OpenAI API Key，请点击右上角 ⚙ 设置填写。")

        base_url = cfg.openai_base_url.strip().rstrip("/") or "https://api.openai.com/v1"
        model = cfg.openai_audio_model.strip() or "gpt-4o-mini-audio-preview"
        url = f"{base_url}/chat/completions"

        audio_b64 = base64.b64encode(segment.wav_bytes).decode("ascii")
        context_block = self._get_context_block(cfg.context_turns)
        user_text = (
            f"{context_block}\n\n"
            "Transcribe and translate the attached meeting audio clip. Return ONLY valid JSON."
        ).strip()

        payload = {
            "model": model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": self._build_system_prompt(cfg, for_audio=True)},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": audio_b64,
                                "format": "wav",
                            },
                        },
                    ],
                },
            ],
        }

        resp = _post_with_retry(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json_body=payload,
            proxies=_build_proxies(cfg),
            timeout=cfg.request_timeout,
        )

        if resp.status_code != 200:
            detail = _extract_http_error(resp)
            raise RuntimeError(f"OpenAI Audio API 错误 ({resp.status_code}): {detail}")

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = _extract_json_object(content)

        speech_detected = bool(parsed.get("speech_detected", True))
        original = str(parsed.get("original", "")).strip()
        translation = str(parsed.get("translation", "")).strip()
        source_lang = str(parsed.get("source_lang", "")).strip().upper()[:8]

        if not speech_detected or _is_hallucination(original):
            return TranslationResult(
                seq_id=seq_id,
                channel_id=segment.channel_id,
                channel_label=segment.channel_label,
                timestamp=segment.timestamp,
                speech_detected=False,
            )

        return TranslationResult(
            seq_id=seq_id,
            channel_id=segment.channel_id,
            channel_label=segment.channel_label,
            timestamp=segment.timestamp,
            source_lang=source_lang or "AUTO",
            original=original,
            translation=translation or original,
            speech_detected=True,
        )

    def _translate_via_openai_pipeline(
        self, seq_id: int, segment: AudioSegment, cfg: AppConfig
    ) -> TranslationResult:
        stt_key = cfg.openai_api_key.strip()
        if not stt_key:
            raise ValueError("尚未配置 OpenAI / 兼容接口 API Key，请点击右上角 ⚙ 设置填写。")

        stt_base = cfg.openai_base_url.strip().rstrip("/") or "https://api.openai.com/v1"
        stt_model = cfg.openai_stt_model.strip() or "gpt-4o-mini-transcribe"
        proxies = _build_proxies(cfg)

        # Step 1: Speech-to-Text (/audio/transcriptions)
        stt_url = f"{stt_base}/audio/transcriptions"
        form_data: dict[str, str] = {
            "model": stt_model,
            "response_format": "json",
        }
        if cfg.direction == "en_to_zh":
            form_data["language"] = "en"
        elif cfg.direction == "zh_to_en":
            form_data["language"] = "zh"
        if cfg.glossary.strip():
            form_data["prompt"] = cfg.glossary.strip()[:300]

        stt_resp = _post_with_retry(
            stt_url,
            headers={"Authorization": f"Bearer {stt_key}"},
            data=form_data,
            files={"file": ("audio.wav", segment.wav_bytes, "audio/wav")},
            proxies=proxies,
            timeout=cfg.request_timeout,
        )

        if stt_resp.status_code != 200:
            detail = _extract_http_error(stt_resp)
            raise RuntimeError(f"语音识别 API 错误 ({stt_resp.status_code}): {detail}")

        stt_json = stt_resp.json()
        original = str(stt_json.get("text", "")).strip()
        if _is_hallucination(original):
            return TranslationResult(
                seq_id=seq_id,
                channel_id=segment.channel_id,
                channel_label=segment.channel_label,
                timestamp=segment.timestamp,
                speech_detected=False,
            )

        # Step 2: Text Translation (/chat/completions)
        if cfg.use_separate_llm and cfg.separate_llm_api_key.strip():
            llm_key = cfg.separate_llm_api_key.strip()
            llm_base = cfg.separate_llm_base_url.strip().rstrip("/") or "https://api.deepseek.com/v1"
            llm_model = cfg.separate_llm_model.strip() or "deepseek-chat"
        else:
            llm_key = stt_key
            llm_base = stt_base
            llm_model = cfg.openai_llm_model.strip() or "gpt-4o-mini"

        context_block = self._get_context_block(cfg.context_turns)
        user_msg = (
            f"{context_block}\n\n"
            f"Current utterance to translate:\n{original}\n\n"
            "Return ONLY a JSON object with `source_lang` and `translation`."
        ).strip()

        llm_url = f"{llm_base}/chat/completions"
        payload = {
            "model": llm_model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._build_system_prompt(cfg, for_audio=False)},
                {"role": "user", "content": user_msg},
            ],
        }

        llm_resp = _post_with_retry(
            llm_url,
            headers={
                "Authorization": f"Bearer {llm_key}",
                "Content-Type": "application/json",
            },
            json_body=payload,
            proxies=proxies,
            timeout=cfg.request_timeout,
        )

        # Fallback without response_format for providers that don't support json_object
        if llm_resp.status_code == 400 and "response_format" in llm_resp.text.lower():
            payload.pop("response_format", None)
            llm_resp = _post_with_retry(
                llm_url,
                headers={
                    "Authorization": f"Bearer {llm_key}",
                    "Content-Type": "application/json",
                },
                json_body=payload,
                proxies=proxies,
                timeout=cfg.request_timeout,
            )

        if llm_resp.status_code != 200:
            detail = _extract_http_error(llm_resp)
            raise RuntimeError(f"翻译 LLM 错误 ({llm_resp.status_code}): {detail}")

        llm_data = llm_resp.json()
        content = str(llm_data["choices"][0]["message"]["content"]).strip()

        source_lang = "AUTO"
        translation = content
        try:
            parsed = _extract_json_object(content)
            source_lang = str(parsed.get("source_lang", "AUTO")).strip().upper()[:8] or "AUTO"
            translation = str(parsed.get("translation", "")).strip() or original
        except Exception:
            # If the LLM returned plain text translation, still show it gracefully
            translation = content

        return TranslationResult(
            seq_id=seq_id,
            channel_id=segment.channel_id,
            channel_label=segment.channel_label,
            timestamp=segment.timestamp,
            source_lang=source_lang,
            original=original,
            translation=translation,
            speech_detected=True,
        )


def _extract_http_error(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err)
            if err:
                return str(err)
            if "message" in data:
                return str(data["message"])
    except Exception:
        pass
    return resp.text[:240].strip() or resp.reason


def test_api_connection(cfg: AppConfig) -> tuple[bool, str]:
    """Quick connectivity & API key verification for the Settings dialog."""
    proxies = _build_proxies(cfg)
    t0 = time.monotonic()

    try:
        if cfg.provider == "gemini":
            key = cfg.gemini_api_key.strip()
            if not key:
                return False, "请先输入 Gemini API Key。"
            base = cfg.gemini_base_url.strip().rstrip("/") or "https://generativelanguage.googleapis.com"
            model = cfg.gemini_model.strip() or "gemini-3.8-flash"
            url = f"{base}/v1beta/models/{model}:generateContent"
            test_gen_cfg: dict = {"maxOutputTokens": 64}
            if model.lower().startswith("gemini-3"):
                test_gen_cfg["thinkingConfig"] = {"thinkingLevel": "low"}
            test_payload = {
                "contents": [{"role": "user", "parts": [{"text": "Reply OK"}]}],
                "generationConfig": test_gen_cfg,
            }
            req_timeout = max(20, int(cfg.request_timeout))
            resp = requests.post(
                url,
                headers={"Content-Type": "application/json", "x-goog-api-key": key},
                json=test_payload,
                proxies=proxies,
                timeout=req_timeout,
            )
            if resp.status_code == 400 and "thinking" in resp.text.lower() and "thinkingConfig" in test_gen_cfg:
                test_gen_cfg.pop("thinkingConfig", None)
                resp = requests.post(
                    url,
                    headers={"Content-Type": "application/json", "x-goog-api-key": key},
                    json=test_payload,
                    proxies=proxies,
                    timeout=req_timeout,
                )
            ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code == 200:
                return True, f"Gemini 连接成功！（模型: {model}，延迟: {ms}ms）"
            return False, f"Gemini 连接失败 ({resp.status_code}): {_extract_http_error(resp)}"

        # OpenAI / OpenAI-compatible / OpenAI Audio
        key = cfg.openai_api_key.strip()
        if not key:
            return False, "请先输入 API Key。"
        base = cfg.openai_base_url.strip().rstrip("/") or "https://api.openai.com/v1"
        url = f"{base}/models"
        with _make_session(url, proxies) as sess:
            resp = sess.get(
                url,
                headers={"Authorization": f"Bearer {key}"},
                proxies=proxies,
                timeout=15,
            )
        ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code == 200:
            return True, f"接口连接成功！（延迟: {ms}ms）"
        return False, f"接口连接失败 ({resp.status_code}): {_extract_http_error(resp)}"
    except requests.exceptions.ProxyError as exc:
        return False, f"代理连接失败，请检查代理地址和端口: {exc}"
    except requests.exceptions.Timeout:
        return False, "连接超时，请检查网络或配置代理（如 http://127.0.0.1:7890）。"
    except Exception as exc:
        return False, f"连接异常: {exc}"
