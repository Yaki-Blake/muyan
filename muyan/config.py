from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path

APP_NAME = "幕言"
APP_ID = "MuYan"  # 配置目录用 ASCII，路径里少点麻烦
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_ID
CONFIG_PATH = CONFIG_DIR / "config.json"
LEGACY_CONFIG_PATH = Path(os.environ.get("APPDATA") or Path.home()) / "ARTEX" / "config.json"

# 第一次跑新版：把旧版 ARTEX 的配置（含 API Key、面板尺寸）搬过来，不用重配
if not CONFIG_PATH.exists() and LEGACY_CONFIG_PATH.exists():
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_bytes(LEGACY_CONFIG_PATH.read_bytes())
    except Exception:
        pass

# kind: unisound_realtime = 云知声 WebSocket 真流式（推荐）
#       dashscope_realtime = 阿里云百炼 WebSocket 真流式
#       openai_compat = 走 /audio/transcriptions 分片上传
#       unisound_async = 云知声异步任务式（延迟高，不推荐）
#       local = 本地 faster-whisper
#       mock = 不联网，只验证音频与字幕窗口
PROVIDER_PRESETS = {
    "unisound_realtime": {
        "label": "云知声 U2-ASR（实时流式）",
        "kind": "unisound_realtime",
        "base_url": "wss://maas-api.unisound.com/v2/audio/asr/realtime",
        "model": "u2-asr",
        "key_url": "https://maas.unisound.com/",
        "note": "实测最好用：逐字增量、自动标点断句、延迟亚秒级。Key 在控制台「按量计费 > API Key 管理」里建。",
    },
    "local": {
        "label": "本地 faster-whisper（离线，无需 Key）",
        "kind": "local",
        "base_url": "",
        "model": "small",
        "key_url": "https://github.com/SYSTRAN/faster-whisper",
        "note": "离线、不要 Key。small 模型中文一般（数字容易听错），有 2-4 秒延迟。",
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow",
        "kind": "openai_compat",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "FunAudioLLM/SenseVoiceSmall",
        "key_url": "https://cloud.siliconflow.cn/account/ak",
        "note": "注册有免费额度。SenseVoiceSmall 便宜、中文准，适合长时间挂机。",
    },
    "dashscope_compat": {
        "label": "阿里云百炼（OpenAI 兼容模式）",
        "kind": "openai_compat",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3-asr-flash",
        "key_url": "https://bailian.console.aliyun.com/?apiKey=1",
        "note": "百炼控制台申请 sk- 开头的 Key，新用户有免费额度。",
    },
    "zhipu": {
        "label": "智谱 GLM-ASR",
        "kind": "openai_compat",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-asr",
        "key_url": "https://bigmodel.cn/usercenter/apikeys",
        "note": "",
    },
    "dashscope_realtime": {
        "label": "阿里云百炼（Paraformer 实时流式）",
        "kind": "dashscope_realtime",
        "base_url": "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        "model": "paraformer-realtime-v2",
        "key_url": "https://bailian.console.aliyun.com/?apiKey=1",
        "note": "真流式，延迟低。按音频时长计费。",
    },
    "unisound_async": {
        "label": "云知声 Token Hub（异步任务式）",
        "kind": "unisound_async",
        "base_url": "https://maas-api.unisound.com/v1",
        "model": "u2-asr",
        "key_url": "https://maas.unisound.com/",
        "note": "异步批处理：每段要上传+排队+轮询，延迟 5-15 秒。实时字幕别用这个。",
    },
    "mock": {
        "label": "测试模式（不联网，验证字幕效果）",
        "kind": "mock",
        "base_url": "",
        "model": "",
        "key_url": "",
        "note": "只用来确认系统声音抓得到、字幕窗口显示正常。",
    },
}


@dataclass
class Settings:
    provider: str = "unisound_realtime"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    language: str = "zh"

    # 分片参数（openai_compat / local 用）
    # 4 秒是实测甜点：整句完整，small 模型推理约 2.4 秒，跟得上实时
    chunk_seconds: float = 4.0
    overlap_seconds: float = 1.0

    # 音频
    device_name: str = ""  # 空 = 默认扬声器

    # 字幕外观
    font_size: int = 22
    opacity: float = 1.0           # 历史字段（旧版整窗透明度），新版不再使用
    max_lines: int = 3             # 历史字段（旧版只留最近几行），新版改为无限累积
    width: int = 960
    height: int = 360
    x: int = -1
    y: int = -1
    click_through: bool = False
    show_partial: bool = True
    text_color: str = "#FFFFFF"
    bg_color: str = "#000000"

    # 字幕排版（2026-11 重构：由 overlay.paint_subtitle 统一渲染）
    font_family: str = ""          # 空 = 自动挑一个中文字形好的黑体
    line_spacing: float = 1.30     # 行距倍数
    letter_spacing: float = 0.4    # 字距（像素）
    align: str = "left"            # center / left / right（长段落左对齐更好读）
    padding_x: int = 34
    padding_y: int = 18

    # 描边与底色
    outline: float = 2.2           # 文字描边粗细（像素），0 = 不描边
    outline_color: str = "#000000"
    bg_alpha: float = 0.95         # 面板底色的不透明度（真正决定挡不挡画面）
    corner_radius: int = 18

    # 其它细节
    show_state_dot: bool = True    # 左下角小圆点：绿色=有声音在流，琥珀=安静
    hide_when_empty: bool = True   # 没有字幕时整块透明，不挡画面
    auto_follow: bool = True       # 停在底部时自动跟最新一句；翻上去后不再拽视图

    # 断句：不等服务端 fixed，自己盯着累积文本，停稳了就把标点那段先定稿
    fast_break: bool = True        # 提前断句（更快出标点、更快换行）
    fast_break_ms: int = 320       # 等多久算「停稳」：越小越快，越小越容易切断

    # 内部标记：从旧的「字幕条」升级到「字幕面板」时用过一次
    panel_v2: bool = True

    def preset(self) -> dict:
        return PROVIDER_PRESETS.get(self.provider, PROVIDER_PRESETS["unisound_realtime"])

    def kind(self) -> str:
        return self.preset().get("kind", "openai_compat")

    def resolved_base_url(self) -> str:
        return (self.base_url or self.preset().get("base_url", "")).strip()

    def resolved_model(self) -> str:
        return (self.model or self.preset().get("model", "")).strip()

    def apply_preset_defaults(self):
        p = self.preset()
        self.base_url = p.get("base_url", "")
        self.model = p.get("model", "")


def load_settings() -> Settings:
    if not CONFIG_PATH.exists():
        s = Settings()
        s.apply_preset_defaults()
        return s
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        s = Settings()
        s.apply_preset_defaults()
        return s
    known = {f for f in Settings.__dataclass_fields__}
    data = {k: v for k, v in raw.items() if k in known}
    s = Settings(**data)
    # 旧配置是 200px 高、26pt 的「字幕条」，换面板后给足高度；
    # 字号压到 22（960 宽一屏能看五六句），底色浓度抬一点，长文更好读
    if not raw.get("panel_v2"):
        s.height = max(int(s.height or 0), 360)
        s.font_size = min(int(s.font_size or 26), 22)
        s.bg_alpha = max(float(s.bg_alpha or 0.85), 0.95)
        # 长段落左对齐更好读；旧配置里的 "center" 是旧默认值，不是用户主动选的
        if str(s.align or "").lower() in ("", "center"):
            s.align = "left"
        s.panel_v2 = True
    if not s.base_url or not s.model:
        p = s.preset()
        s.base_url = s.base_url or p.get("base_url", "")
        s.model = s.model or p.get("model", "")
    return s


def save_settings(s: Settings):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(asdict(s), ensure_ascii=False, indent=2), encoding="utf-8"
    )

