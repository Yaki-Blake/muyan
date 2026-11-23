from __future__ import annotations

import json
import queue
import threading
import time

import numpy as np

from .base import ASRProvider
from .fastseg import FastSegmenter

SR = 16000

# 云知声要的是 zh-CN 这种带地区的码，幕言内部用的是 zh 这种短码。
LANG_MAP = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-CN": "zh-CN",
    "en": "en-US",
    "en-us": "en-US",
    "en-US": "en-US",
    "ja": "ja-JP",
    "ja-JP": "ja-JP",
    "ko": "ko-KR",
    "ko-KR": "ko-KR",
    "fr": "fr-FR",
    "fr-FR": "fr-FR",
    "de": "de-DE",
    "de-DE": "de-DE",
    "es": "es-MX",
    "es-MX": "es-MX",
    "ru": "ru-RU",
    "ru-RU": "ru-RU",
    "pt": "pt-BR",
    "pt-BR": "pt-BR",
    "it": "it-IT",
    "it-IT": "it-IT",
    "ar": "ar-SA",
    "ar-SA": "ar-SA",
    "id": "id-ID",
    "id-ID": "id-ID",
    "tr": "tr-TR",
    "tr-TR": "tr-TR",
    "vi": "vi-VN",
    "vi-VN": "vi-VN",
    "th": "th-TH",
    "th-TH": "th-TH",
}


def _lang(code: str) -> str:
    code = (code or "").strip()
    return LANG_MAP.get(code) or LANG_MAP.get(code.lower()) or "zh-CN"


class UnisoundRealtimeASR(ASRProvider):
    """云知声 U2-ASR 实时语音转写（WebSocket 双向流式）。

    协议：start -> 持续二进制 PCM -> end；服务端回 variable（累积中间文本）/
    fixed（定稿一句）以及 end=true（本轮说完，会话继续）。

    这一版专门修「用着用着不再出字幕」的几个根因：
      1. 发送线程永不退出。旧实现一次发送失败就 return，线程死掉后连接线程
         虽然重连成功、界面还写着「正在识别」，却再也没人喂音频 —— 字幕永久静止。
         现在发送失败只关闭当前连接、交给连接线程重连，发送线程一直活着。
      2. 读循环用 1 秒短超时轮询，超时只是「暂时没消息」，不再当成断线报错。
         只有「我们一直在发音频、30 秒里服务端一个字都没回」才判定连接半死并重建。
      3. 网络类异常走 warn（可自愈），只有鉴权/缺依赖这类才是 error（真停）。
      4. 每轮 end 之后重开一轮时，用「轮次号」把重开前抓到的旧音频丢掉，
         避免把上一轮的声音灌进新会话，造成重复字或错位。
    """

    #: 发送队列容量（100ms 一块 → 50 块约 5 秒）。满了丢最旧，保证音频新鲜。
    SEND_QUEUE_SIZE = 50
    #: WebSocket 读超时，短一点才能及时响应 stop / 心跳判断
    READ_TIMEOUT = 1.0
    #: 一直在发音频却这么久没有任何回包 → 认为连接半死，主动重建
    IDLE_RECONNECT = 30.0
    #: 真的有人声在说话、却连续这么多块（100ms/块 ≈ 5 秒）没有任何回包
    #: → 连接已经死了（切视频/换音频源之后很常见），马上重建，别干等 30 秒
    LOUD_BLOCKS_BEFORE_RECONNECT = 50
    #: 重连退避
    RECONNECT_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0)
    #: 同类 warn 的最小间隔，避免刷屏
    WARN_INTERVAL = 15.0

    def __init__(self, settings, parent=None):
        super().__init__(settings, parent)
        self._alive = threading.Event()
        self._ws = None
        self._ws_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._send_q: "queue.Queue[tuple[int, bytes]]" = queue.Queue(
            maxsize=self.SEND_QUEUE_SIZE
        )
        self._session_active = False
        self._session_gen = 0
        self._sender: threading.Thread | None = None
        self._connect_thread: threading.Thread | None = None

        self._last_partial = ""
        self._last_msg_at = 0.0
        self._last_loud_at = 0.0
        self._sent_since_msg = 0
        self._loud_since_msg = 0
        self._last_warn_at = 0.0
        self._fatal = False
        self._sessions = 0
        self._dropped = 0

        # 提前断句：服务端的 fixed 要等 VAD 判停，慢半拍；
        # 这里自己盯累积文本，停稳了就先把标点那段交出去。
        self._seg_lock = threading.Lock()
        self._seg = FastSegmenter(
            hold_ms=getattr(settings, "fast_break_ms", 320),
            enabled=getattr(settings, "fast_break", True),
        )

    # ---------- 生命周期 ----------

    def start(self):
        if not self._require_key():
            return
        self._running = True
        self._fatal = False
        self._last_partial = ""
        self._session_active = False
        self._session_gen = 0
        self._sent_since_msg = 0
        self._loud_since_msg = 0
        self._last_msg_at = time.monotonic()
        self._sessions = 0
        self._dropped = 0

        with self._seg_lock:
            self._seg.reset()

        self._clear_queue()
        self._alive.set()

        self._sender = threading.Thread(
            target=self._send_loop, daemon=True, name="UnisoundSender"
        )
        self._sender.start()

        self._connect_thread = threading.Thread(
            target=self._connect_loop, daemon=True, name="UnisoundConnect"
        )
        self._connect_thread.start()
        self.status.emit("正在连接云知声…")

    def restart_connection(self, reason: str = ""):
        """外部发现音频源变了（切视频、换播放器、换输出设备）：主动丢掉这次连接。

        不关掉整条链路，只关闭当前 ws，连接线程会按退避重建并重新发 start，
        字幕很快就能接上，面板里的历史也不受影响。
        """
        with self._seg_lock:
            self._seg.reset()
        self._last_partial = ""
        self._loud_since_msg = 0
        self._session_active = False
        with self._ws_lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def stop(self):
        self._running = False
        self._alive.clear()
        self._session_active = False
        self._clear_queue()

        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                with self._send_lock:
                    ws.send(json.dumps({"type": "end"}))
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass

        sender = self._sender
        if sender and sender.is_alive() and sender is not threading.current_thread():
            try:
                sender.join(timeout=1.0)
            except Exception:
                pass

    def _clear_queue(self):
        while True:
            try:
                self._send_q.get_nowait()
            except queue.Empty:
                break

    def _sleep_alive(self, seconds: float):
        end = time.monotonic() + seconds
        while self._alive.is_set() and time.monotonic() < end:
            time.sleep(0.1)

    def _warn(self, text: str):
        now = time.monotonic()
        if now - self._last_warn_at < self.WARN_INTERVAL:
            return
        self._last_warn_at = now
        self.warn.emit(text)

    # ---------- 连接（带自愈重连） ----------

    def _url(self) -> str:
        base = self.settings.resolved_base_url().strip()
        if not base:
            base = "wss://maas-api.unisound.com/v2/audio/asr/realtime"
        if base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        elif base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif not base.startswith(("ws://", "wss://")):
            base = "wss://" + base
        if "model=" not in base:
            sep = "&" if "?" in base else "?"
            base = f"{base}{sep}model={self.settings.resolved_model() or 'u2-asr'}"
        return base

    def _connect_loop(self):
        attempt = 0
        while self._alive.is_set() and not self._fatal:
            served = self._serve_once()
            if not self._alive.is_set() or self._fatal:
                break
            if served:
                # 正常收尾（stop 调用），不再重连
                break

            delay = self.RECONNECT_DELAYS[min(attempt, len(self.RECONNECT_DELAYS) - 1)]
            attempt += 1
            self._warn(f"云知声连接断开，{delay:.0f} 秒后自动重连（第 {attempt} 次）")
            self.status.emit("云知声断线，正在重连…")
            self._sleep_alive(delay)

    def _serve_once(self) -> bool:
        """建立连接并服务到断开。返回 True 表示是正常停止，不需要重连。"""
        try:
            import websocket
        except Exception as e:
            self.error.emit(f"缺少 websocket-client：{e}")
            self._fatal = True
            self._running = False
            return True

        url = self._url()
        headers = [f"Authorization: Bearer {self.settings.api_key.strip()}"]
        try:
            ws = websocket.create_connection(
                url, header=headers, timeout=15, enable_multithread=True
            )
        except Exception as e:
            msg = str(e)
            if "401" in msg:
                self.error.emit("云知声鉴权失败（401）：API Key 无效或没开通 U2-ASR。")
                self._fatal = True
                self._running = False
                return True
            if "429" in msg:
                self._warn("云知声限流（429），稍后自动重试。")
            else:
                self._warn(f"连接云知声失败：{msg}")
            return False

        try:
            ws.settimeout(self.READ_TIMEOUT)
        except Exception:
            pass

        with self._ws_lock:
            self._ws = ws
        self._last_msg_at = time.monotonic()
        self._sent_since_msg = 0
        self._last_partial = ""
        if self._sessions <= 1:
            with self._seg_lock:
                self._seg.reset()

        if not self._start_session():
            self._close_ws(ws)
            return False

        self.status.emit("云知声已连接 · 正在识别")
        try:
            self._read_loop(ws)
        finally:
            self._close_ws(ws)
            with self._ws_lock:
                self._ws = None
            self._session_active = False

        return not self._alive.is_set()

    def _close_ws(self, ws):
        try:
            ws.close()
        except Exception:
            pass

    # ---------- 会话 ----------

    def _start_session(self) -> bool:
        """开一轮识别。返回是否成功。"""
        ws = self._ws
        if ws is None:
            return False
        frame = {
            "type": "start",
            "data": {
                "format": "pcm",
                "sample": "16k",
                # 即使不显示半句也要 variable：提前断句靠它拿累积文本
                "variable": "true",
                "punctuation": "true",
                "post_proc": "true",
                "language": _lang(self.settings.language),
            },
        }
        self._session_gen += 1
        try:
            with self._send_lock:
                ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception as e:
            self._warn(f"发送 start 失败：{e}")
            self._session_active = False
            return False
        self._session_active = True
        self._sessions += 1
        self._loud_since_msg = 0   # 新的一轮，重新算「说了多久没回包」
        return True

    # ---------- 收 ----------

    def _read_loop(self, ws):
        while self._alive.is_set():
            try:
                msg = ws.recv()
            except Exception as e:
                if not self._alive.is_set():
                    return
                if self._is_timeout(e):
                    # 只是这一秒没有消息（安静时服务端本来就不说话），继续等
                    if self._no_response():
                        self._warn("一直在说话但服务端没回包，重建连接…")
                        return
                    if self._stalled():
                        self._warn("云知声 30 秒没有任何回包，重建连接…")
                        return
                    continue
                self._warn(f"云知声流式连接中断：{e}")
                return

            if not msg:
                if self._no_response():
                    self._warn("一直在说话但服务端没回包，重建连接…")
                    return
                if self._stalled():
                    self._warn("云知声连接没有数据回包，重建连接…")
                    return
                continue

            self._last_msg_at = time.monotonic()
            self._sent_since_msg = 0
            self._loud_since_msg = 0

            if isinstance(msg, bytes):
                continue
            try:
                evt = json.loads(msg)
            except Exception:
                continue
            if isinstance(evt, dict) and self._on_event(evt):
                return

    @staticmethod
    def _is_timeout(e: Exception) -> bool:
        if isinstance(e, TimeoutError):
            return True
        name = type(e).__name__.lower()
        if "timeout" in name:
            return True
        return "timed out" in str(e).lower()

    def _no_response(self) -> bool:
        """明明有人在说话，却连续 6 秒一块回包都没有 → 这条连接已经废了。

        切视频、换音频源、服务端把上一轮的会话丢掉的场景里，客户端还在照常发
        音频，但服务端不再回任何东西。旧版要等满 30 秒才重建，于是表现成
        「换了视频就再也不出字幕」。这里提前到 6 秒。
        """
        return self._loud_since_msg >= self.LOUD_BLOCKS_BEFORE_RECONNECT

    def _stalled(self) -> bool:
        if self._sent_since_msg < 10:
            return False
        if (time.monotonic() - self._last_msg_at) <= self.IDLE_RECONNECT:
            return False
        # 只有在「最后一条回包之后还说过话」时才判定半死，
        # 安静时长时间没有回包是正常的，不要乱重连。
        return self._last_loud_at > self._last_msg_at

    def _on_event(self, evt: dict) -> bool:
        """处理一条服务端消息。返回 True 表示要断开连接。"""
        base = evt.get("base_resp") or {}
        code = base.get("status_code", 0)
        if code not in (0, None):
            msg = base.get("status_msg", "未知错误")
            if code == 203002:
                # 连接后没收到音频：不是致命错误，重开一轮继续等
                self.status.emit("等待音频…")
                self._session_active = False
                return not self._start_session()
            self._warn(f"云知声错误 {code}：{msg}")
            return True

        text = (evt.get("text") or "").strip()
        kind = evt.get("type") or ""
        ended = bool(evt.get("end"))

        if text:
            if kind == "fixed":
                pieces = self._seg_call("take_final", text)
            else:
                pieces = self._seg_call("push", text)
            self._emit_pieces(pieces)
            self._emit_partial()

        if ended:
            # 这一轮说完了：把尾巴交出去，清临时行，立刻开新一轮，连接保持
            self._emit_pieces(self._seg_call("take_end"))
            self._last_partial = ""
            self.partial.emit("")
            self._session_active = False
            if not self._start_session():
                return True
        return False

    # ---------- 提前断句 ----------

    def _seg_call(self, name: str, *args) -> list[str]:
        """线程安全地调分段器（读线程和发送线程都会进来）。"""
        try:
            with self._seg_lock:
                return getattr(self._seg, name)(*args) or []
        except Exception:
            return []

    def _emit_pieces(self, pieces: list[str]):
        for p in pieces:
            p = (p or "").strip()
            if p:
                self.final.emit(p)

    def _emit_partial(self):
        """把还没定稿的尾巴同步给界面（不显示半句时就不发）。"""
        if not self.settings.show_partial:
            return
        with self._seg_lock:
            tail = self._seg.tail()
        if tail != self._last_partial:
            self._last_partial = tail
            self.partial.emit(tail)

    # ---------- 发 ----------

    def _send_loop(self):
        """独立发送线程：从队列取 PCM 发出去。feed() 只入队，永不阻塞。

        这条线程必须活得比任何一次连接都久：断线、重连、重开会话都不许退出。
        """
        while self._alive.is_set():
            try:
                item = self._send_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if not self._alive.is_set():
                break
            try:
                gen, pcm = item
            except Exception:
                continue
            if gen != self._session_gen or not self._session_active:
                # 这一块音频属于上一轮会话，丢掉，避免灌进新会话造成重复
                continue
            ws = self._ws
            if ws is None:
                continue
            try:
                with self._send_lock:
                    ws.send_binary(pcm)
                self._sent_since_msg += 1
                # 这块里有声音就记一笔：连着 6 秒有声却没回包，说明连接废了
                if self._is_loud(pcm):
                    self._loud_since_msg += 1
            except Exception as e:
                # 只废掉这次连接，线程继续跑，交给连接线程重连
                self._session_active = False
                self._warn(f"发送音频失败：{e}（将自动重连）")
                try:
                    ws.close()
                except Exception:
                    pass

    @staticmethod
    def _is_loud(pcm: bytes, threshold: int = 300) -> bool:
        """粗判这一块里有没有人声：只看峰值，别做重活。"""
        try:
            a = np.frombuffer(pcm, dtype=np.int16)
        except Exception:
            return False
        if a.size == 0:
            return False
        return int(np.abs(a).max()) >= threshold

    def feed(self, pcm: bytes):
        """只入队，不碰网络。队列满时丢最旧的，保证手上永远是最新音频。"""
        if not self._alive.is_set() or not pcm:
            return
        if self._is_loud(pcm):
            self._last_loud_at = time.monotonic()
        item = (self._session_gen, pcm)
        try:
            self._send_q.put_nowait(item)
            self._tick_seg()
            return
        except queue.Full:
            pass
        self._dropped += 1
        try:
            self._send_q.get_nowait()
        except queue.Empty:
            pass
        try:
            self._send_q.put_nowait(item)
        except queue.Full:
            pass
        self._tick_seg()

    def _tick_seg(self):
        """每块音频（约 100ms）来一次：文本停稳了就切，不必等新消息到。"""
        pieces = self._seg_call("tick")
        if pieces:
            self._emit_pieces(pieces)
            self._emit_partial()
