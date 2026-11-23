"""字幕弹窗行为测试：累积、滚动不跳、滚轮/缩放、边缘缩放。"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QWheelEvent

from muyan.config import Settings
from muyan.overlay import SubtitleOverlay

OUT = os.path.join(os.path.dirname(__file__), "out")
os.makedirs(OUT, exist_ok=True)

app = QApplication(sys.argv)

s = Settings()
s.width, s.height = 960, 380
s.font_size = 24
s.bg_alpha = 0.85

ov = SubtitleOverlay(s)
ov.show()

LINES = [
    "第一句：系统声音已经抓到了，音量条在动就说明环流成功。",
    "第二句：换成真实服务商之后，这里显示的就是识别出来的原话了，一句接一句往上堆。",
    "第三句：滚轮可以上下翻，翻上去以后新到的字不会把视图拽走。",
    "第四句：面板会把这次输出的所有文字都留着，不会像以前那样只显示最近三行。",
    "第五句：这条比较长，用来确认长句折行之后行距是否均匀、左右留白是否对称。",
    "第六句：Ctrl 加滚轮改字号。",
    "第七句：Shift 加滚轮整体缩放窗口。",
    "第八句：拖边缘可以自由改宽高和比例。",
    "第九句：右键能复制全部、保存成 txt，也能一键回中。",
    "第十句：下面是最后一条。",
]
for t in LINES:
    ov.add_final(t)
ov.set_partial("这是正在识别的半句")
app.processEvents()
print("blocks:", len(ov._lines), "doc_h:", round(ov._doc_h, 1),
      "view_h:", round(ov._view_h(), 1), "offset:", ov._offset, "follow:", ov._follow)
ov.grab().save(os.path.join(OUT, "panel_bottom.png"))


def wheel(mods, dy=120, pos=None):
    p = QPointF(ov.width() / 2, ov.height() / 2)
    gp = QPointF(ov.mapToGlobal(QPoint(int(p.x()), int(p.y()))))
    ev = QWheelEvent(p, gp, QPoint(0, 0), QPoint(0, dy),
                     Qt.MouseButton.NoButton, mods,
                     Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(ov, ev)


# 往上翻 300px，再加新字，视图起点必须不变
wheel(Qt.KeyboardModifier.NoModifier, 240)
while ov._scroll_timer.isActive():
    ov._animate_scroll()
start_before = ov._visible_start()
print("scrolled to:", round(start_before, 1), "follow:", ov._follow)
ov.add_final("第十一句：翻上去之后新来的字，不应该把正在读的内容往上顶。")
while ov._scroll_timer.isActive():
    ov._animate_scroll()
app.processEvents()
start_after = ov._visible_start()
print(f"no-jump: before={start_before:.1f} after={start_after:.1f} follow={ov._follow}")
assert abs(start_after - start_before) < 1.0, "翻上去后加字导致视图跳了"
ov.grab().save(os.path.join(OUT, "panel_scrolled.png"))

# 滚回底部
ov.scroll_bottom()
print("back to bottom offset:", ov._offset, "follow:", ov._follow)

# Ctrl + 滚轮 → 字号变大
before_fs = s.font_size
wheel(Qt.KeyboardModifier.ControlModifier, 120)
wheel(Qt.KeyboardModifier.ControlModifier, 120)
app.processEvents()
print("font:", before_fs, "->", s.font_size)
assert s.font_size > before_fs

# Shift + 滚轮 → 窗口整体放大，比例不变
w0, h0 = ov.width(), ov.height()
wheel(Qt.KeyboardModifier.ShiftModifier, 120)
wheel(Qt.KeyboardModifier.ShiftModifier, 120)
app.processEvents()
print("window:", (w0, h0), "->", (ov.width(), ov.height()), "settings:", (s.width, s.height))
assert ov.width() > w0 and ov.height() > h0
assert abs(ov.width() / ov.height() - w0 / h0) < 0.02, "Shift 缩放没保住比例"

# 拖右下角（不经过事件循环，避免真实鼠标事件插进来）
f0 = ov.geometry()
ov._mode = "resize"
ov._edges = 2 | 8
ov._geom0 = QRect(f0)
ov._press_global = QPointF(f0.topLeft())
ov._do_resize(QPointF(f0.topLeft() + QPoint(140, 100)))
print("corner resize:", f0.size().toTuple(), "->", ov.geometry().size().toTuple())
ov._mode = None
assert ov.width() == f0.width() + 140 and ov.height() == f0.height() + 100
ov.grab().save(os.path.join(OUT, "panel_resized.png"))

# 拖左上角缩小，必须卡在最小尺寸
g1 = QRect(ov.geometry())
ov._mode = "resize"
ov._edges = 1 | 4
ov._geom0 = QRect(g1)
ov._press_global = QPointF(g1.topLeft())
ov._do_resize(QPointF(g1.topLeft() + QPoint(2000, 2000)))
ov._mode = None
print("min clamp:", ov.geometry())
assert ov.width() >= 360 and ov.height() >= 140

# 滚动条点击/拖动：点靠顶部 → 跳到最上面；点靠底部 → 回到底部
ov.setGeometry(120, 90, 900, 380)
ov.scroll_bottom()
ov._scroll_drag_to(QPointF(ov._inner().right() - 2, ov._inner().top() + 8))
top_start = ov._visible_start()
print("scrollbar top -> start:", round(top_start, 1), "follow:", ov._follow)
assert top_start < 5 and not ov._follow
ov._scroll_drag_to(QPointF(ov._inner().right() - 2, ov._inner().bottom() - 8))
print("scrollbar bottom -> start:", round(ov._visible_start(), 1), "follow:", ov._follow)
assert ov._follow

# 重绘耗时
ov.scroll_bottom()
ov._invalidate()
t0 = time.perf_counter()
for _ in range(40):
    ov.grab()
print(f"frame avg {(time.perf_counter() - t0) / 40 * 1000:.2f} ms")

# 慢慢堆积 300 句，看增量排版有没有变慢
t0 = time.perf_counter()
for i in range(300):
    ov.add_final(f"第 {i} 条增量测试：这一句用来确认历史很长的时候排版不会越来越慢。")
ov.set_partial("收尾")
app.processEvents()
print(f"300 lines added in {(time.perf_counter() - t0) * 1000:.0f} ms, blocks={len(ov._lines)}")
ov.grab().save(os.path.join(OUT, "panel_many.png"))
print("PASS")
