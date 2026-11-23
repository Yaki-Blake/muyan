from __future__ import annotations

import queue
import sys
import time

from PySide6.QtCore import QRectF, Qt, QTimer, Signal, QObject
from PySide6.QtGui import QAction, QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .asr import build_provider
from .audio import AudioPump, LoopbackCapture
from .config import APP_NAME, Settings, load_settings, save_settings
from .overlay import SubtitleOverlay
from .settings_dialog import SettingsDialog


def _app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#1F6FEB"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    p.setPen(QColor("white"))
    f = QFont()
    f.setPointSize(26)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "A")
    p.end()
    return QIcon(pm)


def _ui_font() -> QFont:
    try:
        fams = set(QFontDatabase.families())
    except Exception:
        fams = set()
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "Segoe UI"):
        if name in fams:
            f = QFont(name)
            f.setPointSizeF(10.0)
            return f
    return QFont()


STATUS_STYLES = {
    "idle": ("#F0F2F6", "#454B54"),
    "running": ("#E6F6EC", "#12703C"),
    "warning": ("#FFF4E3", "#8A5A00"),
    "error": ("#FDECEA", "#B3261E"),
}


class LevelMeter(QWidget):
    """十段音量条。自己画，不依赖等宽字体里有没有 ░ 这种字符。"""

    SEGMENTS = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self._level = 0.0
        self.setFixedSize(132, 14)

    def set_level(self, level: float):
        level = max(0.0, min(1.0, float(level)))
        if abs(level - self._level) > 0.004:
            self._level = level
            self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        gap = 3.0
        w = (self.width() - gap * (self.SEGMENTS - 1)) / self.SEGMENTS
        lit = int(round(self._level * self.SEGMENTS))
        for i in range(self.SEGMENTS):
            x = i * (w + gap)
            if i < lit:
                grad = QColor("#2B7CF5" if i < 7 else "#12A150")
                if i >= 8:
                    grad = QColor("#E0A100")
            else:
                grad = QColor("#E3E7EE")
            p.setBrush(grad)
            p.drawRoundedRect(QRectF(x, 0.0, w, float(self.height())), 2.5, 2.5)
        p.end()


class Bridge(QObject):
    """把工作线程的信号安全地转回主线程。"""

    final = Signal(str)
    partial = Signal(str)
    error = Signal(str)
    warn = Signal(str)
    status = Signal(str)
    level = Signal(float)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings: Settings = load_settings()
        self.setWindowTitle(f"{APP_NAME} · 系统声音字幕")
        self.setWindowIcon(_app_icon())
        self.resize(480, 340)

        self.overlay = SubtitleOverlay(
            self.settings,
            on_geometry_changed=self._persist,
            on_open_settings=self.open_settings,
        )
        self.overlay.show()

        self.bridge = Bridge()
        self.bridge.final.connect(self._on_final)
        self.bridge.partial.connect(self._on_partial)
        self.bridge.error.connect(self._on_error)
        self.bridge.warn.connect(self._on_warn)
        self.bridge.status.connect(self._on_status)
        self.bridge.level.connect(self._on_level)

        # 音频队列只留 3 秒（100ms 一块 → 30 块）。积压超过 3 秒说明下游跟不上，
        # 采集线程会丢最旧的块，宁可丢一点也不要越积越多最后一次性喷出来。
        self.audio_q: "queue.Queue[bytes]" = queue.Queue(maxsize=30)
        self.capture: LoopbackCapture | None = None
        self.pump: AudioPump | None = None
        self.provider = None
        self._running = False

        self._restart_count = 0
        self._last_restart_at = 0.0
        self._capture_restarts: list[float] = []
        self._last_provider_status = ""
        self._capture_was_restarted = False
        self._capture_cooldown_until = 0.0
        self._capture_error_status = False
        self._last_silence_reopens = 0

        self._build_ui()
        self._build_tray()

        # 音量条刷新（纯 UI，不碰音频）
        self._level_timer = QTimer(self)
        self._level_timer.setInterval(120)
        self._level_timer.timeout.connect(self._refresh_level)
        self._pending_level = 0.0

        # 看门狗：采集线程/发送线程/识别器任何一环悄悄死掉，都会被这里拉起来
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(2000)
        self._health_timer.timeout.connect(self._health_check)

        self._refresh_state()

    # ---------------- UI ----------------
    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        central.setStyleSheet(
            "QWidget#central{background:#FBFCFE;}"
            "QLabel{color:#22262C;}"
            "QPushButton{border-radius:9px;padding:6px 12px;font-size:13px;}"
        )
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        self.lb_status = QLabel("未启动")
        self.lb_status.setMinimumHeight(30)
        root.addWidget(self.lb_status, 0, Qt.AlignmentFlag.AlignLeft)

        self.lb_device = QLabel("设备：—")
        self.lb_device.setStyleSheet("color:#5F6673;font-size:12px;")
        root.addWidget(self.lb_device)

        level_row = QHBoxLayout()
        level_row.setSpacing(8)
        lb_level_title = QLabel("音量")
        lb_level_title.setStyleSheet("color:#5F6673;font-size:12px;")
        level_row.addWidget(lb_level_title)
        self.lb_level = LevelMeter()
        level_row.addWidget(self.lb_level)
        level_row.addStretch(1)
        root.addLayout(level_row)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.btn_toggle = QPushButton("开始字幕")
        self.btn_toggle.setMinimumHeight(44)
        self.btn_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle.clicked.connect(self.toggle)
        row.addWidget(self.btn_toggle, 1)

        btn_set = QPushButton("设置")
        btn_set.setMinimumHeight(44)
        btn_set.setMinimumWidth(76)
        btn_set.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_set.setStyleSheet(
            "QPushButton{background:#EEF1F6;color:#2B3038;border:1px solid #DCE1E8;}"
            "QPushButton:hover{background:#E4E9F1;}"
        )
        btn_set.clicked.connect(self.open_settings)
        row.addWidget(btn_set)
        root.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        for text, slot in (
            ("清空文字", lambda: self.overlay.clear_text()),
            ("字幕回中", self.overlay.reset_position),
        ):
            b = QPushButton(text)
            b.setMinimumHeight(36)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                "QPushButton{background:#FFFFFF;color:#3A404A;border:1px solid #DCE1E8;}"
                "QPushButton:hover{background:#F3F5F9;}"
            )
            b.clicked.connect(slot)
            row2.addWidget(b)
        row2.addStretch(1)
        root.addLayout(row2)

        self.lb_hint = QLabel(
            "字幕弹窗：滚轮上下翻；Ctrl+滚轮改字号；Shift+滚轮整体缩放；\n"
            "拖四边/四角可以自由改大小和比例；右键有复制、保存、清空。\n"
            "第一次用建议先选「测试模式」确认音频和显示都正常。"
        )
        self.lb_hint.setWordWrap(True)
        self.lb_hint.setStyleSheet("color:#8A9099;font-size:12px;")
        root.addWidget(self.lb_hint)
        root.addStretch(1)

        self.setCentralWidget(central)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(_app_icon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        self.act_toggle = QAction("开始字幕", self)
        self.act_toggle.triggered.connect(self.toggle)
        menu.addAction(self.act_toggle)

        act_show = QAction("显示主窗口", self)
        act_show.triggered.connect(self._show_main)
        menu.addAction(act_show)

        act_set = QAction("设置…", self)
        act_set.triggered.connect(self.open_settings)
        menu.addAction(act_set)

        menu.addSeparator()
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._show_main()
            if reason == QSystemTrayIcon.ActivationReason.Trigger
            else None
        )
        self.tray.show()

    def _show_main(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ---------------- 状态 ----------------
    def _set_status(self, text: str, kind: str = "idle"):
        bg, fg = STATUS_STYLES.get(kind, STATUS_STYLES["idle"])
        self.lb_status.setText(text)
        self.lb_status.setStyleSheet(
            f"background:{bg};color:{fg};border-radius:9px;"
            "padding:5px 12px;font-size:14px;font-weight:600;"
        )

    def _refresh_state(self):
        running = self._running
        self.btn_toggle.setText("停止字幕" if running else "开始字幕")
        self.btn_toggle.setStyleSheet(
            "QPushButton{font-size:15px;font-weight:600;color:white;border:none;"
            f"background:{'#C62828' if running else '#1F6FEB'};}}"
            f"QPushButton:hover{{background:{'#D63A3A' if running else '#2B7CF5'};}}"
        )
        self.act_toggle.setText("停止字幕" if running else "开始字幕")
        p = self.settings.preset()
        self.lb_device.setText(f"服务：{p['label']}")
        if not running:
            self._set_status("未启动", "idle")

    def _on_status(self, text: str):
        if not self._running:
            return
        self._last_provider_status = text
        kind = "running"
        if any(k in text for k in ("重连", "断开", "中断", "等待", "失败")):
            kind = "warning"
        self._set_status(text, kind)

    def _on_error(self, text: str):
        self._fail(text)

    def _on_warn(self, text: str):
        if not self._running:
            return
        self._set_status(text, "warning")

    def _on_final(self, text: str):
        self._restart_count = 0  # 能正常出字说明链路是好的
        self.overlay.add_final(text)

    def _on_partial(self, text: str):
        self.overlay.set_partial(text)

    def _on_level(self, rms: float):
        # 只记下来，由定时器统一刷新，避免每条音频都触发一次 UI 重绘
        self._pending_level = rms
        self.overlay.note_audio()
        self.overlay.set_state(None)

    def _refresh_level(self):
        n = int(max(0.0, min(1.0, self._pending_level / 3000.0)) * 10)
        self.lb_level.set_level(n / 10.0)

    # ---------------- 启停 ----------------
    def toggle(self):
        if self._running:
            self.stop()
        else:
            self.start()

    def start(self):
        if self._running:
            return
        s = self.settings
        if s.kind() not in ("local", "mock") and not s.api_key.strip():
            QMessageBox.information(
                self,
                "还差一步",
                "这个服务商需要 API Key。\n\n"
                "点「设置」填上 Key，或者先切到「测试模式」看看效果。",
            )
            self.open_settings()
            return

        self._running = True
        self._restart_count = 0
        self._last_restart_at = 0.0
        self._capture_restarts.clear()
        self._capture_was_restarted = False
        self._last_provider_status = ""
        self._capture_cooldown_until = 0.0
        self._capture_error_status = False
        self._boot()

    def _boot(self):
        if not self._running or self.provider is not None:
            return
        s = self.settings
        self._teardown()

        try:
            provider = build_provider(s, parent=self)
        except Exception as e:
            self._fail(f"无法创建识别器：{e}")
            return
        provider.final.connect(self.bridge.final)
        provider.partial.connect(self.bridge.partial)
        provider.error.connect(self.bridge.error)
        provider.warn.connect(self.bridge.warn)
        provider.status.connect(self.bridge.status)
        self.provider = provider
        # 先给个过渡状态，再启动；否则会把 provider 自己报的状态盖掉
        self._set_status("正在启动…", "warning")
        provider.start()
        if not self._running:
            return
        if not provider.running and s.kind() != "mock":
            # 启动失败（缺 Key / 缺依赖），provider 已经 emit 了 error
            self.provider = None
            return

        self._clear_audio_queue()
        self.capture = LoopbackCapture(self.audio_q, device_name=s.device_name)
        self._last_silence_reopens = 0
        self.capture.start()

        # 音频泵跑在独立线程：feed() 里的网络发送绝不占用 GUI 线程
        self.pump = AudioPump(
            self.audio_q,
            get_provider=lambda: self.provider,
            on_level=lambda rms: self.bridge.level.emit(rms),
            on_warn=lambda msg: self.bridge.warn.emit(msg),
        )
        self.pump.start()

        self.overlay.set_state(None)
        self._pending_level = 0.0
        self._refresh_state()
        self._level_timer.start()
        self._health_timer.start()
        self.tray.setToolTip(f"{APP_NAME} · 运行中")

        QTimer.singleShot(900, self._check_capture)

    def _check_capture(self):
        if not self._running:
            return
        cap = self.capture
        if cap is None:
            return
        if cap.error and cap.frames == 0:
            self._capture_error_status = True
            self._set_status(f"录音没起来：{cap.error}", "error")
            return
        self.lb_device.setText(
            f"服务：{self.settings.preset()['label']}　设备：{cap.device_label or '默认扬声器'}"
        )

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._level_timer.stop()
        self._health_timer.stop()
        self._teardown()
        self.overlay.set_state(None)
        self.lb_level.set_level(0.0)
        self._refresh_state()
        self.tray.setToolTip(APP_NAME)

    def _teardown(self):
        for attr in ("capture", "pump", "provider"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.stop()
                except Exception:
                    pass
                setattr(self, attr, None)
        self._clear_audio_queue()

    def _clear_audio_queue(self):
        while not self.audio_q.empty():
            try:
                self.audio_q.get_nowait()
            except Exception:
                break

    # ---------------- 看门狗 ----------------
    def _health_check(self):
        if not self._running:
            return

        if self.provider is None:
            # 启动阶段就失败了，已经被 _on_error 处理
            return

        if self.settings.kind() != "mock" and not self.provider.running:
            self._restart_pipeline("识别服务已断开，正在重连…")
            return

        if self.pump is None or not self.pump.is_alive():
            self._restart_pipeline("音频发送线程已退出，正在重建…")
            return

        cap = self.capture
        if cap is None:
            self._restart_capture("采集器不见了，正在重建…")
            return
        if not cap.is_alive():
            self._restart_capture("音频采集线程已退出，正在重建…")
            return
        if cap.silence_reopens != self._last_silence_reopens:
            # 采集自己发现「一直收到纯静音，环回句柄跟丢了」并重开过（切视频后常见）
            self._last_silence_reopens = cap.silence_reopens
            self._restart_asr_connection()
            return
        if cap.frames > 0 and self._capture_error_status:
            # 之前报过「录音没起来」，现在有数据了，把状态刷回来
            self._capture_error_status = False
            self._restore_running_status()
        if cap.frames > 0 and cap.seconds_since_data() > 12.0:
            self._restart_capture(
                f"已经 {int(cap.seconds_since_data())} 秒没收到系统声音，正在重开采集…"
            )

    def _restart_pipeline(self, reason: str):
        if not self._running:
            return
        now = time.monotonic()
        if now - self._last_restart_at > 30.0:
            self._restart_count = 0
        self._last_restart_at = now
        self._restart_count += 1

        if self._restart_count > 4:
            self._fail(f"{reason} 连续恢复失败，已停止，请检查网络或设置。")
            return

        self._set_status(reason, "warning")
        self.overlay.set_state("error")
        QTimer.singleShot(700, self._do_restart)

    def _do_restart(self):
        if not self._running:
            return
        self._teardown()
        self._boot()

    def _restart_asr_connection(self):
        """音频源变了：采集已自己重开，这里顺手换一条识别连接，字幕接得更快。"""
        self._set_status("音频流断了一下（切视频/换播放源常见），正在重连…", "warning")
        self._clear_audio_queue()
        provider = self.provider
        if provider is not None and hasattr(provider, "restart_connection"):
            try:
                provider.restart_connection("音频源切换")
            except Exception:
                pass
        self.overlay.set_state(None)

    def _restart_capture(self, reason: str):
        if not self._running:
            return
        now = time.monotonic()
        if now < self._capture_cooldown_until:
            return
        recent = [t for t in self._capture_restarts if now - t < 60.0]
        if len(recent) >= 3:
            # 短时间内反复断流，多半是设备问题：放慢重试，别把线程开爆
            self._capture_cooldown_until = now + 15.0
            self._set_status("音频采集反复中断，请检查输出设备或切「默认扬声器」", "error")
            return
        self._capture_restarts = recent + [now]

        self._set_status(reason, "warning")
        self.overlay.set_state("error")
        if self.capture is not None:
            try:
                self.capture.stop()
                # 等旧线程把 recorder 关掉，避免两个句柄同时开着
                self.capture.join(timeout=0.4)
            except Exception:
                pass
            self.capture = None
        self._clear_audio_queue()
        self._capture_was_restarted = True
        QTimer.singleShot(400, self._spawn_capture)

    def _spawn_capture(self):
        if not self._running or self.capture is not None:
            return
        self.capture = LoopbackCapture(self.audio_q, device_name=self.settings.device_name)
        self._last_silence_reopens = 0
        self.capture.start()
        QTimer.singleShot(900, self._check_capture)
        QTimer.singleShot(1500, self._confirm_capture_recovered)

    def _confirm_capture_recovered(self):
        if not self._running or self.capture is None:
            return
        if self.capture.frames <= 0 or not self._capture_was_restarted:
            return
        self._capture_was_restarted = False
        self._restore_running_status()

    def _restore_running_status(self):
        self.overlay.set_state(None)
        text = self._last_provider_status or "运行中 · 正在识别"
        if self.settings.kind() == "mock":
            text = "测试模式运行中（不联网）"
        self._set_status(text, "running")

    def _fail(self, text: str):
        self._running = False
        self._level_timer.stop()
        self._health_timer.stop()
        self._teardown()
        self.overlay.set_state("error")
        self._refresh_state()
        self._set_status("出错：" + text, "error")
        self.tray.setToolTip(APP_NAME)
        try:
            self.tray.showMessage(APP_NAME, text, QSystemTrayIcon.MessageIcon.Warning, 4000)
        except Exception:
            pass

    # ---------------- 设置 ----------------
    def open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            was_running = self._running
            if was_running:
                self.stop()
            self.settings = dlg.result_settings()
            save_settings(self.settings)
            self.overlay.settings = self.settings
            self.overlay._apply_geometry()
            self.overlay.apply_style()
            self._refresh_state()
            if was_running:
                self.start()

    def _persist(self):
        save_settings(self.settings)

    # ---------------- 退出 ----------------
    def _quit(self):
        self.stop()
        save_settings(self.settings)
        self.tray.hide()
        QApplication.quit()

    def closeEvent(self, ev):
        # 关窗口 = 收进托盘，不退出
        ev.ignore()
        self.hide()
        self.tray.showMessage(
            APP_NAME,
            "已最小化到托盘，右键图标可以退出。",
            QSystemTrayIcon.MessageIcon.Information,
            2500,
        )


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setFont(_ui_font())
    app.setQuitOnLastWindowClosed(False)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
