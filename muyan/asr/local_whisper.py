from __future__ import annotations

import io

from .chunked import ChunkedASR

# faster-whisper 接受的语言码（ISO 639-1）
_SUPPORTED_LANGS = {
    "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs", "ca",
    "cs", "cy", "da", "de", "el", "en", "es", "et", "eu", "fa", "fi", "fo", "fr",
    "gl", "gu", "ha", "haw", "he", "hi", "hr", "ht", "hu", "hy", "id", "is", "it",
    "ja", "jw", "ka", "kk", "km", "kn", "ko", "la", "lb", "ln", "lo", "lt", "lv",
    "mg", "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my", "ne", "nl", "nn", "no",
    "oc", "pa", "pl", "ps", "pt", "ro", "ru", "sa", "sd", "si", "sk", "sl", "sn",
    "so", "sq", "sr", "su", "sv", "sw", "ta", "te", "tg", "th", "tk", "tl", "tr",
    "tt", "uk", "ur", "uz", "vi", "yi", "yo", "zh", "yue",
}

# whisper 在中文上会随机输出繁体，统一转简体（可选依赖，没有就跳过）
try:
    import opencc

    _T2S = opencc.OpenCC("t2s")
except Exception:  # pragma: no cover
    _T2S = None


def _to_simplified(text: str) -> str:
    if not text or _T2S is None:
        return text
    try:
        return _T2S.convert(text)
    except Exception:
        return text


class LocalWhisperASR(ChunkedASR):
    """本地 faster-whisper，离线、不要 Key。"""

    def __init__(self, settings, parent=None):
        super().__init__(settings, parent)
        self._model = None

    def start(self):
        try:
            from faster_whisper import WhisperModel  # noqa: F401
        except Exception:
            self.error.emit(
                "本地模式需要先安装：pip install faster-whisper（首次会下载模型）"
            )
            return
        self.status.emit("正在加载本地模型…")
        try:
            from faster_whisper import WhisperModel

            size = self.settings.resolved_model() or "small"
            self._model = WhisperModel(size, device="cpu", compute_type="int8")
        except Exception as e:
            self.error.emit(f"加载模型失败：{e}")
            return
        super().start()

    def transcribe(self, wav: bytes) -> str:
        if self._model is None:
            return ""
        lang = (self.settings.language or "").strip()
        # faster-whisper 只认 ISO 639-1（zh / en / ja），不认 zh-CN 这种带地区的写法
        if lang:
            lang = lang.split("-")[0].split("_")[0].lower()
            if lang not in _SUPPORTED_LANGS:
                lang = ""
        segs, _ = self._model.transcribe(
            io.BytesIO(wav),
            language=(lang or None),
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        text = "".join(s.text for s in segs).strip()
        if lang in ("zh", "yue") or (not lang and any("\u4e00" <= c <= "\u9fff" for c in text)):
            text = _to_simplified(text)
        return text
