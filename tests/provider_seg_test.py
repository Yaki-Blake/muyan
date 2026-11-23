"""Provider 层验证：假 ws 灌服务端消息，确认「提前断句」不会重复、不会吞字。"""
import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from muyan.config import Settings
from muyan.asr.unisound_realtime import UnisoundRealtimeASR

FAIL = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(name)


app = QApplication.instance() or QApplication([])


class FakeWS:
    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def send_binary(self, pcm):
        pass

    def close(self):
        pass


def make(show_partial=True, fast_break=True, fast_ms=320):
    s = Settings()
    s.api_key = "tp-test"
    s.show_partial = show_partial
    s.fast_break = fast_break
    s.fast_break_ms = fast_ms
    p = UnisoundRealtimeASR(s)
    finals, partials = [], []
    p.final.connect(finals.append)
    p.partial.connect(partials.append)
    p._alive.set()
    p._ws = FakeWS()
    p._start_session()
    return p, finals, partials


# 1) start 帧：即使不显示半句，也必须 variable=true（提前断句要靠累积文本）
p, finals, partials = make(show_partial=False)
frame = p._ws.sent[0]
check("start 帧 variable=true", frame["data"]["variable"] == "true", json.dumps(frame["data"], ensure_ascii=False))
check("start 帧 punctuation/post_proc 打开", frame["data"]["punctuation"] == "true" and frame["data"]["post_proc"] == "true")

# 2) 逐字吐到句号 -> 停稳后立刻出定稿，不必等 fixed
for txt in ["今天", "今天我们讨论一下", "今天我们讨论一下第三季度的财务报表。"]:
    p._on_event({"type": "variable", "text": txt, "end": False})
    time.sleep(0.04)
check("吐字过程中没有提前定稿", finals == [], str(finals))
time.sleep(0.30)
p.feed(b"\x00\x00" * 1600)          # 一块静音音频，等价于喂一次 tick
check("停稳后不用等服务端就出定稿", finals == ["今天我们讨论一下第三季度的财务报表。"], str(finals))
check("不显示半句时一条 partial 都不发", partials == [], str(partials))

# 3) 服务端 fixed 晚到：只补没输出的部分，不重复
p._on_event({"type": "fixed", "text": "今天我们讨论一下第三季度的财务报表。", "end": False})
check("fixed 重复内容被丢掉", finals == ["今天我们讨论一下第三季度的财务报表。"], str(finals))

# 4) 长句没标点：硬切加逗号，随后 fixed 只补后面
p, finals, partials = make()
long_text = "这个季度我们的财务报表显示收入同比下降了百分之十二而成本还在往上走"
p._on_event({"type": "variable", "text": long_text, "end": False})
time.sleep(0.50)
p.feed(b"\x00\x00" * 1600)
check("长句没有标点也会自动断开", len(finals) >= 1 and finals[0].endswith("，"), str(finals))
p._on_event({"type": "fixed", "text": long_text, "end": False})
joined = "".join(finals).replace("，", "").replace(" ", "")
check("硬切后 fixed 不重复也不吞字", joined == long_text, joined + " != " + long_text)

# 5) 新一轮 end：尾巴交出去，状态清零，重新开 session
p, finals, partials = make()
p._on_event({"type": "variable", "text": "这句话还没说完", "end": False})
p._on_event({"type": "fixed", "text": "这句话还没说完", "end": True})
check("end 时把尾巴交出去", finals == ["这句话还没说完"], str(finals))
check("end 后重开了 session", len(p._ws.sent) >= 2 and p._ws.sent[-1]["type"] == "start", str([f["type"] for f in p._ws.sent]))
check("end 后半句清空", p._seg.tail() == "" and p._seg.consumed == 0)

# 6) 关闭提前断句：只认服务端 fixed（行为退回旧版）
p, finals, partials = make(fast_break=False)
p._on_event({"type": "variable", "text": "这句有标点，但开关关掉了。", "end": False})
time.sleep(0.45)
p.feed(b"\x00\x00" * 1600)
check("关闭时不提前断句", finals == [], str(finals))
p._on_event({"type": "fixed", "text": "这句有标点，但开关关掉了。", "end": False})
check("关闭时 fixed 照常输出", finals == ["这句有标点，但开关关掉了。"], str(finals))

# 7) 重连后文本错位（新会话从空开始）：不能吞掉新字
p, finals, partials = make()
p._on_event({"type": "variable", "text": "上一轮很长的一句话在这里结束。", "end": False})
p._seg.reset()                      # 模拟重连：分段器清空，但已显示过文字
p._on_event({"type": "variable", "text": "重连之后的新一句。", "end": False})
time.sleep(0.30)
p.feed(b"\x00\x00" * 1600)
check("重连后新句正常输出", any("重连之后的新一句。" in x for x in finals), str(finals))

# 8) 定时 tick 的开销：连喂 300 块音频
p, finals, partials = make()
t0 = time.perf_counter()
for i in range(300):
    p._on_event({"type": "variable", "text": f"第{i}句测试文本内容。", "end": False})
    p.feed(b"\x00\x00" * 1600)
dt = (time.perf_counter() - t0) * 1000
check("300 轮事件+喂音频 < 400ms", dt < 400, f"{dt:.1f}ms")

# 9) 显示半句时：尾巴同步、切走后清空
p, finals, partials = make(show_partial=True)
p._on_event({"type": "variable", "text": "半句会先显示出来", "end": False})
check("显示半句时 tail 同步", partials and partials[-1] == "半句会先显示出来", str(partials))
p._on_event({"type": "variable", "text": "半句会先显示出来，", "end": False})
time.sleep(0.40)
p.feed(b"\x00\x00" * 1600)
check("切走后定稿出现", finals == ["半句会先显示出来，"], str(finals))
check("切走后半句清空", partials and partials[-1] == "", str(partials))

print()
print("FAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)
