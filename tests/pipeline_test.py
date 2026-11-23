"""整条链路冒烟测试：测试模式启动 → 采集 → 看门狗杀线程 → 自动恢复 → 停止。"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

from muyan.app import MainWindow
import muyan.app as appmod

appmod.save_settings = lambda s: None  # 测试期间绝不写用户的真实配置

app = QApplication(sys.argv)
w = MainWindow()
w.settings.provider = "mock"
w.settings.apply_preset_defaults()
w.settings.width, w.settings.height = 900, 200
w.overlay._apply_geometry()
w.show()

w.start()


def spin(sec: float):
    t0 = time.monotonic()
    while time.monotonic() - t0 < sec:
        app.processEvents()
        time.sleep(0.02)


spin(8)
print("running:", w._running)
print("status:", w.lb_status.text())
print("capture frames:", None if w.capture is None else w.capture.frames)
print("overlay text:", w.overlay.current_text().replace("\n", " | ")[:120])
print("restart count:", w._restart_count)

# 模拟采集线程死掉
cap = w.capture
print("kill capture:", cap is not None)
if cap:
    cap.stop()
spin(6)
print("capture rebuilt:", w.capture is not None and w.capture is not cap)
print("capture alive:", bool(w.capture and w.capture.is_alive()))
print("capture frames after:", None if w.capture is None else w.capture.frames)
print("status after:", w.lb_status.text())
print("restart count after:", w._restart_count)

spin(4)
print("overlay text after:", w.overlay.current_text().replace("\n", " | ")[:120])

w.stop()
print("stopped, running:", w._running, "provider:", w.provider, "capture:", w.capture)
w.tray.hide()
w.overlay.close()
