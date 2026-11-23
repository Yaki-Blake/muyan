from __future__ import annotations

import io
import re
import wave

from PySide6.QtCore import QObject, Signal

SR = 16000

# 只用于对齐的字符集：去掉空白和标点，避免标点差异干扰匹配
_STRIP = re.compile(r"[\s，。！？、；：,.!?;:…—\-'\"“”‘’()（）\[\]【】]")


def pcm_to_wav(pcm: bytes, samplerate: int = SR) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(samplerate)
        w.writeframes(pcm)
    return buf.getvalue()


def _norm(s: str) -> str:
    return _STRIP.sub("", s)


def _common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def merge_transcript(prev: str, new: str) -> str:
    """prev 是已累计文本，new 是带重叠的新分片识别结果。返回应当新增的部分。

    分片识别时相邻两片有重叠，new 里会重复 prev 的结尾。难点在于：
      - whisper 两次转写同一段音频措辞可能不同
      - 重叠词可能出现在 new 的开头，也可能往后偏几个字
    所以这里在 new 的前若干字符内滑动，找与 prev 尾部最长的模糊匹配，切掉它。
    """
    new = (new or "").strip()
    if not new:
        return ""
    if not prev:
        return new

    p, n = _norm(prev), _norm(new)
    if not n:
        return ""

    # 整段已经出现过
    if n in p:
        return ""

    # 在 new 的前 12 个字符内滑动起点，找 prev 尾部与 new 片段的最长匹配
    best_len = 0
    best_start = 0
    max_k = min(len(p), 60)
    for start in range(0, min(12, len(n))):
        avail = len(n) - start
        for k in range(min(max_k, avail), 2, -1):
            if k <= best_len:
                break
            tail = p[-k:]
            seg = n[start:start + k]
            # 精确匹配优先；否则要求 85% 以上相似
            if tail == seg or _common_prefix(tail, seg) >= int(k * 0.85):
                best_len = k
                best_start = start
                break

    if best_len < 3:
        return new

    # 把 best_start + best_len 映射回原始 new 的字符位置（跳过标点计数）
    target = best_start + best_len
    seen = 0
    cut = len(new)
    for i, ch in enumerate(new):
        if _STRIP.match(ch):
            continue
        if seen == target:
            cut = i
            break
        seen += 1
    else:
        cut = len(new)

    rest = new[cut:].strip()
    # 边界落在标点上时，去掉重复的标点
    rest = rest.lstrip("，。！？、；：,.!?;:…— ").strip()
    if rest and prev and prev[-1] in "，。！？、；：,.!?;:" and rest[0] in "，。！？、；：,.!?;:":
        rest = rest[1:].strip()
    # 英文单词之间补空格，中文不加
    if rest and prev:
        a, b = prev[-1], rest[0]
        if a.isascii() and a.isalnum() and b.isascii() and b.isalnum():
            rest = " " + rest
        elif a in "，。！？、；：,.!?;:" and b.isascii() and b.isalnum():
            rest = " " + rest
    return rest


class ASRProvider(QObject):
    """识别器基类。所有信号都在主线程消费。"""

    final = Signal(str)    # 新增的一句（增量）
    partial = Signal(str)  # 正在识别中的临时文本
    error = Signal(str)    # 致命错误：链路必须停
    warn = Signal(str)     # 可自愈的异常：只提示，不中断
    status = Signal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._running = False

    def start(self):
        self._running = True
        self.status.emit("已启动")

    def feed(self, pcm: bytes):
        raise NotImplementedError

    def stop(self):
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def _require_key(self) -> bool:
        if not self.settings.api_key.strip():
            self.error.emit("还没有填 API Key，去设置里填一个。")
            return False
        return True
