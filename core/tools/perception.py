"""屏幕识别辅助：实时帧获取、OCR provider 解析与内部识别调用记账。"""

from __future__ import annotations

import time
from typing import Any

from ..live_screen import LiveFrame


DEFAULT_OCR_PROMPT = (
    "你是 OCR，逐字识别图片中的文字/数字/验证码，只输出识别结果，不要解释"
)


def _ocr_caption_settings(app_context: Any, event: Any) -> dict:
    """读取 AstrBot provider_settings（图片描述 Provider 等）。"""
    if app_context is None:
        return {}
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    try:
        try:
            cfg = app_context.get_config(umo=umo)
        except TypeError:
            cfg = app_context.get_config()
    except Exception:  # noqa: BLE001 - 配置读取失败按无配置处理
        return {}
    try:
        settings = cfg.get("provider_settings", {}) or {}
    except Exception:  # noqa: BLE001
        settings = getattr(cfg, "provider_settings", {}) or {}
    return settings if isinstance(settings, dict) else {}


def _resolve_ocr_provider(
    service: Any, app_context: Any, event: Any
) -> tuple[str, str]:
    """按「插件配置 → AstrBot 图片描述 Provider → 当前会话 Provider」选 OCR provider。"""
    direct = str(service.config.perception.ocr_provider_id or "").strip()
    if direct:
        return direct, "插件配置 perception.ocr_provider_id"
    settings = _ocr_caption_settings(app_context, event)
    caption = str(settings.get("default_image_caption_provider_id") or "").strip()
    if caption:
        return caption, "AstrBot 图片描述 Provider"
    if app_context is None:
        return "", ""
    try:
        provider = app_context.get_using_provider(
            umo=str(getattr(event, "unified_msg_origin", "") or "")
        )
    except Exception:  # noqa: BLE001 - 获取当前 provider 失败按未配置处理
        provider = None
    if provider is None:
        return "", ""
    provider_id = ""
    try:
        provider_id = str(provider.meta().id or "")
    except Exception:  # noqa: BLE001
        provider_config = getattr(provider, "provider_config", {}) or {}
        try:
            provider_id = str(provider_config.get("id") or "")
        except Exception:  # noqa: BLE001
            provider_id = ""
    return provider_id, "当前会话 Provider"


async def _live_frame(
    service: Any, wait_fresh_ms: int
) -> LiveFrame:
    """取实时帧：优先 watcher，未启用/未初始化时退化为即时抓一张。"""
    config = service.config
    watcher = getattr(service, "live_screen", None)
    wait = max(0, int(wait_fresh_ms or 0))
    # 仅任务期间 watcher 会运行；停止时走一次性抓帧，不自动启动常驻协程。
    if (
        watcher is not None
        and bool(config.perception.live_screen_enabled)
        and getattr(watcher, "running", False)
    ):
        if wait > 0:
            return await watcher.ensure_fresh(
                wait, timeout=max(3.0, wait / 1000.0 + 3.0)
            )
        return await watcher.latest(0)
    data, path, meta = await service.capture()
    meta = meta or {}
    foreground = ""
    activity = ""
    try:
        foreground, activity = await service.actions.foreground()
    except Exception:  # noqa: BLE001 - 前台信息失败不影响帧
        pass
    return LiveFrame(
        data=data,
        path=path,
        ts=time.time(),
        method=str(meta.get("method") or "png"),
        foreground=str(meta.get("foreground") or foreground or ""),
        activity=str(meta.get("activity") or activity or ""),
    )


def _frame_mime(frame: LiveFrame) -> str:
    return "image/jpeg" if str(frame.method).lower() in {"jpeg", "jpg"} else "image/png"


def _record_internal_usage(
    service: Any,
    response: Any,
    event: Any,
    source: str,
    prompt: str,
    completion: str,
    image_count: int,
) -> None:
    """内部 llm_generate 调用记账（失败不影响工具结果）。"""
    if service is None:
        return
    try:
        service.record_internal_usage(
            getattr(response, "usage", None),
            source=source,
            event=event,
            prompt_text=prompt,
            completion_text=completion,
            image_count=image_count,
        )
    except Exception:  # noqa: BLE001 - 记账失败不影响工具结果
        pass
