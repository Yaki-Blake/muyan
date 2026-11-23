"""幕言 —— 抓系统声音，实时显示字幕。

用法：
    python -m muyan            # 启动
    python -m muyan --check    # 只做环境自检
"""

from __future__ import annotations

import sys


def _check() -> int:
    ok = True
    print("== 幕言 环境自检 ==")
    print(f"Python: {sys.version.split()[0]}")

    try:
        import PySide6

        print(f"PySide6: {PySide6.__version__}  OK")
    except Exception as e:
        print(f"PySide6: 缺失 ({e})")
        ok = False

    try:
        import numpy

        print(f"numpy: {numpy.__version__}  OK")
    except Exception as e:
        print(f"numpy: 缺失 ({e})")
        ok = False

    try:
        import soundcard as sc

        spk = sc.default_speaker()
        print(f"soundcard: OK  默认扬声器 = {spk.name}")
        print(f"可环回设备数: {len(sc.all_speakers())}")
    except Exception as e:
        print(f"soundcard: 不可用 ({e})")
        ok = False

    try:
        import requests

        print(f"requests: {requests.__version__}  OK")
    except Exception as e:
        print(f"requests: 缺失 ({e})")
        ok = False

    try:
        import websocket  # noqa: F401

        print("websocket-client: OK")
    except Exception:
        print("websocket-client: 缺失（只有百炼流式模式需要）")

    try:
        import faster_whisper  # noqa: F401

        print("faster-whisper: OK（本地离线模式可用）")
    except Exception:
        print("faster-whisper: 未安装（本地离线模式不可用，可选）")

    print("\n结论:", "全部就绪" if ok else "有必需依赖缺失，先 pip install -r requirements.txt")
    return 0 if ok else 1


def main():
    # 打包成 exe 后 stdout 是 GBK，中文自检输出会变乱码，统一改成 utf-8
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if "--check" in sys.argv:
        raise SystemExit(_check())
    from .app import main as run

    run()


if __name__ == "__main__":
    main()
