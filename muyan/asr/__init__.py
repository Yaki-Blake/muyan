from __future__ import annotations

from ..config import Settings
from .base import ASRProvider


def build_provider(settings: Settings, parent=None) -> ASRProvider:
    kind = settings.kind()
    if kind == "unisound_realtime":
        from .unisound_realtime import UnisoundRealtimeASR

        return UnisoundRealtimeASR(settings, parent)
    if kind == "dashscope_realtime":
        from .dashscope_realtime import DashScopeRealtimeASR

        return DashScopeRealtimeASR(settings, parent)
    if kind == "unisound_async":
        from .unisound_async import UnisoundAsyncASR

        return UnisoundAsyncASR(settings, parent)
    if kind == "local":
        from .local_whisper import LocalWhisperASR

        return LocalWhisperASR(settings, parent)
    if kind == "mock":
        from .mock import MockASR

        return MockASR(settings, parent)
    from .openai_compat import OpenAICompatASR

    return OpenAICompatASR(settings, parent)


__all__ = ["ASRProvider", "build_provider"]
