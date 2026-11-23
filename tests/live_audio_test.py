"""真机音频链路自检：真放一段系统声音，看环回能不能抓到非零音频。

只验证采集这一环（不联网、不消耗 API 额度），确认新加的「全零检测」没有
误伤正常录音。
"""
import glob
import queue
import sys
import threading
import time

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))

import numpy as np

from muyan.audio import LoopbackCapture

wavs = glob.glob(r"C:\Windows\Media\*.wav")
if not wavs:
    print("SKIP: 系统里没有示例 wav")
    sys.exit(0)
wav = next((w for w in wavs if "notify" in w.lower() or "chimes" in w.lower()), wavs[0])
print("播放:", wav)


def play():
    import winsound
    try:
        winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_NODEFAULT)
    except Exception as e:
        print("播放失败:", e)


q: "queue.Queue" = queue.Queue(maxsize=200)
cap = LoopbackCapture(q, device_name="", block_ms=100)
cap.start()
time.sleep(0.4)
t = threading.Thread(target=play, daemon=True)
t.start()

peaks = []
deadline = time.time() + 6.0
while time.time() < deadline:
    try:
        pcm = q.get(timeout=0.2)
    except queue.Empty:
        continue
    a = np.frombuffer(pcm, dtype=np.int16)
    if a.size:
        peaks.append(int(np.abs(a).max()))
    if peaks and max(peaks) > 500:
        break

cap.stop()
time.sleep(0.2)

print("采集块数:", cap.frames, "重开次数:", cap.reopens, "零值重开:", cap.silence_reopens)
print("峰值:", max(peaks) if peaks else 0)

ok = bool(peaks) and max(peaks) > 500 and cap.silence_reopens == 0
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
