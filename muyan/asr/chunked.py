from __future__ import annotations

import queue
import threading
import time

import numpy as np

from .base import ASRProvider, merge_transcript, pcm_to_wav

SR = 16000


class ChunkedASR(ASRProvider):
    """把连续 PCM 切成带重叠的窗口，顺序送识别，再把结果去重拼接。

    子类只需实现 transcribe(wav_bytes) -> str。
    """

    #: 低于这个 RMS 视为静音，直接跳过（省钱、省算力）
    silence_rms = 90

    def __init__(self, settings, parent=None):
        super().__init__(settings, parent)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._q: "queue.Queue[bytes]" = queue.Queue(maxsize=4)
        self._worker = None
        self._committed = ""
        self._dropped = 0

    # ---- 子类实现 ----
    def transcribe(self, wav: bytes) -> str:
        raise NotImplementedError

    # ---- 生命周期 ----
    def start(self):
        self._committed = ""
        self._buf.clear()
        self._dropped = 0
        self._running = True
        self._worker = threading.Thread(target=self._loop, daemon=True, name="ASRWorker")
        self._worker.start()
        self.status.emit("正在识别")

    def stop(self):
        self._running = False
        try:
            self._q.put_nowait(b"")
        except Exception:
            pass
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=3.0)
        self._worker = None

    # ---- 音频入口 ----
    def feed(self, pcm: bytes):
        if not self._running:
            return
        chunk_bytes = int(self.settings.chunk_seconds * SR) * 2
        overlap_bytes = int(self.settings.overlap_seconds * SR) * 2

        with self._lock:
            self._buf.extend(pcm)
            if len(self._buf) < chunk_bytes:
                return
            window = bytes(self._buf[:chunk_bytes])
            # 保留尾部 overlap，避免切断词
            keep = overlap_bytes if overlap_bytes < chunk_bytes else 0
            self._buf = bytearray(self._buf[chunk_bytes - keep:])

        if self._is_silent(window):
            return
        try:
            self._q.put_nowait(window)
        except queue.Full:
            self._dropped += 1
            if self._dropped in (1, 10, 50):
                self.status.emit(f"识别跟不上，已丢 {self._dropped} 段（可调大分片时长）")

    @staticmethod
    def _is_silent(pcm: bytes) -> bool:
        if len(pcm) < 320:
            return True
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if a.size == 0:
            return True
        return float(np.sqrt(np.mean(a * a))) < ChunkedASR.silence_rms

    # ---- 工作线程 ----
    def _loop(self):
        while self._running or not self._q.empty():
            try:
                pcm = self._q.get(timeout=0.3)
            except queue.Empty:
                continue
            if not pcm:
                continue
            try:
                text = self.transcribe(pcm_to_wav(pcm, SR))
            except Exception as e:
                # 单片失败不该把整条链路停掉：提示一下，继续下一片
                self.warn.emit(f"识别出错：{e}")
                time.sleep(0.3)
                continue
            text = (text or "").strip()
            if not text:
                continue
            add = merge_transcript(self._committed, text)
            if not add:
                continue
            self._committed = (self._committed + add)[-400:]
            self.final.emit(add)
