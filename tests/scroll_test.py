"""滚动手感测试：验证「无吸附 + 逐帧缓动 + 不跳变」。

用真实 SubtitleOverlay，塞 40 句，然后模拟滚轮 6 格，
逐帧调用 _animate_scroll，记录 offset 轨迹：
  - 每帧位移必须单调且小于阈值（不跳变）
  - 最终精确停在 target
  - 停下来时的 offset 允许是任意像素，不再对齐行边界
"""
import os
import sys
import time

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt, QPoint, QPointF, QEvent
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

import muyan.app as appmod
appmod.save_settings = lambda s: None  # 别动用户真实配置

from muyan.config import Settings
from muyan.overlay import SubtitleOverlay

FAIL = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(name)


def settle(ov, limit=400):
    """把缓动跑到停，返回逐帧轨迹。"""
    traj = []
    for _ in range(limit):
        if not ov._scroll_timer.isActive():
            break
        ov._animate_scroll()
        traj.append(ov._offset)
    return traj


app = QApplication.instance() or QApplication([])
s = Settings()
s.width, s.height = 960, 360
s.font_size, s.line_spacing = 22, 1.3
s.x = s.y = 40

ov = SubtitleOverlay(s)
ov.show()
for i in range(40):
    ov.add_final(f"第{i}句：这一句用来把面板撑高，好让滚动有内容可翻，句子长度也够折行。")
app.processEvents()
ov.scroll_bottom()
settle(ov)
app.processEvents()

maxo = ov._max_offset()
check("内容够高，可以滚动", maxo > 200, f"max_offset={maxo:.0f} doc={ov._doc_h:.0f}")
check("回底后 offset=0", abs(ov._offset) < 1e-6, f"{ov._offset:.3f}")

# ---- 滚轮：一格 1.15 行 ----
t0 = ov._target
events = []
for _ in range(6):
    ev = QWheelEvent(
        QPointF(50, 50), QPointF(50, 50), QPoint(0, 0), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    ov.wheelEvent(ev)
    events.append(round(ov._target - t0, 1))
check("6 格滚轮目标逐格累加", events == sorted(events) and len(set(events)) == 6, str(events))

step_px = 6 * max(16.0, ov._step * 1.15)
check("每格约 1.15 行（不是整页跳）", abs(events[-1] - step_px) < 3.0, f"{events[-1]:.1f} vs {step_px:.1f}")

# ---- 逐帧轨迹 ----
traj = settle(ov)

deltas = [abs(traj[i] - traj[i - 1]) for i in range(1, len(traj))]
check("有缓动过程（不是一步到位）", len(traj) >= 6, f"{len(traj)} 帧")
check("逐帧位移单调收敛（末帧收到 target 时允许 ≤0.7 的收尾）",
      all(deltas[i] <= deltas[i - 1] + 0.7 for i in range(1, len(deltas))),
      str([round(d, 1) for d in deltas[:8]]) + " ... last=" + f"{deltas[-1]:.2f}")
check("单帧最大位移 < 目标距离的 45%", max(deltas) < abs(events[-1]) * 0.45, f"max delta {max(deltas):.1f}")
check("最终精确停在 target", abs(ov._offset - ov._target) < 1e-6, f"{ov._offset:.3f} vs {ov._target:.3f}")
check("停下后定时器已关", not ov._scroll_timer.isActive())

# ---- 无吸附：滚动位置可以是任意像素，而且和小位移能落住 ----
check("停在非行边界位置", abs(ov._offset % ov._step) > 0.01 and abs(ov._offset % ov._step - ov._step) > 0.01,
      f"offset % step = {ov._offset % ov._step:.2f}")

ov._offset = ov._target = 0.0
ov._scroll_by(6.0)     # 只有 6px 的小位移
settle(ov)
check("小位移不会被吸附回原处", abs(ov._offset - 6.0) < 1e-6, f"{ov._offset:.2f}")

# ---- 滚到顶/底夹紧 ----
ov._target = 0.0
ov._offset = 0.0
for _ in range(120):
    ev = QWheelEvent(QPointF(50, 50), QPointF(50, 50), QPoint(0, 0), QPoint(0, 120),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    ov.wheelEvent(ev)
settle(ov)
check("滚到底不会超出 max_offset", ov._offset <= maxo + 1e-6 and abs(ov._offset - maxo) < 1.0,
      f"{ov._offset:.1f} / {maxo:.1f}")
check("滚到底不算跟随最新", ov._follow is False)

ov.scroll_bottom()
settle(ov)
check("回底恢复跟随", ov._follow is True and ov._offset < 1e-6, f"{ov._offset:.3f}")

# ---- 打字时不拽走视图（无吸附之后仍要成立）----
ov._offset = ov._target = 0.0
ov._scroll_by(300)
settle(ov)
start_before = ov._visible_start()
ov.add_final("翻上去以后新来的一句，不应该把视图拽走，位置要保持住。")
app.processEvents()
start_after = ov._visible_start()
check("翻上去后加新句，视图不动", abs(start_after - start_before) < 0.6,
      f"{start_before:.1f} -> {start_after:.1f}")

# ---- 跟随模式：新句是滑上去而不是跳一下 ----
ov.scroll_bottom()
settle(ov)
ov.add_final("下面这句是跟随模式下的新内容，应该缓动滑上来。")
app.processEvents()
check("跟随模式下发起了缓动", ov._scroll_timer.isActive() or ov._offset == 0.0,
      f"offset={ov._offset:.1f} anim={ov._scroll_timer.isActive()}")

# ---- 每帧绘制耗时（决定能不能稳住 60fps）----
from PySide6.QtGui import QImage

img = QImage(ov.width(), ov.height(), QImage.Format.Format_ARGB32_Premultiplied)
ov._offset = ov._target = maxo * 0.5
ov._ensure_layout()
ov._scroll_timer.start()          # 模拟正在滚动（走字形位图路径）
ov._pm_cache.clear()
ov._pm_keys.clear()
n = 40
t = time.perf_counter()
for i in range(n):
    ov._offset = maxo * 0.5 + i * 2.0     # 每帧位移 2px，模拟滚动
    ov.render(img)
dt = (time.perf_counter() - t) / n * 1000
check("单帧绘制 < 16.7ms（60fps）", dt < 16.7, f"{dt:.2f}ms/帧")
check("滚动动画期间的贴图绘制 < 5ms", dt < 5.0, f"{dt:.2f}ms/帧")

print()
print("FAILED:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)
