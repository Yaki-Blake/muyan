from __future__ import annotations

import random
import threading
import time

import numpy as np

from .base import ASRProvider

SR = 16000

SAMPLES = [
    "这是一条测试字幕，用来确认悬浮窗显示正常。",
    "系统声音已经抓到了，音量条在动就说明环回成功。",
    "换成真实服务商之后，这里显示的就是识别出来的原话了。",
    "面板会一直往下累积，想回看就往上滚一点。",
    "Ctrl 加滚轮调字号，Shift 加滚轮缩放整个面板。",
]


class MockASR(ASRProvider):
    """不联网。每 2.5 秒吐一句假字幕，用来验证音频链路和界面。"""

    def __init__(self, settings, parent=None):
        super().__init__(settings, parent)
        self._thread = None
        self._i = 0

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="MockASR")
        self._thread.start()
        self.status.emit("测试模式运行中（不联网）")

    def _loop(self):
        while self._running:
            time.sleep(2.5)
            if not self._running:
                break
            self.final.emit(SAMPLES[self._i % len(SAMPLES)])
            self._i += 1

    def feed(self, pcm: bytes):
        return

    def stop(self):
        self._running = False
