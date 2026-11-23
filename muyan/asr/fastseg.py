"""提前断句：不等服务端 `fixed`，本地先把「已经停稳的句子」切成一段。

为什么需要它：云知声的 `variable` 是这一轮的累积全文，标点和断句都靠服务端
在「一句话真的说完了」之后才回 `fixed`。而服务端的判停要等 VAD 确认安静，
通常还要再压 0.8–1.5 秒，于是屏幕上出现的是「一大串没有标点的字」，
标点姗姗来迟，断句更晚。

这里换个做法：自己盯着这段累积文本。一旦它「停稳」了（一小段时间没有变化），
并且结尾已经有标点，就把标点前那一段立刻定稿输出 —— 标点、断句和换行都跟着
提前出现。服务端稍后回 `fixed` 时，用「已经输出过多少字」去重，只补没输出的部分，
不会重复也不会错位。

判断「停稳」是为了防抖：识别是逐字往外吐的，正吐到一半就切会切出残句。
"""
from __future__ import annotations

import time

# 一句话收尾的标点：出现这类标点，说明这句真的结束了，可以很快切
STRONG = "。！？!?…；;\n"
# 句中停顿类标点：能切，但要等得久一点，避免把半句切断
SOFT = "，,、：:"
# 标点后面常常跟着的收尾符号（引号、括号），切的时候要带上它们
CLOSERS = "”’)）】]》\"'"

# 硬切：一直没有标点又一直不停地说，攒到这么长就自己加个逗号断开
HARD_CUT_CHARS = 28
# 切出来的段至少要这么长，太短的段（比如只有「。」）不切，留在下一段里
MIN_STRONG = 4
MIN_SOFT = 7


def _is_cjk(s: str) -> bool:
    for ch in s:
        if "\u4e00" <= ch <= "\u9fff":
            return True
    return False


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class FastSegmenter:
    """把一轮识别里的累积文本，按标点和停顿切成一段一段。

    用法::

        seg = FastSegmenter(hold_ms=320)
        for piece in seg.push(text, now):     # 收到 variable 时
            emit_final(piece)
        partial = seg.tail()                  # 剩下的半句，给 partial 显示
        for piece in seg.take_final(srv_text):  # 收到 fixed 时去重
            emit_final(piece)
        for piece in seg.take_end():           # 本轮结束，尾巴也要交出去
            emit_final(piece)
    """

    def __init__(self, hold_ms: float = 320.0, enabled: bool = True):
        hold = max(120.0, float(hold_ms or 320.0)) / 1000.0  # 毫秒 -> 秒
        self.enabled = bool(enabled)
        # 三类标点各自等多久。收尾标点等得最短，句中逗号等得久一些
        self.strong_hold = hold * 0.60
        self.soft_hold = hold
        self.hard_hold = hold * 1.45
        self.reset()

    # ---------- 状态 ----------
    def reset(self):
        self._round = ""          # 本轮累积文本（服务端 variable 的全文）
        self._consumed = 0        # 已经作为「定稿」交出去的字符数
        self._changed_at = 0.0    # 这段文本最后一次变化的时刻
        self._tick_at = 0.0

    @property
    def consumed(self) -> int:
        return self._consumed

    def tail(self) -> str:
        """还没定稿的那部分（含正在识别的半句）。"""
        if self._consumed >= len(self._round):
            return ""
        return self._round[self._consumed:]

    # ---------- 入口 ----------
    def push(self, text: str, now: float | None = None) -> list[str]:
        """收到一条累积文本，返回这次可以直接定稿的段（可能为空）。"""
        now = time.monotonic() if now is None else now
        text = (text or "")
        if not text:
            return []
        # 服务端换了一轮 / 或者回头改写了前面的字，都要重新对齐「已输出位置」
        if self._round:
            if len(text) < self._consumed:
                # 比已输出的还短：肯定是新的一轮，从头来
                self._consumed = 0
            else:
                # 识别偶尔会回头修字（"我们"→"我"）：把位置收到还能对上的地方，
                # 宁可多吐一两个字，也不能把已经显示过的整段再吐一遍
                c = _common_prefix_len(self._round, text)
                if c < self._consumed:
                    self._consumed = c
        if text != self._round:
            self._round = text
            self._changed_at = now
        return self._maybe_cut(now)

    def tick(self, now: float | None = None) -> list[str]:
        """没有新消息时也调一下：文本停稳了同样要切。"""
        now = time.monotonic() if now is None else now
        if not self._round:
            return []
        return self._maybe_cut(now)

    def take_final(self, text: str) -> list[str]:
        """服务端 fixed 到了：剥掉已经输出过的前缀，只交剩下的。"""
        text = (text or "").strip()
        if not text:
            return []
        if len(text) <= self._consumed:
            # 这句早就被我们提前交出去了
            return []
        rest = text[self._consumed:]
        self._consumed = max(self._consumed, len(text))
        rest = rest.strip()
        return [rest] if rest else []

    def take_end(self) -> list[str]:
        """本轮结束：把尾巴也交出去，然后清状态准备下一轮。"""
        rest = self.tail().strip()
        self.reset()
        return [rest] if rest else []

    # ---------- 核心：找切点 ----------
    def _maybe_cut(self, now: float) -> list[str]:
        if not self.enabled or not self._round:
            return []
        idle = now - self._changed_at
        self._tick_at = now

        buf = self._round[self._consumed:]
        if not buf:
            return []

        cut, wait, hard = self._find_cut(buf)
        if not cut or idle < wait:
            return []
        if hard and idle > max(3.0, wait * 3):
            # 停太久了说明人已经不说了，服务端马上会回 fixed，别自己硬加逗号
            return []

        piece = buf[:cut]
        self._consumed += cut
        if hard:
            piece += self.punct_for_hard_cut(piece)
        return [piece.strip()] if piece.strip() else []

    def _find_cut(self, buf: str) -> tuple[int, float, bool]:
        """返回 (切点相对 buf 的下标, 需要等多久, 是否硬切)。切点包含标点本身。"""
        # 1) 收尾标点：从后往前找最后一个
        for i in range(len(buf) - 1, -1, -1):
            if buf[i] in STRONG:
                end = i + 1
                while end < len(buf) and buf[end] in CLOSERS:
                    end += 1
                if end >= MIN_STRONG:
                    return end, self.strong_hold, False
                break

        # 2) 句中标点：能切，但要求更长、更稳
        for i in range(len(buf) - 1, -1, -1):
            if buf[i] in SOFT:
                if i + 1 >= MIN_SOFT:
                    return i + 1, self.soft_hold, False
                break

        # 3) 一路没有标点又一直不停：攒够长度就自己断开
        if len(buf) >= HARD_CUT_CHARS:
            return self._hard_cut_point(buf), self.hard_hold, True

        return 0, 0.0, False

    def _hard_cut_point(self, buf: str) -> int:
        """没标点时找个不破坏语义的位置硬切（切完会补一个逗号）。"""
        limit = min(len(buf), HARD_CUT_CHARS + 6)
        floor = HARD_CUT_CHARS - 6
        # 优先断在逗号/空格/「的、了、是」这类语气停点上，读起来自然
        for i in range(limit - 1, floor, -1):
            ch = buf[i]
            if ch in SOFT or ch == " ":
                return i
            if ch in "的了是在和与":
                return i + 1
        return limit

    def punct_for_hard_cut(self, piece: str) -> str:
        """硬切出来的段补什么标点：中文给逗号，英文给逗号加空格。"""
        if not piece:
            return ""
        last = piece.rstrip()[-1]
        if last in STRONG or last in SOFT:
            return ""
        return "，" if _is_cjk(piece) else ", "
