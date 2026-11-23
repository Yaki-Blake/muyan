"""提前断句的单元测试：不启动 GUI，只喂假的服务端消息。"""
import sys
import time

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))

from muyan.asr.fastseg import FastSegmenter

FAIL = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(name)


# 1) 收尾标点停稳后立刻切，且不必等 fixed
seg = FastSegmenter(hold_ms=320)
t = 1000.0
out = []
for txt in ["今天", "今天我们", "今天我们讨论一下", "今天我们讨论一下财务报表。"]:
    out += seg.push(txt, t)
    t += 0.08
check("吐字过程中不切", out == [], str(out))
out += seg.push("今天我们讨论一下财务报表。", t)   # 文本没变，只是又过了一拍
t += 0.25
out += seg.tick(t)                                # 0.25s 后
check("停稳 0.25s 仍未切（阈值 0.32*0.6=0.192）", out == ["今天我们讨论一下财务报表。"], str(out))
out += seg.tick(t + 0.05)
check("不重复切", out == ["今天我们讨论一下财务报表。"], str(out))
check("尾巴为空", seg.tail() == "", repr(seg.tail()))

# 2) 半句不切，等逗号或停稳
seg = FastSegmenter(hold_ms=320)
out = seg.push("这个季度的收入", 10.0)
out += seg.tick(10.4)
check("没有标点不切", out == [], str(out))
out += seg.push("这个季度的收入同比", 10.5)
out += seg.tick(10.6)
check("有新字也不切", out == [], str(out))

# 3) 逗号切，但要等更久（soft_hold = 320ms）
seg = FastSegmenter(hold_ms=320)
out = seg.push("这个季度的收入同比，", 20.0)
out += seg.tick(20.2)
check("逗号 0.2s 还不切", out == [], str(out))
out += seg.tick(20.35)
check("逗号 0.35s 切", out == ["这个季度的收入同比，"], str(out))

# 4) 服务端 fixed 去重：已经提前交出去的部分不能再吐一遍
seg = FastSegmenter(hold_ms=320)
seg.push("今天我们讨论一下财务报表。收入同比", 30.0)
seg.tick(30.3)
check("提前切第一句", seg.tail() == "收入同比", repr(seg.tail()))
rest = seg.take_final("今天我们讨论一下财务报表。", 30.4) if False else seg.take_final("今天我们讨论一下财务报表。")
check("fixed 已被提前输出 -> 不重复", rest == [], str(rest))
rest2 = seg.take_final("今天我们讨论一下财务报表。收入同比下降")
check("fixed 更长 -> 只补后面", rest2 == ["收入同比下降"], str(rest2))
check("消费位置推进", seg.consumed == len("今天我们讨论一下财务报表。收入同比下降"), str(seg.consumed))

# 5) 本轮结束交尾巴
seg = FastSegmenter(hold_ms=320)
seg.push("还没说完的半句", 40.0)
tail = seg.take_end()
check("end 交尾巴", tail == ["还没说完的半句"], str(tail))
check("end 后清空", seg.tail() == "" and seg.consumed == 0)

# 6) 硬切：一直没有标点，攒够长度自动逗号断开
seg = FastSegmenter(hold_ms=320)
long_text = "这个季度我们的财务报表显示收入同比下降了百分之十二而成本上升了百分之八"
out = seg.push(long_text, 50.0)
out += seg.tick(50.2)
check("硬切要看更久（0.32*1.45=0.464s）", out == [], str(out))
out += seg.tick(50.5)
check("硬切生效", len(out) == 1 and out[0].endswith("，"), str(out))
check("硬切长度合理", 28 <= len(out[0].rstrip("，")) <= 36, str(len(out[0])))

# 7) 停很久（人不说了）不要硬切加逗号
seg = FastSegmenter(hold_ms=320)
seg.push(long_text, 60.0)
out = seg.tick(70.0)
check("长时间没更新不硬切", out == [], str(out))

# 8) 服务端换一轮（文本变短）：自动重置，不吞字
seg = FastSegmenter(hold_ms=320)
seg.push("第一轮说的话。", 80.0)
seg.tick(80.3)
seg.push("第二轮", 81.0)
seg.push("第二轮全新的一句。", 81.1)
out = seg.tick(81.35)
check("换轮后能正常切", out == ["第二轮全新的一句。"], str(out))

# 9) 关闭开关时完全不动
seg = FastSegmenter(hold_ms=320, enabled=False)
out = seg.push("这句话有标点。", 90.0) + seg.tick(90.5)
check("关闭时不分段", out == [], str(out))

# 10) 处理速度：1000 次 tick + 200 句
seg = FastSegmenter(hold_ms=320)
t0 = time.perf_counter()
for i in range(200):
    seg.push(f"第{i}句测试文本，内容差不多长。" * 1, 100.0 + i * 0.5)
    seg.tick(100.0 + i * 0.5 + 0.3)
seg.tick(999.0)
dt = (time.perf_counter() - t0) * 1000
check("200 句分段耗时 < 60ms", dt < 60, f"{dt:.1f}ms")

print()
print("FAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)
