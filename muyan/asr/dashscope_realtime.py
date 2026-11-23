from __future__ import annotations

import json
import threading
import time
import uuid

import numpy as np

from .base import ASRProvider

SR = 16000


class DashScopeRealtimeASR(ASRProvider):
    """阿里云百炼 Paraformer 实时流式（WebSocket 双向）。

    协议：run-task -> task-started -> 持续推二进制 PCM -> result-generated -> finish-task
    注意：这条链路需要真实 Key 才能验证，首次使用请先用「测试模式」确认音频链路。
    """

    def __init__(self, settings, parent=None):
        super().__init__(settings, parent)
        self._ws = None
        self._thread = None
        self._task_id = ""
        self._sentences = ""
        self._err_count = 0

    def start(self):
        if not self._require_key():
            return
        self._running = True
        self._sentences = ""
        self._thread = threading.Thread(target=self._run, daemon=True, name="DSRealtime")
        self._thread.start()

    def _run(self):
        try:
            import websocket
        except Exception as e:
            self.error.emit(f"缺少 websocket-client：{e}")
            self._running = False
            return

        url = self.settings.resolved_base_url().rstrip("/")
        self._task_id = uuid.uuid4().hex
        headers = [f"Authorization: bearer {self.settings.api_key.strip()}"]

        try:
            self._ws = websocket.create_connection(
                url, header=headers, timeout=15, enable_multithread=True
            )
        except Exception as e:
            self.error.emit(f"连接百炼失败：{e}")
            self._running = False
            return

        run_task = {
            "header": {"action": "run-task", "task_id": self._task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.settings.resolved_model(),
                "parameters": {
                    "format": "pcm",
                    "sample_rate": SR,
                    "language_hints": [self.settings.language or "zh"],
                },
                "input": {},
            },
        }
        self._ws.send(json.dumps(run_task))
        self.status.emit("已连接百炼，等待 task-started")

        reader = threading.Thread(target=self._read_loop, daemon=True, name="DSReader")
        reader.start()

    def _read_loop(self):
        try:
            while self._running and self._ws:
                msg = self._ws.recv()
                if not msg:
                    continue
                if isinstance(msg, bytes):
                    continue
                try:
                    evt = json.loads(msg)
                except Exception:
                    continue
                header = evt.get("header", {})
                name = header.get("event", "")
                if name == "task-started":
                    self.status.emit("正在识别（流式）")
                elif name == "result-generated":
                    self._on_result(evt)
                elif name == "task-finished":
                    self.status.emit("识别已结束")
                    break
                elif name == "task-failed":
                    self.error.emit(f"识别失败：{header.get('error_message', '未知错误')}")
                    break
        except Exception as e:
            if self._running:
                self.error.emit(f"流式连接中断：{e}")
        finally:
            self._running = False

    def _on_result(self, evt: dict):
        out = evt.get("payload", {}).get("output", {}) or {}
        sentence = out.get("sentence") or {}
        text = (sentence.get("text") or "").strip()
        if not text:
            return
        if sentence.get("sentence_end"):
            self._sentences = (self._sentences + text)[-400:]
            self.final.emit(text)
        elif self.settings.show_partial:
            self.partial.emit(text)

    def feed(self, pcm: bytes):
        if not self._running or not self._ws:
            return
        try:
            self._ws.send_binary(pcm)
        except Exception as e:
            self._err_count += 1
            if self._err_count <= 2:
                self.error.emit(f"发送音频失败：{e}")
            self._running = False

    def stop(self):
        self._running = False
        ws = self._ws
        self._ws = None
        if not ws:
            return
        try:
            ws.send(
                json.dumps(
                    {"header": {"action": "finish-task", "task_id": self._task_id}, "payload": {"input": {}}}
                )
            )
            time.sleep(0.2)
        except Exception:
            pass
        try:
            ws.close()
        except Exception:
            pass
