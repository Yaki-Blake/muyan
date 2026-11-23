from __future__ import annotations

import threading
import time

import requests

from .base import ASRProvider, merge_transcript, pcm_to_wav

SR = 16000


class UnisoundAsyncASR(ASRProvider):
    """云知声 Token Hub 异步任务式 ASR。

    实测协议（2026-11-23 验证）：
      POST /v1/files/upload         multipart: file + purpose
      POST /v1/audio/asr/tasks      {model, file_id | file_url, format}
      GET  /v1/audio/asr/tasks/{id} 轮询结果

    重要限制：这是异步批处理接口，不是流式。每段音频要上传 + 排队 + 轮询，
    端到端延迟通常 5-15 秒。做实时字幕体验很差，只在没有别的选择时用。
    """

    POLL_INTERVAL = 0.8
    POLL_TIMEOUT = 30.0

    def __init__(self, settings, parent=None):
        super().__init__(settings, parent)
        self._base = ""
        self._headers = {}
        self._committed = ""
        self._session = requests.Session()
        self._buf = bytearray()
        self._inflight = 0
        self._lock = threading.Lock()

    def start(self):
        if not self._require_key():
            return
        self._base = self.settings.resolved_base_url().rstrip("/")
        self._headers = {"Authorization": f"Bearer {self.settings.api_key.strip()}"}
        self._committed = ""
        self._buf.clear()
        self._inflight = 0
        self._running = True
        self.status.emit("已连接云知声（异步模式，延迟 5-15 秒）")

    def feed(self, pcm: bytes):
        if not self._running:
            return
        need = int(self.settings.chunk_seconds * SR) * 2
        with self._lock:
            self._buf.extend(pcm)
            if len(self._buf) < need:
                return
            window = bytes(self._buf[:need])
            self._buf = bytearray()
            if self._inflight >= 2:  # 别把并发打爆
                return
            self._inflight += 1
        threading.Thread(target=self._process, args=(window,), daemon=True).start()

    def _process(self, pcm: bytes):
        try:
            text = self._transcribe(pcm)
        except Exception as e:
            self.error.emit(f"云知声识别失败：{e}")
            return
        finally:
            with self._lock:
                self._inflight -= 1
        text = (text or "").strip()
        if not text:
            return
        add = merge_transcript(self._committed, text)
        if add:
            self._committed = (self._committed + add)[-400:]
            self.final.emit(add)

    def _transcribe(self, pcm: bytes) -> str:
        fid = self._upload(pcm_to_wav(pcm, SR))
        task_id = self._create_task(fid)
        return self._poll(task_id)

    def _upload(self, wav: bytes) -> str:
        last = ""
        for purpose in ("asr", "audio", "file", "transcription"):
            r = self._session.post(
                f"{self._base}/files/upload",
                headers=self._headers,
                files={"file": ("chunk.wav", wav, "audio/wav")},
                data={"purpose": purpose},
                timeout=30,
            )
            try:
                j = r.json()
            except Exception:
                last = r.text[:160]
                continue
            code = j.get("base_resp", {}).get("status_code", -1)
            if code == 0:
                data = j.get("data") or j
                fid = data.get("id") or data.get("file_id") or ""
                if fid:
                    return fid
            last = j.get("base_resp", {}).get("status_msg", str(j))
        raise RuntimeError(f"上传失败：{last}")

    def _create_task(self, file_id: str) -> str:
        body = {
            "model": self.settings.resolved_model() or "u2-asr",
            "file_id": file_id,
            "format": "wav",
        }
        r = self._session.post(
            f"{self._base}/audio/asr/tasks",
            headers={**self._headers, "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        j = r.json()
        code = j.get("base_resp", {}).get("status_code", -1)
        if code != 0:
            raise RuntimeError(j.get("base_resp", {}).get("status_msg", str(j)))
        data = j.get("data") or j
        tid = data.get("task_id") or data.get("id") or ""
        if not tid:
            raise RuntimeError(f"没有拿到 task_id：{str(j)[:160]}")
        return tid

    def _poll(self, task_id: str) -> str:
        deadline = time.time() + self.POLL_TIMEOUT
        while time.time() < deadline:
            r = self._session.get(
                f"{self._base}/audio/asr/tasks/{task_id}",
                headers=self._headers,
                timeout=20,
            )
            j = r.json()
            data = j.get("data") or j
            status = str(data.get("status") or "").lower()
            if status in ("succeeded", "success", "completed", "done"):
                result = data.get("result") or {}
                return (
                    data.get("text")
                    or result.get("text")
                    or data.get("output", {}).get("text")
                    or ""
                )
            if status in ("failed", "error"):
                raise RuntimeError(data.get("error") or data.get("message") or "任务失败")
            time.sleep(self.POLL_INTERVAL)
        raise RuntimeError("轮询超时")

    def stop(self):
        self._running = False
