from __future__ import annotations

import queue
import threading
import time
import warnings
from typing import Optional

import numpy as np

SR = 16000

# soundcard 在重开设备时会喊 data discontinuity，属于正常现象，别刷屏
try:
    from soundcard import SoundcardRuntimeWarning

    warnings.filterwarnings("ignore", category=SoundcardRuntimeWarning)
except Exception:
    pass


def list_loopback_devices() -> list[str]:
    """列出所有可做环回录音的播放设备名。"""
    try:
        import soundcard as sc
    except Exception:
        return []
    names: list[str] = []
    try:
        for spk in sc.all_speakers():
            names.append(spk.name)
    except Exception:
        pass
    return names


class LoopbackCapture(threading.Thread):
    """抓系统正在播放的声音（WASAPI loopback），输出 16k 单声道 int16 的定长块。

    队列满时丢最旧的一块：宁可丢一点音频，也不要越积越多、最后一次性喷出来。

    线程自己负责自愈：录音句柄异常（设备被拔、蓝牙耳机切换、系统睡眠唤醒）
    都会被外层循环捕获，退避后重新打开设备，而不是直接把线程结束掉。
    上层用 seconds_since_data() 判断是否真的断流。
    """

    def __init__(
        self,
        out_queue,
        device_name: str = "",
        samplerate: int = SR,
        block_ms: int = 100,
    ):
        super().__init__(daemon=True, name="LoopbackCapture")
        self.out_queue = out_queue
        self.device_name = device_name
        self.samplerate = samplerate
        self.block_ms = block_ms
        self._stop = threading.Event()
        self.error: Optional[str] = None
        self.device_label: str = ""
        self.dropped = 0
        self.reopens = 0
        self.frames = 0
        self.started_at = time.monotonic()
        self.last_data_at = 0.0
        self.last_sound_at = 0.0
        self.silence_reopens = 0
        self._zero_since = 0.0
        self._last_reopen_at = 0.0
        # 只在「听到过声音之后又突然全零」时才重开；安静时重开一次就收手，
        # 不然用户暂停视频期间会每 15 秒重开一次
        self._silent_reopen_armed = True
        #: 连续这么久收到「纯数字零」就认为环回句柄跟丢了，重开设备
        self.ZERO_SECONDS_BEFORE_REOPEN = 2.5
        #: 两次「因为静音而重开」之间的最小间隔，避免休息时反复重开
        self.REOPEN_COOLDOWN = 10.0

    def stop(self):
        self._stop.set()

    def seconds_since_data(self) -> float:
        """距离上一块音频多久（还没收到过数据时从启动时刻算）。"""
        ref = self.last_data_at or self.started_at
        return max(0.0, time.monotonic() - ref)

    def seconds_since_sound(self) -> float:
        """距离上一次「不是纯静音」的音频块多久。"""
        ref = self.last_sound_at or self.started_at
        return max(0.0, time.monotonic() - ref)

    def _pick_device(self, sc):
        if self.device_name:
            try:
                return sc.get_microphone(id=self.device_name, include_loopback=True)
            except Exception:
                pass
        spk = sc.default_speaker()
        if spk is None:
            raise RuntimeError("系统没有可用的播放设备")
        return sc.get_microphone(id=str(spk.name), include_loopback=True)

    def _push(self, pcm: bytes):
        try:
            self.out_queue.put_nowait(pcm)
            return
        except queue.Full:
            pass
        # 队列满：先扔最旧的一块，再塞新的，保证手上永远是最新的音频
        self.dropped += 1
        try:
            self.out_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self.out_queue.put_nowait(pcm)
        except queue.Full:
            pass

    def run(self):
        try:
            import soundcard as sc
        except Exception as e:  # pragma: no cover
            self.error = f"缺少 soundcard 库：{e}"
            return

        frames = max(1, int(self.samplerate * self.block_ms / 1000))
        backoff = 0.2

        while not self._stop.is_set():
            try:
                mic = self._pick_device(sc)
                name = getattr(mic, "name", "") or self.device_name or "默认扬声器"
                self.device_label = name
                with mic.recorder(samplerate=self.samplerate, channels=1) as rec:
                    backoff = 0.2
                    self.error = None
                    next_device_check = time.monotonic() + 2.0
                    while not self._stop.is_set():
                        data = rec.record(numframes=frames)
                        if data is None or len(data) == 0:
                            continue
                        mono = data.mean(axis=1) if data.ndim > 1 else data
                        pcm = np.clip(mono, -1.0, 1.0)
                        i16 = (pcm * 32767.0).astype(np.int16)
                        self._push(i16.tobytes())
                        self.frames += 1
                        now = time.monotonic()
                        self.last_data_at = now

                        peak = int(np.abs(i16).max()) if i16.size else 0
                        if peak > 0:
                            self.last_sound_at = now
                            self._zero_since = 0.0
                            self._silent_reopen_armed = True
                        else:
                            # 不是「安静」，是「一个采样都不是非零」：环回句柄跟丢了
                            # （切视频、换播放器、换输出设备之后最典型的症状）
                            if self._zero_since <= 0.0:
                                self._zero_since = now
                            elif (
                                self._silent_reopen_armed
                                and now - self._zero_since >= self.ZERO_SECONDS_BEFORE_REOPEN
                                and now - self._last_reopen_at >= self.REOPEN_COOLDOWN
                            ):
                                self._last_reopen_at = now
                                self._zero_since = 0.0
                                self._silent_reopen_armed = False
                                self.silence_reopens += 1
                                self.reopens += 1
                                self.error = "音频流断了（切换播放源后常见），正在重开采集…"
                                break

                        # 默认输出设备被换掉（插拔耳机、蓝牙切换）→ 主动重开
                        if not self.device_name and now >= next_device_check:
                            next_device_check = now + 2.0
                            try:
                                cur = sc.default_speaker()
                            except Exception:
                                cur = None
                            if cur is not None and getattr(cur, "name", "") != self.device_label:
                                break
            except Exception as e:
                if self._stop.is_set():
                    break
                self.reopens += 1
                if self.error is None:
                    self.error = f"录音中断：{e}（正在自动恢复…）"
                self._stop.wait(backoff)
                backoff = min(2.0, backoff * 2)


class AudioPump(threading.Thread):
    """把采集队列里的 PCM 持续喂给识别器。

    单独开线程的原因：provider.feed() 对实时接口是一次网络发送。
    放在 GUI 线程里做，网络一慢整个界面就卡住，队列随之积压，
    恢复后一次性喷出一大堆字幕——这正是之前的症状。
    """

    def __init__(self, audio_q, get_provider, on_level=None, on_warn=None):
        super().__init__(daemon=True, name="AudioPump")
        self.audio_q = audio_q
        self.get_provider = get_provider
        self.on_level = on_level
        self.on_warn = on_warn
        self._stop = threading.Event()
        self.frames = 0
        self.last_feed_at = 0.0

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                pcm = self.audio_q.get(timeout=0.1)
            except queue.Empty:
                continue
            if self._stop.is_set():
                break
            provider = self.get_provider()
            if provider is None:
                continue
            try:
                provider.feed(pcm)
            except Exception as e:
                # 单块发送失败只提示，不中断整条链路
                if self.on_warn:
                    self.on_warn(str(e))
                continue
            self.frames += 1
            self.last_feed_at = time.monotonic()
            if self.on_level and len(pcm) >= 2:
                a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                if a.size:
                    self.on_level(float(np.sqrt(np.mean(a * a))))
