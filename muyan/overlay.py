"""悬浮字幕弹窗（2026-11 第二版）。

和旧的「只显示最近几行」字幕条相比，这里换成一块可以一直往下写、能自己滚动的
字幕面板：

* 所有识别结果按顺序累积，不会因为「最多行数」被丢掉；
* 滚轮上下翻，翻上去以后新到的字不会把视图拽走（不跳）；
* Ctrl + 滚轮改字号，Shift + 滚轮整体缩放窗口，拖边缘/四角自由改大小和比例；
* 右键有复制、保存、回中、清空。

绘制仍然是自绘（QTextLayout 折行 + 描边 + 渐变底），所以外观设置和预览一致。
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QWidget

from .config import Settings

# 中文字形好看的字体，按优先级挑第一个装了的
FALLBACK_FAMILIES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "微软雅黑",
    "HarmonyOS Sans SC",
    "Source Han Sans SC",
    "思源黑体",
    "Noto Sans CJK SC",
    "PingFang SC",
    "Segoe UI",
)

MIN_W = 360          # 窗口最窄（再窄折行会碎成一片）
MIN_H = 140
SCROLL_GUTTER = 11.0  # 右侧留给滚动条的宽度（常驻，出现滚动条时不重排）
HISTORY_MAX = 2000    # 会话里最多留多少段，防止挂一整天把内存吃光
RESIZE_MARGIN = 7     # 边缘多少像素内算「拖边框」
BLOCK_GAP = 0.22      # 段间距 = 行距 × 这个系数
PM_MARGIN = 6.0       # 字形位图四周留出的边（描边/投影会稍微溢出）
PM_CACHE_MAX = 80     # 最多缓存多少段字形位图（约 40MB 上限）

_family_cache: dict[str, str] = {}


def resolve_family(explicit: str = "") -> str:
    """把设置里的字体名解析成实际可用的字体；空值就挑一个中文黑体。"""
    key = (explicit or "").strip()
    if key in _family_cache:
        return _family_cache[key]
    try:
        families = set(QFontDatabase.families())
    except Exception:
        families = set()
    chosen = key if (key and key in families) else ""
    if not chosen:
        for cand in FALLBACK_FAMILIES:
            if cand in families:
                chosen = cand
                break
    _family_cache[key] = chosen
    return chosen


def build_font(s: Settings, point_size: float | None = None) -> QFont:
    f = QFont()
    fam = resolve_family(getattr(s, "font_family", "") or "")
    if fam:
        f.setFamily(fam)
    size = float(point_size if point_size is not None else s.font_size)
    f.setPointSizeF(max(6.0, size))
    f.setWeight(QFont.Weight.DemiBold)
    ls = float(getattr(s, "letter_spacing", 0.0) or 0.0)
    if abs(ls) > 1e-6:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, ls)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


_RING: dict[float, list[tuple[float, float]]] = {}


def _ring(radius: float) -> list[tuple[float, float]]:
    """描边用的环形偏移点（12 个方向比 4/8 个更圆）。"""
    r = round(float(radius), 2)
    if r in _RING:
        return _RING[r]
    pts = [
        (r * math.cos(2 * math.pi * i / 12), r * math.sin(2 * math.pi * i / 12))
        for i in range(12)
    ]
    _RING[r] = pts
    return pts


def _line_step(s: Settings, font: QFont) -> float:
    spacing = max(1.0, float(getattr(s, "line_spacing", 1.3) or 1.3))
    return QFontMetricsF(font).lineSpacing() * spacing


def _one_layout(text: str, s: Settings, font: QFont, width: float, step: float,
                top: float):
    """排一段文字，返回 (QTextLayout, 顶边, 高度)。"""
    align = str(getattr(s, "align", "left") or "left").lower()
    lay = QTextLayout(text, font)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    lay.setTextOption(opt)
    lay.beginLayout()
    n = 0
    local_y = 0.0
    while True:
        line = lay.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        tw = float(line.naturalTextWidth())
        if align == "right":
            x = max(0.0, width - tw)
        elif align == "center":
            x = max(0.0, (width - tw) / 2.0)
        else:
            x = 0.0
        line.setPosition(QPointF(x, local_y))
        local_y += step
        n += 1
        if n >= 200:  # 兜底：别让一条疯了的长文本把界面拖死
            break
    lay.endLayout()
    if local_y <= 0:
        local_y = step
    return lay, top, local_y


def layout_document(texts: list[str], s: Settings, width: float, size: float | None = None):
    """把整块字幕按宽度折行，返回 (font, entries, doc_height)。

    entries 每项是 (QTextLayout, 顶边 y, 高度)：绘制时把 layout 的原点放到
    (0, 顶边 y) 就行，行内坐标已经排好。
    """
    font = build_font(s, size if size else s.font_size)
    step = _line_step(s, font)
    block_gap = step * BLOCK_GAP

    entries: list[tuple[QTextLayout, float, float]] = []
    top = 0.0
    for text in texts:
        lay, _t, h = _one_layout(text, s, font, width, step, top)
        entries.append((lay, top, h))
        top += h + block_gap

    doc_h = (entries[-1][1] + entries[-1][2]) if entries else 0.0
    return font, entries, doc_h


def _draw_lines(p: QPainter, lay: QTextLayout, origin: QPointF, fill: QColor,
                outline: QColor, ow: float):
    # 投影：贴着文字下方一点点，深色背景上提立体感
    shadow = QColor(0, 0, 0, 130)
    p.setPen(shadow)
    lay.draw(p, origin + QPointF(0.0, ow + 1.6))

    if ow > 0:
        p.setPen(outline)
        for dx, dy in _ring(ow):
            lay.draw(p, origin + QPointF(dx, dy))

    p.setPen(fill)
    lay.draw(p, origin)


def _paint_background(p: QPainter, rect: QRectF, s: Settings):
    radius = float(max(0, int(getattr(s, "corner_radius", 18))))
    r = min(radius, rect.height() / 2.0, rect.width() / 2.0)

    base = QColor(s.bg_color)
    a = max(0.0, min(1.0, float(getattr(s, "bg_alpha", 0.70))))

    body = QRectF(rect)
    body.adjust(0.5, 0.5, -0.5, -0.5)
    path = QPainterPath()
    path.addRoundedRect(body, r, r)

    grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    top = QColor(base)
    top.setAlphaF(min(1.0, a * 0.94))
    bottom = QColor(base)
    bottom.setAlphaF(min(1.0, a))
    grad.setColorAt(0.0, top)
    grad.setColorAt(1.0, bottom)

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawPath(path)

    # 一道几乎看不见的顶光，避免纯黑块显得死板
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
    p.drawPath(path)


def _paint_dot(p: QPainter, rect: QRectF, s: Settings, state: str):
    if not bool(getattr(s, "show_state_dot", True)):
        return
    palette = {
        "live": QColor("#37D67A"),
        "idle": QColor("#F2B84B"),
        "error": QColor("#FF5F56"),
    }
    color = palette.get(state, palette["idle"])
    # 左下角一颗小指示灯：有声音在流 = 绿，安静 = 琥珀，出错 = 红
    cx = rect.left() + 14.0
    cy = rect.bottom() - 14.0

    halo = QColor(color)
    halo.setAlphaF(0.16)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(halo)
    p.drawEllipse(QPointF(cx, cy), 6.5, 6.5)

    core = QColor(color)
    core.setAlphaF(0.95 if state == "live" else 0.70)
    p.setBrush(core)
    p.drawEllipse(QPointF(cx, cy), 2.9, 2.9)


def _content_rect(rect: QRectF, s: Settings, gutter: float):
    px = float(max(0, int(getattr(s, "padding_x", 34))))
    py = float(max(0, int(getattr(s, "padding_y", 18))))
    inner = QRectF(
        rect.left() + px,
        rect.top() + py,
        max(24.0, rect.width() - 2 * px),
        max(24.0, rect.height() - 2 * py),
    )
    text_w = max(24.0, inner.width() - gutter)
    return inner, text_w


def paint_subtitle(painter: QPainter, rect: QRectF, settings: Settings, blocks,
                   state: str = "live"):
    """设置面板的预览：画一块「从顶部开始」的字幕面板（静态，不滚动）。"""
    texts = [str(t).strip() for t in (blocks or []) if str(t).strip()]
    if not texts and bool(getattr(settings, "hide_when_empty", True)):
        return

    p = painter
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    _paint_background(p, rect, settings)

    inner, text_w = _content_rect(rect, settings, SCROLL_GUTTER)
    if texts:
        _font, entries, doc_h = layout_document(texts, settings, text_w)
        fill = QColor(settings.text_color)
        outline = QColor(getattr(settings, "outline_color", "#000000") or "#000000")
        ow = float(getattr(settings, "outline", 0.0) or 0.0)

        p.save()
        p.setClipRect(inner)
        origin_y = inner.top()
        for lay, top, _h in entries:
            if top > inner.height() + 2:
                break
            _draw_lines(p, lay, QPointF(inner.left(), origin_y + top), fill, outline, ow)
        p.restore()

        if doc_h > inner.height() + 1:
            _paint_fade(p, inner, settings, top=False, bottom=True)
            _paint_scrollbar(p, inner, 0.0, inner.height() / doc_h)

    _paint_dot(p, rect, settings, state)
    p.restore()


def _paint_fade(p: QPainter, inner: QRectF, s: Settings, top: bool, bottom: bool):
    """上下边缘的渐隐，提示「上面/下面还有字」；视口已对齐行边界，做得很轻。"""
    c = QColor(s.bg_color)
    a = max(0.0, min(1.0, float(getattr(s, "bg_alpha", 0.85))))
    h = min(12.0, inner.height() * 0.18)
    for yes, from_top in ((top, True), (bottom, False)):
        if not yes:
            continue
        grad = QLinearGradient(0, 0, 0, 1)
        grad.setCoordinateMode(QLinearGradient.CoordinateMode.ObjectMode)
        if from_top:
            grad.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(), int(255 * a * 0.58)))
            grad.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0))
            r = QRectF(inner.left(), inner.top(), inner.width(), h)
        else:
            grad.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(), 0))
            grad.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), int(255 * a * 0.58)))
            r = QRectF(inner.left(), inner.bottom() - h, inner.width(), h)
        p.fillRect(r, grad)


def _paint_scrollbar(p: QPainter, inner: QRectF, frac: float, ratio: float):
    """右侧细滚动条。frac = 顶部位置（0 = 最上），ratio = 视口 / 内容。"""
    track = QRectF(inner.right() - 2.0, inner.top() + 4.0, 3.5, inner.height() - 8.0)
    if track.height() <= 20:
        return
    r = track.width() / 2.0
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 26))
    p.drawRoundedRect(track, r, r)

    ratio = max(0.06, min(1.0, ratio))
    th = max(24.0, track.height() * ratio)
    travel = max(0.0, track.height() - th)
    y = track.top() + max(0.0, min(1.0, frac)) * travel
    thumb = QRectF(track.left(), y, track.width(), th)
    p.setBrush(QColor(255, 255, 255, 96))
    p.drawRoundedRect(thumb, r, r)


class SubtitleOverlay(QWidget):
    """无边框置顶的字幕弹窗：累积所有字、可滚动、可缩放。"""

    PARTIAL_TTL = 6.0  # 临时文本这么久没更新就清掉，避免「卡着半句」

    def __init__(self, settings: Settings, on_geometry_changed=None, on_open_settings=None):
        super().__init__(None)
        self.settings = settings
        self._on_geometry_changed = on_geometry_changed
        self._on_open_settings = on_open_settings

        self._lines: list[str] = []
        self._partial = ""
        self._partial_at = 0.0
        self._last_audio_at = 0.0
        self._state_override: str | None = None
        self._shown_state = "idle"

        # 版面缓存
        self._rev = 0
        self._finals_rev = 0
        self._font_sig = None
        self._layout_count = 0
        self._finals_entries: list[tuple[QTextLayout, float, float]] = []
        self._finals_h = 0.0
        self._font: QFont | None = None
        self._entries: list[tuple[QTextLayout, float, float]] = []
        self._doc_h = 0.0
        self._step = 40.0

        # 滚动状态：offset 是「离底部多远」，0 = 停在最新一句
        self._offset = 0.0
        # target 是滚动的目标位置，offset 每帧朝它缓动过去，才有「丝滑」的感觉
        self._target = 0.0
        self._follow = True
        self._anchor_start = 0.0
        self._anchor_vh = 0.0
        self._anchor_doc_h = 0.0
        self._pre_offset = 0.0
        self._pre_target = 0.0

        # 鼠标交互
        self._mode: str | None = None          # move / resize / scroll
        self._press_global = QPointF()
        self._geom0 = QRect()
        self._edges = 0
        self._thumb0 = 0.0

        # 滚动时的字形位图缓存：动画期间直接贴图，省掉每条 14 次文字绘制
        self._pm_cache: dict[tuple, QPixmap] = {}
        self._pm_keys: list[tuple] = []

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setWindowTitle("幕言 · 字幕")

        self._apply_geometry()
        self.apply_style()

        self._tick = QTimer(self)
        self._tick.setInterval(300)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

        self._persist_timer = QTimer(self)
        self._persist_timer.setSingleShot(True)
        self._persist_timer.setInterval(400)
        self._persist_timer.timeout.connect(self._persist)

        # 滚动缓动：16ms 一帧（≈60fps），每帧把 offset 拉向 target
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(16)
        self._scroll_timer.timeout.connect(self._animate_scroll)

    # ---------- 外观 ----------

    def apply_style(self):
        s = self.settings
        # 整窗不透明度会让文字一起发灰，这里固定 1.0，透明度只交给底色
        self.setWindowOpacity(1.0)
        self._invalidate()
        self._sync_pointer_events()
        self.update()

    def _sync_pointer_events(self):
        """空白时整块不可见，就别再挡住下面的鼠标点击。"""
        through = bool(getattr(self.settings, "click_through", False))
        if bool(getattr(self.settings, "hide_when_empty", True)) and not self._blocks():
            through = True
        try:
            if self.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents) != through:
                self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, through)
        except Exception:
            pass

    def _apply_geometry(self):
        s = self.settings
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        w = max(MIN_W, int(s.width))
        h = max(MIN_H, int(s.height))
        if s.x is not None and s.y is not None and s.x >= 0 and s.y >= 0:
            x, y = int(s.x), int(s.y)
        elif avail:
            x = avail.x() + (avail.width() - w) // 2
            y = avail.y() + avail.height() - h - 90
        else:
            x, y = 100, 100
        # 显示器换了/拔了之后，别把窗口留在屏幕外
        try:
            target = QGuiApplication.screenAt(QPoint(x + w // 2, y + h // 2))
            area = target.availableGeometry() if target else avail
            if area is not None:
                x = max(area.x(), min(x, area.x() + area.width() - w))
                y = max(area.y(), min(y, area.y() + area.height() - h))
        except Exception:
            pass
        self.setGeometry(x, y, w, h)

    def reset_position(self):
        self.settings.x = -1
        self.settings.y = -1
        self._apply_geometry()
        self._persist()

    # ---------- 状态 ----------

    def note_audio(self):
        self._last_audio_at = time.monotonic()

    def set_state(self, state: str | None):
        """state=None 回到「按音频活跃度自动判断」；error 会一直红着。"""
        self._state_override = state
        self.update()

    def _state(self) -> str:
        if self._state_override:
            return self._state_override
        if self._last_audio_at and time.monotonic() - self._last_audio_at < 1.5:
            return "live"
        return "idle"

    def _on_tick(self):
        now = time.monotonic()
        changed = False
        if self._partial and now - self._partial_at > self.PARTIAL_TTL:
            self._begin_change()
            self._partial = ""
            self._rev += 1
            self._end_change()
            changed = True
        state = self._state()
        if state != self._shown_state:
            self._shown_state = state
            changed = True
        if changed:
            self._sync_pointer_events()
            self.update()

    # ---------- 文本 ----------

    def _begin_change(self):
        self._ensure_layout()
        self._anchor_doc_h = self._doc_h
        self._anchor_vh = self._view_h()
        self._anchor_start = self._visible_start()
        self._pre_offset = self._offset
        self._pre_target = self._target

    def _end_change(self):
        self._ensure_layout()
        maxo = self._max_offset()
        if self._follow and bool(getattr(self.settings, "auto_follow", True)):
            # 跟随最新：先把画面钉在原来的位置，再缓动滑到底，
            # 新句子进来时是「滚上去」而不是「跳一下」
            new_off = max(0.0, min(maxo, self._doc_h - self._view_h() - self._anchor_start))
            self._offset = new_off
            self._target = 0.0
            # 只差一点点就滑过去；差得远（比如刚从卡顿里恢复、一下冒出一大堆）
            # 直接归位，免得画面追着跑
            if 0.42 < new_off <= self._view_h() * 1.2:
                self._start_scroll_anim()
            else:
                self._offset = 0.0
        else:
            new_off = max(0.0, min(maxo, self._doc_h - self._view_h() - self._anchor_start))
            delta = new_off - self._pre_offset
            self._offset = new_off
            self._target = max(0.0, min(maxo, self._pre_target + delta))
        self._sync_pointer_events()
        self.update()

    def add_final(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        if self._lines and self._lines[-1] == text:
            if self._partial:
                self._begin_change()
                self._partial = ""
                self._rev += 1
                self._end_change()
            return
        self._begin_change()
        self._partial = ""
        self._lines.append(text)
        if len(self._lines) > HISTORY_MAX:
            drop = len(self._lines) - HISTORY_MAX
            del self._lines[:drop]
        self._finals_rev += 1
        self._rev += 1
        self._end_change()

    def set_partial(self, text: str):
        text = (text or "").strip()
        if text == self._partial:
            return
        if text and self._lines and self._lines[-1] == text:
            text = ""
        self._begin_change()
        self._partial = text
        self._partial_at = time.monotonic()
        self._rev += 1
        self._end_change()

    def clear_text(self):
        self._lines.clear()
        self._partial = ""
        self._finals_rev += 1
        self._rev += 1
        self._offset = 0.0
        self._target = 0.0
        self._follow = True
        self._stop_scroll_anim()
        self._invalidate()
        self._sync_pointer_events()
        self.update()

    def current_text(self) -> str:
        parts = list(self._lines)
        if self._partial:
            parts.append(self._partial)
        return "\n".join(parts)

    def full_text(self) -> str:
        return "\n".join(self._lines)

    def _blocks(self) -> list[str]:
        out = list(self._lines)
        if self._partial and bool(getattr(self.settings, "show_partial", True)):
            out.append(self._partial)
        return out

    # ---------- 版面 ----------

    def _invalidate(self):
        self._font_sig = None
        self._clear_pm_cache()

    def _inner(self) -> QRectF:
        rect = QRectF(0.0, 0.0, float(self.width()), float(self.height()))
        inner, _w = _content_rect(rect, self.settings, SCROLL_GUTTER)
        return inner

    def _view_h(self) -> float:
        return max(1.0, self._inner().height())

    def _ensure_layout(self):
        """排版：定稿的每段只排一次，新来一句只补一句；临时半句单独重排。"""
        inner = self._inner()
        text_w = max(24.0, inner.width() - SCROLL_GUTTER)
        s = self.settings
        sig = (
            round(text_w, 1),
            float(s.font_size),
            float(getattr(s, "line_spacing", 1.3) or 1.3),
            float(getattr(s, "letter_spacing", 0.0) or 0.0),
            str(getattr(s, "align", "left") or "left"),
            str(getattr(s, "font_family", "") or ""),
        )

        if sig != self._font_sig:
            # 字号/宽度/字体变了：整块重排（不常发生）
            font, entries, doc_h = layout_document(self._lines, s, text_w)
            self._font = font
            self._step = _line_step(s, font)
            self._finals_entries = entries
            self._finals_h = doc_h
            self._layout_count = len(self._lines)
            self._font_sig = sig
        elif len(self._lines) != self._layout_count:
            if len(self._lines) == self._layout_count + 1 and self._font:
                # 正常情况：只在末尾多了一句，补排一句就够了
                text = self._lines[-1]
                top = self._finals_h + (self._step * BLOCK_GAP if self._finals_h > 0 else 0.0)
                lay, _t, h = _one_layout(text, s, self._font, text_w, self._step, top)
                self._finals_entries.append((lay, top, h))
                self._finals_h = top + h
                self._layout_count = len(self._lines)
            else:
                # 清空 / 裁掉最旧的一段：重排
                font, entries, doc_h = layout_document(self._lines, s, text_w)
                self._font = font
                self._step = _line_step(s, font)
                self._finals_entries = entries
                self._finals_h = doc_h
                self._layout_count = len(self._lines)

        entries = list(self._finals_entries)
        doc_h = self._finals_h
        if self._partial and bool(getattr(s, "show_partial", True)) and self._font:
            top = doc_h + (self._step * BLOCK_GAP if doc_h > 0 else 0.0)
            lay, _t, h = _one_layout(self._partial, s, self._font, text_w, self._step, top)
            entries.append((lay, top, h))
            doc_h = top + h
        self._entries = entries
        self._doc_h = doc_h

    def _max_offset(self) -> float:
        return max(0.0, self._doc_h - self._view_h())

    def _block_pixmap(self, lay: QTextLayout, h: float, text: str, text_w: float,
                      fill: QColor, outline: QColor, ow: float):
        """把一段字画进位图缓存。滚动动画期间直接贴图，比每条重画 14 次快好几倍。

        静态时不走缓存（直接用矢量文字，最清晰）。
        """
        try:
            dpr = float(self.devicePixelRatioF() or 1.0)
        except Exception:
            dpr = 1.0
        key = (
            text,
            round(float(h), 1),
            round(float(text_w), 1),
            round(float(ow), 2),
            fill.rgba(),
            outline.rgba(),
            round(dpr, 2),
        )
        pm = self._pm_cache.get(key)
        if pm is not None:
            return pm

        w = int(math.ceil(text_w + PM_MARGIN * 2.0))
        hh = int(math.ceil(float(h) + PM_MARGIN * 2.0))
        if w <= 0 or hh <= 0:
            return None
        try:
            pm = QPixmap(int(w * dpr), int(hh * dpr))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent)
            pp = QPainter(pm)
            pp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pp.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            _draw_lines(pp, lay, QPointF(PM_MARGIN, PM_MARGIN), fill, outline, ow)
            pp.end()
        except Exception:
            return None

        self._pm_cache[key] = pm
        self._pm_keys.append(key)
        if len(self._pm_keys) > PM_CACHE_MAX:
            old = self._pm_keys.pop(0)
            self._pm_cache.pop(old, None)
        return pm

    def _clear_pm_cache(self):
        self._pm_cache.clear()
        self._pm_keys.clear()

    def _visible_start(self) -> float:
        vh = self._view_h()
        if self._doc_h <= vh:
            return 0.0
        return max(0.0, self._doc_h - vh - self._offset)

    # ---------- 绘制 ----------

    def paintEvent(self, _ev):
        s = self.settings
        texts = self._blocks()
        if not texts and bool(getattr(s, "hide_when_empty", True)):
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        rect = QRectF(0.0, 0.0, float(self.width()), float(self.height()))
        _paint_background(p, rect, s)

        inner = self._inner()
        if not texts:
            p.setPen(QColor(255, 255, 255, 90))
            p.drawText(inner, Qt.AlignmentFlag.AlignCenter, "等待语音…")
            _paint_dot(p, rect, s, self._state())
            p.end()
            return

        self._ensure_layout()
        vh = inner.height()
        start = self._visible_start()
        maxo = self._max_offset()

        items = []
        for idx, (lay, top, h) in enumerate(self._entries):
            if top + h < start - 4.0:
                continue
            if top > start + vh + 4.0:
                break
            items.append((idx, lay, top, h))

        fill = QColor(s.text_color)
        outline = QColor(getattr(s, "outline_color", "#000000") or "#000000")
        ow = float(getattr(s, "outline", 0.0) or 0.0)
        base_y = inner.top() - start
        partial_idx = len(self._blocks()) - 1 if self._partial else -1

        p.save()
        p.setClipRect(inner)
        text_w = max(24.0, inner.width() - SCROLL_GUTTER)
        animating = self._scroll_timer.isActive()
        dpr = float(self.devicePixelRatioF() or 1.0)
        for idx, lay, top, _h in items:
            color = fill
            if idx == partial_idx:
                color = QColor(fill)
                color.setAlphaF(0.82)  # 正在识别的半句略淡，和定稿区分
            if animating and idx != partial_idx and idx < len(texts):
                pm = self._block_pixmap(lay, _h, texts[idx], text_w, color, outline, ow)
                if pm is not None:
                    # 对齐到设备像素，滚动时文字不会因为亚像素而发虚
                    qx = round((inner.left() - PM_MARGIN) * dpr) / dpr
                    qy = round((base_y + top - PM_MARGIN) * dpr) / dpr
                    p.drawPixmap(QPointF(qx, qy), pm)
                    continue
            _draw_lines(p, lay, QPointF(inner.left(), base_y + top), color, outline, ow)
        p.restore()

        if maxo > 0.5:
            _paint_fade(p, inner, s, top=start > 0.5, bottom=start + vh < self._doc_h - 0.5)
            _paint_scrollbar(p, inner, start / maxo, vh / max(1.0, self._doc_h))

        _paint_dot(p, rect, s, self._state())
        p.end()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._invalidate()
        self._ensure_layout()
        maxo = self._max_offset()
        self._offset = 0.0 if self._follow else max(0.0, min(maxo, self._offset))
        self._target = 0.0 if self._follow else max(0.0, min(maxo, self._target))
        self._stop_scroll_anim()

    # ---------- 滚动 ----------

    def _start_scroll_anim(self):
        if not self._scroll_timer.isActive():
            self._scroll_timer.start()

    def _stop_scroll_anim(self):
        if self._scroll_timer.isActive():
            self._scroll_timer.stop()

    def _animate_scroll(self):
        """每帧把 offset 拉向 target（指数缓动），没有吸附、没有整行跳。"""
        diff = self._target - self._offset
        if abs(diff) <= 0.6:
            self._offset = self._target
            self._stop_scroll_anim()
            self.update()
            return
        # 0.34/帧 ≈ 100ms 内收敛到位：手感跟手但不生硬
        self._offset += diff * 0.34
        self._follow = self._target <= 0.5
        self.update()

    def _scroll_by(self, delta: float):
        """只改目标位置，真正的位移交给缓动动画。"""
        self._ensure_layout()
        maxo = self._max_offset()
        if maxo <= 0.5:
            self._target = 0.0
            self._offset = 0.0
            self._follow = True
            self.update()
            return
        self._target = max(0.0, min(maxo, self._target + delta))
        self._follow = self._target <= 0.5
        self._start_scroll_anim()

    def scroll_bottom(self):
        self._follow = True
        self._target = 0.0
        # 离得不远就滑回去，离得很远（翻了几十句）直接归位
        if 0.6 < abs(self._offset) <= self._view_h() * 1.5:
            self._start_scroll_anim()
        else:
            self._offset = 0.0
            self._stop_scroll_anim()
        self.update()

    def wheelEvent(self, ev):
        dy = float(ev.angleDelta().y())
        pixel = float(ev.pixelDelta().y())
        if pixel and not dy:
            # 触控板：pixelDelta 本身就是像素，直接位移，最跟手
            self._scroll_by(pixel * 1.0)
            ev.accept()
            return
        if not dy:
            ev.ignore()
            return
        steps = dy / 120.0
        mods = ev.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            self._zoom_font(1.09 ** steps)
            ev.accept()
            return
        if mods & Qt.KeyboardModifier.ShiftModifier:
            self._scale_window(1.06 ** steps)
            ev.accept()
            return
        self._ensure_layout()
        # 一格只走 1.15 行，配合缓动才有「滚动」而不是「跳页」的手感
        self._scroll_by(steps * max(16.0, self._step * 1.15))
        ev.accept()

    def _zoom_font(self, factor: float):
        s = self.settings
        old = int(s.font_size)
        new = max(10, min(96, int(round(old * factor))))
        if new == old:
            return
        self._begin_change()
        s.font_size = new
        self._rev += 1
        self._invalidate()
        self._end_change()
        self._persist_soon()

    def _scale_window(self, factor: float):
        w = max(MIN_W, int(round(self.width() * factor)))
        h = max(MIN_H, int(round(self.height() * factor)))
        avail = self._screen_area()
        if avail is not None:
            w = min(w, int(avail.width() * 0.96))
            h = min(h, int(avail.height() * 0.96))
        if w == self.width() and h == self.height():
            return
        self.setGeometry(self._clamped_rect(QRect(self.x(), self.y(), w, h)))
        self.settings.width = self.width()
        self.settings.height = self.height()
        self._persist_soon()

    def _screen_area(self) -> QRect | None:
        try:
            scr = QGuiApplication.screenAt(self.geometry().center())
            if scr is None:
                scr = QGuiApplication.primaryScreen()
            return scr.availableGeometry() if scr else None
        except Exception:
            return None

    def _clamped_rect(self, r: QRect) -> QRect:
        area = self._screen_area()
        if area is None:
            return r
        w = max(MIN_W, min(r.width(), int(area.width() * 0.96)))
        h = max(MIN_H, min(r.height(), int(area.height() * 0.96)))
        x = max(area.left(), min(r.x(), area.right() - w + 1))
        y = max(area.top(), min(r.y(), area.bottom() - h + 1))
        return QRect(x, y, w, h)

    # ---------- 鼠标 ----------

    def _edges_at(self, pos: QPointF) -> int:
        """0=不在边上，1=左 2=右 4=上 8=下（可组合成角）。"""
        w, h = float(self.width()), float(self.height())
        m = float(RESIZE_MARGIN)
        e = 0
        if pos.x() <= m:
            e |= 1
        elif pos.x() >= w - m:
            e |= 2
        if pos.y() <= m:
            e |= 4
        elif pos.y() >= h - m:
            e |= 8
        return e

    def _cursor_for(self, edges: int):
        if edges == 0:
            return Qt.CursorShape.ArrowCursor
        if edges == 1 or edges == 2:
            return Qt.CursorShape.SizeHorCursor
        if edges == 4 or edges == 8:
            return Qt.CursorShape.SizeVerCursor
        if edges in (5, 10):
            return Qt.CursorShape.SizeFDiagCursor
        return Qt.CursorShape.SizeBDiagCursor

    def _over_scrollbar(self, pos: QPointF) -> bool:
        inner = self._inner()
        return pos.x() >= inner.right() - 9.0 and pos.y() >= inner.top() - 2.0

    def mousePressEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        pos = ev.position()
        edges = self._edges_at(pos)
        self._press_global = ev.globalPosition()
        if edges:
            self._mode = "resize"
            self._edges = edges
            self._geom0 = self.geometry()
            self.setCursor(self._cursor_for(edges))
        elif self._over_scrollbar(pos) and self._max_offset() > 0.5:
            self._mode = "scroll"
            self._scroll_drag_to(pos)
        else:
            self._mode = "move"
            self._geom0 = self.geometry()
        self.update()

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        gpos = ev.globalPosition()
        if self._mode == "move":
            d = (gpos - self._press_global).toPoint()
            self.move(self._geom0.topLeft() + d)
        elif self._mode == "resize":
            self._do_resize(gpos)
        elif self._mode == "scroll":
            self._scroll_drag_to(pos)
        else:
            self.setCursor(self._cursor_for(self._edges_at(pos)))

    def mouseReleaseEvent(self, ev):
        mode = self._mode
        self._mode = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if mode in ("move", "resize"):
            self.settings.x = self.x()
            self.settings.y = self.y()
            self.settings.width = self.width()
            self.settings.height = self.height()
            self._persist()

    def _do_resize(self, gpos: QPointF):
        g = QRect(self._geom0)
        d = (gpos - self._press_global).toPoint()
        dx, dy = d.x(), d.y()
        e = self._edges
        if e & 1:
            g.setLeft(min(g.right() - MIN_W, g.left() + dx))
        if e & 2:
            g.setRight(max(g.left() + MIN_W, g.right() + dx))
        if e & 4:
            g.setTop(min(g.bottom() - MIN_H, g.top() + dy))
        if e & 8:
            g.setBottom(max(g.top() + MIN_H, g.bottom() + dy))

        if ev_shift_held():
            ratio = max(0.2, self._geom0.width() / max(1, self._geom0.height()))
            only_h = bool(e & (1 | 2)) and not bool(e & (4 | 8))
            only_v = bool(e & (4 | 8)) and not bool(e & (1 | 2))
            if only_h:
                h = int(round(g.width() / ratio))
                if e & 4:
                    g.setTop(g.bottom() - h)
                else:
                    g.setBottom(g.top() + h)
            elif only_v:
                w = int(round(g.height() * ratio))
                if e & 1:
                    g.setLeft(g.right() - w)
                else:
                    g.setRight(g.left() + w)
            else:
                if abs(g.width() - self._geom0.width()) >= abs(g.height() - self._geom0.height()):
                    h = int(round(g.width() / ratio))
                    if e & 4:
                        g.setTop(g.bottom() - h)
                    else:
                        g.setBottom(g.top() + h)
                else:
                    w = int(round(g.height() * ratio))
                    if e & 1:
                        g.setLeft(g.right() - w)
                    else:
                        g.setRight(g.left() + w)

        self.setGeometry(self._clamped_rect(g))

    def _track_rects(self):
        inner = self._inner()
        track = QRectF(inner.right() - 2.0, inner.top() + 4.0, 3.5, inner.height() - 8.0)
        if track.height() <= 20 or self._doc_h <= 0:
            return None, None
        ratio = max(0.06, min(1.0, inner.height() / max(1.0, self._doc_h)))
        th = max(24.0, track.height() * ratio)
        travel = max(0.0, track.height() - th)
        frac = 0.0 if self._max_offset() <= 0 else self._visible_start() / self._max_offset()
        thumb = QRectF(track.left(), track.top() + frac * travel, track.width(), th)
        return track, thumb

    def _scroll_drag_to(self, pos: QPointF):
        track, thumb = self._track_rects()
        if track is None or thumb is None:
            return
        travel = max(1.0, track.height() - thumb.height())
        frac = (pos.y() - track.top() - thumb.height() / 2.0) / travel
        frac = max(0.0, min(1.0, frac))
        self._ensure_layout()
        maxo = self._max_offset()
        self._offset = maxo * (1.0 - frac)
        self._target = self._offset
        self._follow = self._offset <= 0.5
        self._stop_scroll_anim()   # 拖动时 1:1 跟手，不做缓动
        self.update()

    # ---------- 菜单 / 持久化 ----------

    def copy_all(self):
        QApplication.clipboard().setText(self.current_text())

    def save_txt(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "保存字幕", "幕言-字幕.txt", "文本文件 (*.txt)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.current_text())
        except Exception:
            pass

    def contextMenuEvent(self, ev):
        menu = QMenu(self)
        menu.addAction("滚到底部（跟随最新）", self.scroll_bottom)
        menu.addAction("复制全部文字", self.copy_all)
        menu.addAction("保存为 txt…", self.save_txt)
        menu.addSeparator()
        menu.addAction("清空文字", self.clear_text)
        menu.addAction("面板回中", self.reset_position)
        if self._on_open_settings:
            menu.addSeparator()
            menu.addAction("打开设置…", self._on_open_settings)
        menu.exec(ev.globalPos())

    def _persist_soon(self):
        self._persist_timer.start()

    def _persist(self):
        self.settings.width = self.width()
        self.settings.height = self.height()
        self.settings.x = self.x()
        self.settings.y = self.y()
        if self._on_geometry_changed:
            self._on_geometry_changed()


def ev_shift_held() -> bool:
    try:
        return bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
    except Exception:
        return False
