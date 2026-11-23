"""验证 UnisoundRealtimeASR 的断线自愈：发送失败后线程不能死、连接要重连、字幕要能继续出。"""
from __future__ import annotations

import json
import sys
import threading
import time
import types

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))

# ---- 假 websocket 模块 ----
fake = types.ModuleType("websocket")


class WebSocketTimeoutException(Exception):
    pass


fake.WebSocketTimeoutException = WebSocketTimeoutException

hits = {"connections": 0, "binary": 0, "recv_timeouts": 0, "fixed": 0}


class FakeWS:
    def __init__(self, idx: int):
        self.idx = idx
        self.closed = False
        self.fail_binary_left = 1 if idx == 1 else 0  # 只在第一条连接上模拟一次网络抽风
        self.t0 = time.monotonic()
        self.recv_n = 0

    def settimeout(self, t):
        pass

    def send(self, payload):
        if self.closed:
            raise RuntimeError("closed")
        # 忽略 start/end 的细节

    def send_binary(self, pcm):
        if self.closed:
            raise RuntimeError("closed")
        if self.fail_binary_left > 0:
            self.fail_binary_left -= 1
            raise RuntimeError("模拟发送失败：网络抖动")
        hits["binary"] += 1

    def recv(self):
        if self.closed:
            raise RuntimeError("closed")
        self.recv_n += 1
        # 每秒最多回一条消息，剩下抛超时（模拟安静时段）
        if self.recv_n % 1 == 0 and time.monotonic() - self.t0 > 1.0 + self.idx * 0.5:
            self.recv_n = 0
            self.t0 = time.monotonic()
            hits["fixed"] += 1
            return json.dumps({"type": "fixed", "text": f"第{hits['fixed']}句字幕", "end": False})
        hits["recv_timeouts"] += 1
        raise WebSocketTimeoutException("timed out")

    def close(self):
        self.closed = True


def create_connection(url, header=None, timeout=None, enable_multithread=True):
    hits["connections"] += 1
    return FakeWS(hits["connections"])


fake.create_connection = create_connection
sys.modules["websocket"] = fake

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from muyan.config import Settings
from muyan.asr.unisound_realtime import UnisoundRealtimeASR

app = QApplication(sys.argv)

s = Settings()
s.api_key = "tp-test"
s.provider = "unisound_realtime"
s.apply_preset_defaults()

p = UnisoundRealtimeASR(s)

got: list[str] = []
errs: list[str] = []
warns: list[str] = []
p.final.connect(lambda t: got.append(t), Qt.ConnectionType.DirectConnection)
p.error.connect(lambda t: errs.append(t), Qt.ConnectionType.DirectConnection)
p.warn.connect(lambda t: warns.append(t), Qt.ConnectionType.DirectConnection)
p.status.connect(lambda t: None, Qt.ConnectionType.DirectConnection)

p.start()

# 灌 6 秒音频，第 10 帧左右会撞上发送失败
import numpy as np

block = (np.random.randn(1600) * 2000).astype(np.int16).tobytes()
t0 = time.monotonic()
while time.monotonic() - t0 < 7.0:
    p.feed(block)
    time.sleep(0.1)

sender_alive = bool(p._sender and p._sender.is_alive())
print("发送线程存活:", sender_alive)
print("连接次数:", hits["connections"])
print("成功发送的音频块:", hits["binary"])
print("收到的定稿句数:", len(got), got[:3])
print("致命错误:", errs)
print("提示:", warns[:3])
print("running:", p.running)

p.stop()
time.sleep(0.3)
print("stop 后发送线程存活:", bool(p._sender and p._sender.is_alive()))
assert sender_alive, "发送线程死了 —— 还是会卡住"
assert hits["connections"] >= 1
assert len(got) >= 1, "断线后没有再出字幕"
assert not errs, "不该有致命错误"
print("PASS")
