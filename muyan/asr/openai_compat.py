from __future__ import annotations

import requests

from .chunked import ChunkedASR


class OpenAICompatASR(ChunkedASR):
    """走 OpenAI 兼容的 /audio/transcriptions。硅基流动、百炼兼容模式、智谱都吃这套。"""

    def __init__(self, settings, parent=None):
        super().__init__(settings, parent)
        self._session = requests.Session()

    def start(self):
        if not self._require_key():
            return
        base = self.settings.resolved_base_url().rstrip("/")
        if not base:
            self.error.emit("这个服务商需要填 Base URL。")
            return
        self._url = f"{base}/audio/transcriptions"
        self._headers = {"Authorization": f"Bearer {self.settings.api_key.strip()}"}
        super().start()

    def transcribe(self, wav: bytes) -> str:
        files = {"file": ("chunk.wav", wav, "audio/wav")}
        data = {"model": self.settings.resolved_model()}
        if self.settings.language:
            data["language"] = self.settings.language
        r = self._session.post(
            self._url, headers=self._headers, files=files, data=data, timeout=45
        )
        if r.status_code == 401:
            raise RuntimeError("API Key 无效（401）")
        if r.status_code == 402 or r.status_code == 403:
            raise RuntimeError(f"额度或权限问题（{r.status_code}）：{r.text[:120]}")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
        try:
            j = r.json()
        except Exception:
            return r.text.strip()
        return (j.get("text") or j.get("transcription") or "").strip()
