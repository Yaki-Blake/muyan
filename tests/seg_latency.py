"""量化对比：同一段语音文本，标点和断句分别要多久才出现在屏幕上。

模拟服务端行为：variable 每 100ms 吐一块累积文本；
人说完后，fixed 还要再压 1.2 秒才回来（接近云知声判停的真实延迟）。
对比「旧行为：只认 fixed」与「新的提前断句」。
"""
import sys
import time

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))

from muyan.asr.fastseg import FastSegmenter

SENTENCE = "今天我们讨论一下第三季度的财务报表。"
CHUNK = 0.1          # 服务端每 100ms 吐一块
FIXED_DELAY = 1.2    # 判停后 fixed 还要压多久
LIMIT = 4.0


def run(fast: bool) -> float:
    """返回「这句带标点的话出现在屏幕上」用了多少秒。"""
    seg = FastSegmenter(hold_ms=320, enabled=fast)
    t0 = time.monotonic()

    n = 1
    while time.monotonic() - t0 < LIMIT:
        time.sleep(CHUNK)
        el = time.monotonic() - t0
        if n <= len(SENTENCE):
            text = SENTENCE[:n]
            n += 1
        else:
            text = SENTENCE
        pieces = seg.push(text)
        if fast and pieces:
            return el
        # 旧行为：只有服务端 fixed 才算定稿
        if el >= len(SENTENCE) * CHUNK + FIXED_DELAY:
            if not fast:
                return el
            seg.take_final(SENTENCE)
            return el
        for p in seg.tick():
            if fast:
                return time.monotonic() - t0
    return LIMIT


first = run(True)
old = run(False)
print(f"旧的（等服务端 fixed）: {old:.2f}s")
print(f"新的（提前断句）      : {first:.2f}s")
print(f"提前了                : {old - first:.2f}s（{(old - first) / old * 100:.0f}%）")
