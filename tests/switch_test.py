"""复现「切换视频后不出字幕」，并验证两条自愈路径。

两个机制（切换播放源后最常见）：
  A. WASAPI 环回句柄跟丢了：还能读到数据，但全是纯数字零 → 采集器要自己重开设备
  B. 音频照常发，服务端却不再回包 → 识别器要 6 秒内重建连接（旧版是 30 秒）
"""
import queue
import sys
import threading
import time
import types

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))

import numpy as np

FAIL = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(name)


# ---------------- A. 采集器：全零音频 → 自己重开 ----------------
class FakeSpeaker:
    name = "扬声器 (Fake)"


class FakeRecorder:
    """第一次录音返回纯零（模拟切视频后句柄失联），重开之后恢复正常。"""

    def __init__(self, state, frames=1600):
        self.state = state
        self.frames = frames

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def record(self, numframes=None):
        time.sleep(0.005)
        self.state["calls"] += 1
        if self.state["silent"]:
            return np.zeros((self.frames, 1), dtype=np.float32)
        return np.full((self.frames, 1), 0.25, dtype=np.float32)


class FakeMic:
    def __init__(self, state):
        self.state = state
        self.name = "扬声器 (Fake)"

    def recorder(self, samplerate=16000, channels=1):
        with self.state["lock"]:
            self.state["opens"] += 1
            # 第二次打开之后才算「切视频之前已经在录」
            if self.state["opens"] >= 2:
                self.state["silent"] = False
        return FakeRecorder(self.state)


def install_fake_soundcard(state):
    mod = types.ModuleType("soundcard")

    def default_speaker():
        return FakeSpeaker()

    def get_microphone(id=None, include_loopback=False):
        return FakeMic(state)

    mod.default_speaker = default_speaker
    mod.get_microphone = get_microphone
    sys.modules["soundcard"] = mod


from muyan.audio import LoopbackCapture  # noqa: E402

state = {"calls": 0, "opens": 0, "silent": True, "lock": threading.Lock()}
install_fake_soundcard(state)

q: "queue.Queue" = queue.Queue(maxsize=400)
cap = LoopbackCapture(q, device_name="", block_ms=100)
cap.ZERO_SECONDS_BEFORE_REOPEN = 0.25
cap.REOPEN_COOLDOWN = 0.05
cap.start()
before = cap.silence_reopens

# 先喂一会儿「全是零」的音频（模拟切视频之后的失联期）
time.sleep(0.5)

# 观测：一旦采集器重开设备（state["opens"] >= 2），后面应该有非零音频进队列
got_sound = False
deadline = time.time() + 3.0
while time.time() < deadline:
    try:
        pcm = q.get(timeout=0.2)
    except queue.Empty:
        continue
    a = np.frombuffer(pcm, dtype=np.int16)
    if a.size and int(np.abs(a).max()) > 1000:
        got_sound = True
        break

check("全零音频触发了采集重开", cap.silence_reopens >= before + 1, f"silence_reopens={cap.silence_reopens} opens={state['opens']}")
check("重开后重新收到真实音频", got_sound, "peak 恢复")
check("采集线程没有死", cap.is_alive())

# 静音期间不要反复重开（armed 机制）
cap._silent_reopen_armed = False
state["silent"] = True
r0 = cap.silence_reopens
time.sleep(0.5)
check("暂停（持续静音）时不再反复重开", cap.silence_reopens == r0, f"{cap.silence_reopens}")
cap.stop()
time.sleep(0.2)


# ---------------- B. 识别器：有声却没回包 → 6 秒内重建 ----------------
import muyan.asr.unisound_realtime as ur
from muyan.config import Settings

p = ur.UnisoundRealtimeASR(Settings())
p._alive.set()


class SilentWS:
    """recv 永远超时，模拟服务端不理会我们。"""

    def __init__(self):
        self.recv_calls = 0

    def recv(self):
        self.recv_calls += 1
        raise TimeoutError("timed out")

    def send(self, s):
        pass

    def send_binary(self, b):
        pass

    def close(self):
        pass


# 喂 60 块「有声」音频，走真实发送线程
p._ws = SilentWS()
p._session_active = True
loud = (np.full(1600, 0.3) * 32767).astype(np.int16).tobytes()
p._sender = threading.Thread(target=p._send_loop, daemon=True)
p._sender.start()
for _ in range(ur.UnisoundRealtimeASR.LOUD_BLOCKS_BEFORE_RECONNECT + 2):
    p.feed(loud)
time.sleep(0.6)
check("有声块计数达到阈值", p._loud_since_msg >= ur.UnisoundRealtimeASR.LOUD_BLOCKS_BEFORE_RECONNECT, f"{p._loud_since_msg}")
check("判定为无响应", p._no_response() is True)

# 读循环应该立刻退出（让连接线程去重连）
p._ws = SilentWS()
done = []


def read():
    p._read_loop(p._ws)
    done.append(1)


t = threading.Thread(target=read, daemon=True)
t.start()
t.join(timeout=3.0)
check("读循环在无响应时退出（触发重连）", bool(done), f"done={bool(done)}")

# 对照：只是安静（没有有声块）时不该退出
p._alive.set()
p._loud_since_msg = 0
p._last_msg_at = time.monotonic()
p._sent_since_msg = 0
t2 = threading.Thread(target=lambda: p._read_loop(SilentWS()), daemon=True)
t2.start()
time.sleep(0.8)
check("安静时不会误判为断线", t2.is_alive())
p._alive.clear()
time.sleep(0.2)

# ---------------- C. 音频源切换 → 主动换连接 ----------------
class ClosableWS(SilentWS):
    def __init__(self):
        super().__init__()
        self.closed = False

    def close(self):
        self.closed = True


ws = ClosableWS()
p._ws = ws
p._session_active = True
p._last_partial = "半句还在"
p._loud_since_msg = 7
p.restart_connection("音频源切换")
check("restart_connection 关掉了旧连接", ws.closed)
check("会话标记复位", p._session_active is False)
check("半句清掉、计数清零", p._last_partial == "" and p._loud_since_msg == 0)
check("分段器清空", p._seg.tail() == "" and p._seg.consumed == 0)

print()
print("FAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)
