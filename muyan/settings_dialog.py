from __future__ import annotations

import copy

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFontDatabase, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .audio import list_loopback_devices
from .config import PROVIDER_PRESETS, Settings
from .overlay import paint_subtitle


def _num(sp, width: int = 104):
    """数字框别横着撑满整行，挤在一起很难看。"""
    sp.setFixedWidth(width)
    sp.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return sp


class ColorButton(QPushButton):
    """点一下弹调色板，按钮本身显示当前颜色。"""

    def __init__(self, color: str, parent=None):
        super().__init__(parent)
        self._color = color or "#000000"
        self.setFixedSize(78, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._pick)
        self._sync()

    def _sync(self):
        self.setText(self._color.upper())
        fg = "#000000" if QColor(self._color).lightness() > 150 else "#FFFFFF"
        self.setStyleSheet(
            f"QPushButton{{background:{self._color};color:{fg};border:1px solid #b8bcc4;"
            "border-radius:7px;font-size:11px;font-weight:600;}}"
        )

    def _pick(self):
        c = QColorDialog.getColor(QColor(self._color), self, "选择颜色")
        if c.isValid():
            self._color = c.name()
            self._sync()
            self.colorChanged()

    def colorChanged(self):
        pass

    def color(self) -> str:
        return self._color

    def set_color(self, color: str):
        self._color = color or "#000000"
        self._sync()


class SubtitlePreview(QWidget):
    """用真正的渲染函数画一段示例字幕，改一个选项就能看到效果。"""

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setMinimumHeight(168)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "background:#111318;border:1px solid #2a2d36;border-radius:10px;"
        )

    SAMPLES = [
        "这一句是已经定稿的字幕，会一直累积在上面，不会消失。",
        "滚轮可以上下翻，翻上去以后新到的字不会把视图拽走。",
        "最后一句是正在识别的，颜色会略淡一点。",
    ]

    def paintEvent(self, _ev):
        from PySide6.QtGui import QBrush, QFontMetricsF, QLinearGradient

        from .overlay import SCROLL_GUTTER, layout_document

        p = QPainter(self)
        # 按宽度缩放（字不会小到看不清），高度能放几行画几行，绝不切半行字
        box_w = float(max(320, int(self.settings.width)))
        box_h = float(max(140, int(self.settings.height)))
        avail_w = float(self.width() - 20)
        avail_h = float(self.height() - 20)
        scale = min(avail_w / box_w, 1.0)

        pad_x = float(max(0, int(self.settings.padding_x)))
        pad_y = float(max(0, int(self.settings.padding_y)))
        text_w = max(60.0, box_w - 2.0 * pad_x - SCROLL_GUTTER)

        font, _entries, _doc_h = layout_document(self.SAMPLES, self.settings, text_w)
        fm = QFontMetricsF(font)
        step = fm.lineSpacing() * max(1.0, float(self.settings.line_spacing or 1.3))

        # 卡片高度：面板有多高就画多高，超出预览区就截到底
        card_h = min(box_h, max(pad_y * 2.0 + step, avail_h / scale))

        # 面板窄/字号大时把示例句压成一行，宁可加省略号也不折行挤爆预览
        samples = []
        used = pad_y
        for t in self.SAMPLES:
            if used + step + pad_y > card_h + 0.5:
                break
            line = t
            if fm.horizontalAdvance(line) > text_w:
                lo, hi = 1, len(t) - 1
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if fm.horizontalAdvance(t[:mid] + "…") <= text_w:
                        lo = mid
                    else:
                        hi = mid - 1
                line = t[:lo] + "…"
            samples.append(line)
            used += step * 1.22
        if not samples:
            samples = [self.SAMPLES[0]]

        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.translate((self.width() - box_w * scale) / 2.0, 10.0)
        p.scale(scale, scale)
        paint_subtitle(p, QRectF(0.0, 0.0, box_w, card_h), self.settings, samples, "live")
        p.restore()

        # 面板本身比预览还高（内容没画完）时，底部来一道很轻的渐隐
        if card_h < box_h - 1:
            card_w = box_w * scale
            left = (self.width() - card_w) / 2.0
            base = 10.0 + card_h * scale
            g = QLinearGradient(0, base - 18, 0, base)
            g.setColorAt(0.0, QColor(17, 19, 24, 0))
            g.setColorAt(1.0, QColor(17, 19, 24, 170))
            p.fillRect(QRectF(left, base - 18, card_w, 18), QBrush(g))
        p.end()


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        # 改副本：点取消不会污染正在使用的配置
        self.s = copy.deepcopy(settings)
        self.setWindowTitle("幕言 · 设置")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(940)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 14)
        root.setSpacing(14)

        columns = QHBoxLayout()
        columns.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(14)
        right = QVBoxLayout()
        right.setSpacing(14)

        columns.addLayout(left, 1)
        columns.addLayout(right, 1)
        root.addLayout(columns)

        # ---------- 识别服务 ----------
        gb_asr = QGroupBox("识别服务")
        form = QFormLayout(gb_asr)
        form.setSpacing(8)

        self.cb_provider = QComboBox()
        for key, preset in PROVIDER_PRESETS.items():
            self.cb_provider.addItem(preset["label"], key)
        idx = self.cb_provider.findData(self.s.provider)
        self.cb_provider.setCurrentIndex(max(0, idx))
        form.addRow("服务商", self.cb_provider)

        self.ed_key = QLineEdit(self.s.api_key)
        self.ed_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_key.setPlaceholderText("sk-...")
        key_row = QHBoxLayout()
        key_row.addWidget(self.ed_key)
        btn_show = QPushButton("显示")
        btn_show.setCheckable(True)
        btn_show.setFixedWidth(56)
        btn_show.toggled.connect(
            lambda on: self.ed_key.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        key_row.addWidget(btn_show)
        kw = QWidget()
        kw.setLayout(key_row)
        form.addRow("API Key", kw)

        self.ed_base = QLineEdit(self.s.base_url)
        form.addRow("Base URL", self.ed_base)

        self.ed_model = QLineEdit(self.s.model)
        form.addRow("模型名", self.ed_model)

        self.cb_lang = QComboBox()
        for label, val in [("中文", "zh"), ("英文", "en"), ("自动", "")]:
            self.cb_lang.addItem(label, val)
        li = self.cb_lang.findData(self.s.language)
        self.cb_lang.setCurrentIndex(max(0, li))
        form.addRow("语言", self.cb_lang)

        row_fast = QHBoxLayout()
        self.ck_fast = QCheckBox("提前断句")
        self.ck_fast.setChecked(bool(self.s.fast_break))
        self.ck_fast.setToolTip(
            "不等服务端判停：累积文本停稳就先把标点那段定稿，标点和换行会早半拍出现。"
        )
        row_fast.addWidget(self.ck_fast)
        self.sp_fast = QSpinBox()
        self.sp_fast.setRange(150, 700)
        self.sp_fast.setSingleStep(20)
        self.sp_fast.setSuffix(" ms")
        self.sp_fast.setValue(int(self.s.fast_break_ms))
        self.sp_fast.setToolTip("等多久算「停稳」。越小越早，太小容易把半句切断。")
        self.sp_fast.setEnabled(self.ck_fast.isChecked())
        _num(self.sp_fast, 96)
        self.ck_fast.toggled.connect(self.sp_fast.setEnabled)
        row_fast.addWidget(QLabel("等待"))
        row_fast.addWidget(self.sp_fast)
        row_fast.addStretch(1)
        wrap_fast = QWidget()
        wrap_fast.setLayout(row_fast)
        form.addRow("断句", wrap_fast)

        self.lb_note = QLabel()
        self.lb_note.setWordWrap(True)
        self.lb_note.setStyleSheet("color:#666;font-size:12px;")
        form.addRow("", self.lb_note)

        self.lb_keylink = QLabel()
        self.lb_keylink.setOpenExternalLinks(True)
        self.lb_keylink.setWordWrap(True)
        form.addRow("", self.lb_keylink)
        left.addWidget(gb_asr)

        # ---------- 音频 ----------
        gb_audio = QGroupBox("音频采集")
        af = QFormLayout(gb_audio)
        af.setSpacing(8)
        self.cb_device = QComboBox()
        self.cb_device.addItem("默认扬声器（推荐）", "")
        for name in list_loopback_devices():
            self.cb_device.addItem(name, name)
        di = self.cb_device.findData(self.s.device_name)
        self.cb_device.setCurrentIndex(max(0, di))
        af.addRow("输出设备", self.cb_device)

        self.sp_chunk = QDoubleSpinBox()
        self.sp_chunk.setRange(0.8, 8.0)
        self.sp_chunk.setSingleStep(0.2)
        self.sp_chunk.setDecimals(1)
        self.sp_chunk.setSuffix(" 秒")
        self.sp_chunk.setValue(self.s.chunk_seconds)
        _num(self.sp_chunk, 120)
        af.addRow("分片时长", self.sp_chunk)
        left.addWidget(gb_audio)
        left.addStretch(1)

        # ---------- 预览 ----------
        self.preview = SubtitlePreview(self.s, self)
        right.addWidget(self.preview)

        # ---------- 字幕外观 ----------
        gb_look = QGroupBox("字幕外观")
        lf = QFormLayout(gb_look)
        lf.setSpacing(8)

        self.cb_font = QComboBox()
        self.cb_font.addItem("自动（推荐黑体）", "")
        try:
            families = sorted(QFontDatabase.families(), key=str.lower)
        except Exception:
            families = []
        for fam in families:
            self.cb_font.addItem(fam, fam)
        fi = self.cb_font.findData(self.s.font_family)
        if fi < 0:
            self.cb_font.addItem(self.s.font_family, self.s.font_family)
            fi = self.cb_font.count() - 1
        self.cb_font.setCurrentIndex(max(0, fi))
        lf.addRow("字体", self.cb_font)

        row_size = QHBoxLayout()
        self.sp_font = QSpinBox()
        self.sp_font.setRange(10, 96)
        self.sp_font.setSuffix(" pt")
        self.sp_font.setValue(self.s.font_size)
        _num(self.sp_font, 104)
        row_size.addWidget(self.sp_font)
        self.cb_align = QComboBox()
        for label, val in [("居中", "center"), ("左对齐", "left"), ("右对齐", "right")]:
            self.cb_align.addItem(label, val)
        ai = self.cb_align.findData(self.s.align)
        if ai < 0:
            self.cb_align.addItem(self.s.align or "left", self.s.align or "left")
            ai = self.cb_align.count() - 1
        self.cb_align.setCurrentIndex(max(0, ai))
        self.cb_align.setFixedWidth(104)
        row_size.addWidget(QLabel("对齐"))
        row_size.addWidget(self.cb_align)
        row_size.addStretch(1)
        wrap_size = QWidget()
        wrap_size.setLayout(row_size)
        lf.addRow("字号 / 对齐", wrap_size)

        row_sp = QHBoxLayout()
        self.sp_line = QDoubleSpinBox()
        self.sp_line.setRange(1.0, 2.2)
        self.sp_line.setSingleStep(0.05)
        self.sp_line.setDecimals(2)
        self.sp_line.setValue(self.s.line_spacing)
        _num(self.sp_line, 92)
        row_sp.addWidget(QLabel("行距"))
        row_sp.addWidget(self.sp_line)
        self.sp_letter = QDoubleSpinBox()
        self.sp_letter.setRange(-1.0, 6.0)
        self.sp_letter.setSingleStep(0.1)
        self.sp_letter.setDecimals(1)
        self.sp_letter.setValue(self.s.letter_spacing)
        _num(self.sp_letter, 86)
        row_sp.addWidget(QLabel("字距"))
        row_sp.addWidget(self.sp_letter)
        row_sp.addStretch(1)
        wrap_sp = QWidget()
        wrap_sp.setLayout(row_sp)
        lf.addRow("排版", wrap_sp)

        row_box = QHBoxLayout()
        self.sp_width = QSpinBox()
        self.sp_width.setRange(320, 3840)
        self.sp_width.setSingleStep(20)
        self.sp_width.setValue(self.s.width)
        _num(self.sp_width, 100)
        row_box.addWidget(QLabel("宽"))
        row_box.addWidget(self.sp_width)
        self.sp_height = QSpinBox()
        self.sp_height.setRange(140, 1200)
        self.sp_height.setSingleStep(10)
        self.sp_height.setValue(self.s.height)
        _num(self.sp_height, 86)
        row_box.addWidget(QLabel("高"))
        row_box.addWidget(self.sp_height)
        row_box.addStretch(1)
        wrap_box = QWidget()
        wrap_box.setLayout(row_box)
        lf.addRow("面板尺寸", wrap_box)

        self.btn_text_color = ColorButton(self.s.text_color)
        lf.addRow("文字颜色", self.btn_text_color)

        row_outline = QHBoxLayout()
        self.sp_outline = QDoubleSpinBox()
        self.sp_outline.setRange(0.0, 6.0)
        self.sp_outline.setSingleStep(0.2)
        self.sp_outline.setDecimals(1)
        self.sp_outline.setSuffix(" px")
        self.sp_outline.setValue(self.s.outline)
        _num(self.sp_outline, 88)
        row_outline.addWidget(self.sp_outline)
        self.btn_outline_color = ColorButton(self.s.outline_color)
        row_outline.addWidget(self.btn_outline_color)
        row_outline.addStretch(1)
        wrap_outline = QWidget()
        wrap_outline.setLayout(row_outline)
        lf.addRow("描边 粗细 / 颜色", wrap_outline)

        row_bg = QHBoxLayout()
        self.btn_bg_color = ColorButton(self.s.bg_color)
        row_bg.addWidget(self.btn_bg_color)
        self.sp_bg_alpha = QDoubleSpinBox()
        self.sp_bg_alpha.setRange(0.0, 1.0)
        self.sp_bg_alpha.setSingleStep(0.05)
        self.sp_bg_alpha.setDecimals(2)
        self.sp_bg_alpha.setValue(self.s.bg_alpha)
        _num(self.sp_bg_alpha, 88)
        row_bg.addWidget(QLabel("浓度"))
        row_bg.addWidget(self.sp_bg_alpha)
        row_bg.addStretch(1)
        wrap_bg = QWidget()
        wrap_bg.setLayout(row_bg)
        lf.addRow("底色", wrap_bg)

        row_shape = QHBoxLayout()
        self.sp_radius = QSpinBox()
        self.sp_radius.setRange(0, 60)
        self.sp_radius.setValue(self.s.corner_radius)
        _num(self.sp_radius, 74)
        row_shape.addWidget(QLabel("圆角"))
        row_shape.addWidget(self.sp_radius)
        self.sp_pad_x = QSpinBox()
        self.sp_pad_x.setRange(0, 120)
        self.sp_pad_x.setValue(self.s.padding_x)
        _num(self.sp_pad_x, 78)
        row_shape.addWidget(QLabel("左右留白"))
        row_shape.addWidget(self.sp_pad_x)
        self.sp_pad_y = QSpinBox()
        self.sp_pad_y.setRange(0, 100)
        self.sp_pad_y.setValue(self.s.padding_y)
        _num(self.sp_pad_y, 78)
        row_shape.addWidget(QLabel("上下留白"))
        row_shape.addWidget(self.sp_pad_y)
        row_shape.addStretch(1)
        wrap_shape = QWidget()
        wrap_shape.setLayout(row_shape)
        lf.addRow("外形", wrap_shape)

        row_flags = QHBoxLayout()
        self.ck_dot = QCheckBox("显示状态点")
        self.ck_dot.setChecked(bool(self.s.show_state_dot))
        row_flags.addWidget(self.ck_dot)
        self.ck_hide = QCheckBox("空白时隐藏")
        self.ck_hide.setChecked(bool(self.s.hide_when_empty))
        row_flags.addWidget(self.ck_hide)
        self.ck_follow = QCheckBox("自动跟随最新")
        self.ck_follow.setChecked(bool(self.s.auto_follow))
        row_flags.addWidget(self.ck_follow)
        row_flags.addStretch(1)
        self.btn_reset_look = QPushButton("恢复默认外观")
        self.btn_reset_look.clicked.connect(self._reset_look)
        row_flags.addWidget(self.btn_reset_look)
        wrap_flags = QWidget()
        wrap_flags.setLayout(row_flags)
        lf.addRow("细节", wrap_flags)

        right.addWidget(gb_look)
        right.addStretch(1)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

        self.cb_provider.currentIndexChanged.connect(self._on_provider_changed)
        self._on_provider_changed()

        # 任一外观控件变化 → 写回副本 → 重画预览
        for w in (
            self.cb_font,
            self.cb_align,
            self.sp_font,
            self.sp_line,
            self.sp_letter,
            self.sp_width,
            self.sp_height,
            self.sp_outline,
            self.sp_bg_alpha,
            self.sp_radius,
            self.sp_pad_x,
            self.sp_pad_y,
        ):
            if hasattr(w, "valueChanged"):
                w.valueChanged.connect(self._collect_look)
            if hasattr(w, "currentIndexChanged"):
                w.currentIndexChanged.connect(self._collect_look)
        for b in (self.btn_text_color, self.btn_outline_color, self.btn_bg_color):
            b.clicked.connect(self._collect_look)
        for c in (self.ck_dot, self.ck_hide, self.ck_follow):
            c.toggled.connect(self._collect_look)

        self._collect_look()

    # ---------- 交互 ----------

    def _on_provider_changed(self):
        key = self.cb_provider.currentData()
        preset = PROVIDER_PRESETS[key]
        self.ed_base.setText(preset.get("base_url", ""))
        self.ed_model.setText(preset.get("model", ""))
        self.ed_base.setCursorPosition(0)  # 长 URL 从头显示，别让人以为被截断了
        self.lb_note.setText(preset.get("note", ""))
        url = preset.get("key_url", "")
        self.lb_keylink.setText(f'<a href="{url}">→ 在这里申请 Key</a>' if url else "")
        needs_key = preset.get("kind") not in ("local", "mock")
        self.ed_key.setEnabled(needs_key)
        self.ed_key.setPlaceholderText("sk-..." if needs_key else "这个模式不需要 Key")

    def _collect_look(self, *_args):
        s = self.s
        s.font_family = self.cb_font.currentData() or ""
        s.align = self.cb_align.currentData() or "center"
        s.font_size = int(self.sp_font.value())
        s.line_spacing = float(self.sp_line.value())
        s.letter_spacing = float(self.sp_letter.value())
        s.width = int(self.sp_width.value())
        s.height = int(self.sp_height.value())
        s.outline = float(self.sp_outline.value())
        s.bg_alpha = float(self.sp_bg_alpha.value())
        s.corner_radius = int(self.sp_radius.value())
        s.padding_x = int(self.sp_pad_x.value())
        s.padding_y = int(self.sp_pad_y.value())
        s.text_color = self.btn_text_color.color()
        s.outline_color = self.btn_outline_color.color()
        s.bg_color = self.btn_bg_color.color()
        s.show_state_dot = bool(self.ck_dot.isChecked())
        s.hide_when_empty = bool(self.ck_hide.isChecked())
        s.auto_follow = bool(self.ck_follow.isChecked())
        if hasattr(self, "preview"):
            self.preview.update()

    def _reset_look(self):
        d = Settings()
        self.cb_font.setCurrentIndex(max(0, self.cb_font.findData(d.font_family)))
        self.cb_align.setCurrentIndex(max(0, self.cb_align.findData(d.align)))
        self.sp_font.setValue(d.font_size)
        self.sp_line.setValue(d.line_spacing)
        self.sp_letter.setValue(d.letter_spacing)
        self.sp_width.setValue(d.width)
        self.sp_height.setValue(d.height)
        self.sp_outline.setValue(d.outline)
        self.sp_bg_alpha.setValue(d.bg_alpha)
        self.sp_radius.setValue(d.corner_radius)
        self.sp_pad_x.setValue(d.padding_x)
        self.sp_pad_y.setValue(d.padding_y)
        self.btn_text_color.set_color(d.text_color)
        self.btn_outline_color.set_color(d.outline_color)
        self.btn_bg_color.set_color(d.bg_color)
        self.ck_dot.setChecked(d.show_state_dot)
        self.ck_hide.setChecked(d.hide_when_empty)
        self.ck_follow.setChecked(d.auto_follow)
        self._collect_look()

    # ---------- 结果 ----------

    def result_settings(self) -> Settings:
        self._collect_look()
        s = self.s
        s.provider = self.cb_provider.currentData()
        s.api_key = self.ed_key.text().strip()
        s.base_url = self.ed_base.text().strip()
        s.model = self.ed_model.text().strip()
        s.language = self.cb_lang.currentData() or ""
        s.fast_break = bool(self.ck_fast.isChecked())
        s.fast_break_ms = int(self.sp_fast.value())
        s.device_name = self.cb_device.currentData() or ""
        s.chunk_seconds = float(self.sp_chunk.value())
        return s


