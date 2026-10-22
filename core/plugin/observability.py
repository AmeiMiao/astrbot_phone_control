"""插件可观测性与集成 mixin：监控与资料管理 API、记忆插件同步、token 记账、自主任务模型供应商。"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.utils.astrbot_path import (
    get_astrbot_config_path,
    get_astrbot_data_path,
    get_astrbot_plugin_path,
)

try:  # AstrBot 内置命令的写法：一个 GreedyStr 参数吃下命令后的全部文本
    from astrbot.core.star.filter.command import GreedyStr
except ImportError:  # pragma: no cover - AstrBot 版本或单测环境没有该类型
    GreedyStr = str  # type: ignore[assignment,misc]

from ..actions import DeviceActions
from ..adb import AdbClient
from ..app_memory import AppMemoryStore
from ..apps import (
    KNOWN_APP_LABELS,
    UNKNOWN_APP_HINT_TEMPLATE,
    app_label_for_package,
    format_preferred_apps,
    match_preferred_app,
    parse_package_list,
    resolve_app,
    resolve_package,
)
from ..backends import BackendManager
from ..browse import BrowseSession
from ..chat_memory import ChatMemory
from ..config import (
    UNCONFIGURED_MESSAGE,
    PhoneConfig,
    require_configured,
)
from ..config_apply import ACTION_START, ACTION_STOP, build_apply_plan
from ..diary import PhoneDiary
from ..interactions import (
    KIND_COMMENT,
    KIND_PASSIVE_REPLY,
    KIND_POST,
    InteractionStore,
)
from ..learning import LearningError, LearningStore
from ..novelty import NoveltyStore
from ..live_screen import LiveScreenWatcher
from ..memory_sync import (
    COMPANION_PLUGIN_NAME,
    COMPANION_SOURCE_PLUGIN,
    LEARNING_MEMORY_TYPE,
    LIVINGMEMORY_PLUGIN_NAME,
    build_bot_action_payload,
    wants_companion,
    wants_livingmemory,
)
from ..errors import (
    ActionFailedError,
    AdbNotConfiguredError,
    AppNotInstalledError,
    PhoneControlError,
)
from ..models import BrowseRequest, BrowseResult, DeviceInfo, ScreenSnapshot, UiNode
from ..continuous import (
    build_continuous_note,
    next_run_delay,
    normalize_continuous_apps,
    normalize_gap_seconds,
)
from ..random_play import (
    build_play_note,
    build_random_play_note,
    next_random_delay,
    should_play,
)
from .. import verification as verification_mod
from ..device_gate import (
    DeviceExecutionGate,
    ExecutionCancelled,
    GateError,
)
from ..screenshot_cleanup import cleanup_directory
from ..page_content import (
    PAGE_ACTION_WINDOW_MAX_SECONDS,
    PAGE_COMPACT_BODY_CHARS,
    PAGE_COMPACT_MAX_TEXT_CHARS,
    PAGE_FLOW_LIMIT,
    PAGE_MAX_TEXT_CHARS,
    PageTrafficSnapshot,
    build_page_traffic,
)
from ..traffic import extract_share_links, infer_flow_hints, mask_url, render_traffic
from ..traffic_capture import TrafficCapture
from ..share_policy import (
    classify_session_umo,
    is_recent_interaction,
    is_share_window,
    share_window_text,
)
from ..scheduler import (
    CONTINUOUS_JOB_ORIGIN,
    JOB_ORIGIN as AUTO_BROWSE_JOB_ORIGIN,
    AutoBrowseError,
    AutoBrowseScheduler,
    BackgroundTaskError,
    create_background_task,
    continuous_job_running,
    create_continuous_task,
    delete_plugin_jobs,
    list_background_tasks,
    list_plugin_jobs,
    validate_background_instruction,
)
from ..token_quota import (
    PlayBudget,
    PlayBudgetRegistry,
    TokenQuota,
    estimate_fallback_usage,
    estimate_messages_tokens,
    estimate_tokens,
    usage_breakdown,
    usage_is_empty,
)
from .. import usage_audit
from ..agent_usage_patch import (
    USAGE_EXTRA as AGENT_USAGE_EXTRA,
    install_agent_usage_capture,
    uninstall_agent_usage_capture,
)
from astrbot.core.platform.message_type import MessageType
from ..task_priority import (
    UserTaskPriority,
    should_resume_continuous,
    should_yield_to_user,
)
from ..tools import build_phone_tools, check_admin_permission
from ..media import (
    MediaAudioCapture,
    build_vision_prompt,
    clamp_seconds as clamp_media_seconds,
    clean_transcript as clean_media_transcript,
    plan_frame_times,
    safe_unlink as safe_unlink_media,
)
from ..telemetry import MetricsStore
from ..ui import dump_ui, render_ui
from .shared import (
    _AutonomousDeviceEvent,
    _HELP_TEXT,
    _PHONE_CONTROL_TOOLS,
    _build_guidance,
    _companion_plugin_available,
    _read_plugin_version,
    PLUGIN_NAME,
    BUNDLED_ADB,
    PHONE_KEYWORDS,
    plugin_root_dir,
    _GUIDANCE,
    _format_time,
    _llm_response_text,
    _INBOX_KEYWORDS,
    _INBOX_EXCLUDE_WORDS,
)

from astrbot.api.web import request
from .compat import error_response, json_response, plugin_main_module
from .library_dashboard import LibraryDashboardMixin


class ObservabilityMixin(LibraryDashboardMixin):
    def _traffic_status_payload(self, *, limit: int = 5) -> dict[str, Any]:
        """Dashboard/命令共用的抓包状态摘要（含最近流量预览）。"""
        try:
            config = self.service.config
        except Exception:  # noqa: BLE001
            config = None
        enabled = bool(getattr(getattr(config, "traffic", None), "enabled", False))
        capture = self.service.traffic
        if capture is None:
            return {
                "enabled": enabled,
                "running": False,
                "port": 0,
                "flows": 0,
                "error": "抓包未初始化",
                "log_path": "",
                "preview": "抓包未初始化",
            }
        status = capture.status()
        flows = capture.recent(limit=max(1, int(limit)))
        body_chars = int(getattr(getattr(config, "traffic", None), "body_chars", 1200) or 1200)
        preview = render_traffic(flows, window_seconds=0, body_chars=min(body_chars, 400))
        return {
            "enabled": enabled,
            "running": bool(status.get("running")),
            "port": int(status.get("port") or 0),
            "flows": int(status.get("flows") or 0),
            "error": str(status.get("error") or ""),
            "log_path": str(status.get("log_path") or ""),
            "preview": preview,
        }

    def _traffic_text(self, *, limit: int = 10) -> str:
        """``/phone traffic`` 状态文本：运行状态 + 日志位置 + 最近流量。"""
        payload = self._traffic_status_payload(limit=limit)
        lines = [
            "流量抓包：",
            f"- 配置：{'已启用' if payload['enabled'] else '已关闭'}（traffic.enabled）",
            f"- 运行：{'运行中' if payload['running'] else '未运行'}"
            + (f"，端口 {payload['port']}" if payload["port"] else ""),
            f"- 已记录：{payload['flows']} 条流量",
            f"- 日志：{payload['log_path'] or '（未初始化）'}",
        ]
        if payload.get("error"):
            lines.append(f"- 错误：{payload['error']}")
        lines.append("")
        lines.append(payload.get("preview") or "（暂无流量）")
        return "\n".join(lines)

    def _learning_payload(
        self, *, query: str = "", limit: int = 50
    ) -> dict[str, Any]:
        """拼装学习记录页数据：统计信息 + 记录列表（最新在前）。"""
        payload: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "stats": {
                "entries": 0,
                "sources": {},
                "last_ts": 0.0,
                "max_entries": 0,
            },
            "entries": [],
        }
        store = getattr(self.service, "learning", None)
        if store is None:
            return payload
        stats = store.stats()
        entries = (
            store.search(query, limit=limit)
            if query
            else store.recent(limit=limit)
        )
        items: list[dict[str, Any]] = []
        for entry in reversed(entries):  # 最新在前
            try:
                ts = float(entry.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            tags = [
                str(tag) for tag in entry.get("tags") or [] if str(tag)
            ]
            items.append(
                {
                    "id": str(entry.get("id") or ""),
                    "ts": ts,
                    "content": str(entry.get("content") or ""),
                    "source": str(entry.get("source") or ""),
                    "tags": tags,
                }
            )
        payload["entries"] = items
        payload["stats"] = {
            "entries": int(stats.get("entries") or 0),
            "sources": {
                str(key): int(value)
                for key, value in (stats.get("sources") or {}).items()
            },
            "last_ts": float(stats.get("last_ts") or 0.0),
            "max_entries": int(stats.get("max_entries") or 0),
        }
        return payload

    def _dashboard_manager(self) -> Any:
        return getattr(self.service, "backends", None)

    def _register_dashboard_apis(self) -> None:
        """注册 Dashboard 监控与资料管理 Web API（页面由 views/dashboard 提供）。"""
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.debug("当前 AstrBot 不支持 register_web_api，跳过 Dashboard API 注册")
            return
        base = f"/{PLUGIN_NAME}/dashboard"
        routes = (
            (f"{base}/overview", self.dashboard_overview, ["GET"], "手机监控概览"),
            (f"{base}/series", self.dashboard_series, ["GET"], "手机监控指标时间序列"),
            (f"{base}/learning", self.dashboard_learning, ["GET"], "手机学习记录"),
        )
        routes += self._library_dashboard_routes(base)
        routes += self._config_migration_routes(base)
        for route, handler, methods, desc in routes:
            try:
                register(route, handler, methods, desc)
            except Exception as exc:  # noqa: BLE001 - 注册失败只记录
                logger.warning("注册 Dashboard API 失败（%s）：%s", route, exc)

    async def dashboard_overview(self):
        """GET dashboard/overview：运行状态/额度/今日互动/最近指标。"""
        self._maybe_warm_backends_from_dashboard()
        try:
            manager = self._dashboard_manager()
            status = manager.status() if manager is not None else None
            config = self.service.config
            used = float(self.quota.used(config.quota.window_hours))
            remaining = max(0.0, float(config.quota.max_tokens) - used)
            reason = ""
            if status is not None:
                reason = status.fallback_reason or status.last_error or ""
            data = {
                "version": self._version,
                "uptime_s": round(max(0.0, time.time() - self._started_at), 1),
                "backends": {
                    "control": getattr(status, "control", "adb"),
                    "control_config": str(config.control.backend),
                    "perception": getattr(status, "perception", "adb"),
                    "perception_config": str(config.perception.screen_source),
                    "scrcpy_running": bool(
                        getattr(status, "scrcpy_running", False)
                    ),
                    "scrcpy_video": getattr(status, "scrcpy_video", None),
                    "reason": reason,
                },
                "quota": {
                    "enabled": bool(config.quota.enabled),
                    "max_tokens": int(config.quota.max_tokens),
                    "window_hours": int(config.quota.window_hours),
                    "used": round(used, 1),
                    "remaining": round(remaining, 1),
                },
                "today": {
                    "comments": self.interactions.used_today(KIND_COMMENT),
                    "passive_replies": self.interactions.used_today(
                        KIND_PASSIVE_REPLY
                    ),
                    "posts": self.interactions.used_today(KIND_POST),
                },
                "traffic": self._traffic_status_payload(),
                "last_screenshot": self.telemetry.latest_screenshot(),
                "last_llm": (
                    {
                        key: (self.telemetry.latest_llm() or {}).get(key)
                        for key in ("ts", "tokens", "ms", "tps")
                    }
                    if self.telemetry.latest_llm()
                    else None
                ),
            }
            return json_response(data)
        except Exception as exc:  # noqa: BLE001 - 页面 API 失败给可读错误
            return error_response(f"读取概览失败：{exc}", status_code=500)

    async def dashboard_series(self):
        """GET dashboard/series?minutes=30：截图/LLM 序列与 10s token/s 桶。"""
        try:
            minutes = plugin_main_module().request.query.get("minutes", 30, type=int) or 30
            minutes = max(1, min(int(minutes), 24 * 60))
            return json_response(self.telemetry.recent_series(minutes))
        except Exception as exc:  # noqa: BLE001
            return error_response(f"读取指标序列失败：{exc}", status_code=500)

    async def dashboard_learning(self):
        """GET dashboard/learning?query=&limit=：AI 在手机上学到的内容（最新在前）。"""
        try:
            query = str(plugin_main_module().request.query.get("query", "") or "").strip()[:100]
            raw_limit = plugin_main_module().request.query.get("limit", 50, type=int)
            try:
                limit = int(raw_limit) if raw_limit is not None else 50
            except (TypeError, ValueError):
                limit = 50
            limit = max(1, min(limit, 200))
            return json_response(self._learning_payload(query=query, limit=limit))
        except Exception as exc:  # noqa: BLE001 - 学习档案读取失败给可读错误
            logger.warning("读取学习记录失败：%s", exc)
            return error_response(f"读取学习记录失败：{exc}", status_code=500)

    def _get_memory_companion_bridge(self) -> Any:
        """从已加载的 astrbot_plugin_memory_companion 模块取公开桥接对象。"""
        cached = getattr(self, "_memory_companion_bridge_cache", None)
        if cached is not None:
            return cached
        bridge = None
        try:
            lookup = getattr(self.context, "get_registered_star", None)
            metadata = lookup(COMPANION_PLUGIN_NAME) if callable(lookup) else None
            module = getattr(metadata, "module", None)
            getter = getattr(module, "get_memory_companion_bridge", None)
            if callable(getter):
                bridge = getter()
        except Exception as exc:  # noqa: BLE001 - 未安装/未加载时静默降级
            logger.debug("获取 MemoryCompanion 桥接失败：%s", exc)
        if bridge is not None:
            self._memory_companion_bridge_cache = bridge
        return bridge

    async def _sync_memory_to_companion(
        self, event: AstrMessageEvent, payload: dict[str, Any]
    ) -> tuple[bool, str]:
        """把一条学习/App 经验同步到 MemoryCompanion（失败只报告原因）。"""
        bridge = self._get_memory_companion_bridge()
        if bridge is None:
            return False, "未检测到 astrbot_plugin_memory_companion 记忆插件"
        user_id = self.service.memory_user_id(event)
        if not user_id:
            return False, "未配置记忆绑定用户（memory.companion_user_id 或 owner_ids）"
        try:
            result = await bridge.record_external_memory(
                user_id=user_id,
                content=str(payload.get("content") or ""),
                summary=str(payload.get("content") or ""),
                memory_type=str(payload.get("memory_type") or "external_memory"),
                source_plugin=COMPANION_SOURCE_PLUGIN,
                idempotency_key=str(payload.get("idempotency_key") or ""),
                tags=list(payload.get("tags") or []),
                payload=dict(payload.get("payload") or {}),
                long_term=True,
            )
        except Exception as exc:  # noqa: BLE001 - 桥接异常如实报告
            logger.warning("MemoryCompanion 外部记忆写入异常：%s", exc)
            return False, str(exc)
        ok = isinstance(result, dict) and bool(result.get("ok"))
        if ok:
            logger.info(
                "记忆已同步到 MemoryCompanion：type=%s memory_id=%s user=%s",
                payload.get("memory_type") or "",
                (result or {}).get("memory_id") or "?",
                user_id,
            )
            return True, ""
        reason = ""
        if isinstance(result, dict):
            reason = str(result.get("error_code") or result.get("error") or "")
        logger.warning("MemoryCompanion 拒绝外部记忆写入：%s", result)
        return False, reason or "记忆插件拒绝写入"

    def _get_livingmemory_plugin(self) -> Any:
        """获取已加载的 astrbot_plugin_livingmemory 插件实例（未装返回 None）。"""
        cached = getattr(self, "_livingmemory_plugin_cache", None)
        if cached is not None:
            return cached
        plugin = None
        try:
            lookup = getattr(self.context, "get_registered_star", None)
            metadata = lookup(LIVINGMEMORY_PLUGIN_NAME) if callable(lookup) else None
            plugin = getattr(metadata, "star_cls", None)
        except Exception as exc:  # noqa: BLE001 - 未安装/未加载时静默降级
            logger.debug("获取 LivingMemory 插件实例失败：%s", exc)
        if plugin is not None:
            self._livingmemory_plugin_cache = plugin
        return plugin

    async def _sync_memory_to_livingmemory(
        self, event: AstrMessageEvent, payload: dict[str, Any]
    ) -> tuple[bool, str]:
        """把一条学习/App 经验写入 LivingMemory（失败只报告原因）。"""
        plugin = self._get_livingmemory_plugin()
        if plugin is None:
            return False, "未检测到 astrbot_plugin_livingmemory 记忆插件"
        engine = getattr(getattr(plugin, "initializer", None), "memory_engine", None)
        if engine is None:
            engine = getattr(getattr(plugin, "event_handler", None), "memory_engine", None)
        if engine is None or not hasattr(engine, "add_memory"):
            return False, "LivingMemory 记忆引擎未就绪"
        content = str(payload.get("content") or "")
        if not content:
            return False, "记忆内容为空"
        session_id = ""
        try:
            session_id = str(getattr(event, "unified_msg_origin", "") or "").strip()
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            pass
        try:
            memory_id = await engine.add_memory(
                content=content,
                session_id=session_id or None,
                importance=0.6,
                metadata={
                    "source_plugin": COMPANION_SOURCE_PLUGIN,
                    "memory_type": "GENERAL",
                    "phone_memory_type": payload.get("memory_type") or "",
                    "tags": list(payload.get("tags") or []),
                    "phone_payload": dict(payload.get("payload") or {}),
                    "source": PLUGIN_NAME,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 写入异常如实报告
            logger.warning("写入 LivingMemory 失败：%s", exc)
            return False, str(exc)
        logger.info(
            "记忆已同步到 LivingMemory：type=%s memory_id=%s session=%s",
            payload.get("memory_type") or "",
            memory_id,
            session_id or "-",
        )
        return memory_id is not None, ""

    async def _sync_memory(
        self, event: AstrMessageEvent, payload: dict[str, Any]
    ) -> tuple[bool, str]:
        """按 memory.backend 同步到 MemoryCompanion / LivingMemory（可两者）。"""
        config = self.service.config
        backend = config.memory.backend
        targets: list[str] = []
        failures: list[str] = []
        if wants_companion(backend):
            targets.append("MemoryCompanion")
            ok, reason = await self._sync_memory_to_companion(event, payload)
            if not ok:
                failures.append(f"MemoryCompanion：{reason}")
        if wants_livingmemory(backend):
            targets.append("LivingMemory")
            ok, reason = await self._sync_memory_to_livingmemory(event, payload)
            if not ok:
                failures.append(f"LivingMemory：{reason}")
        # 自行刷手机（自主任务）的记忆：按开关额外记入 MemoryCompanion「个人记忆-行动」一份
        if (
            config.memory.bot_action_enabled
            and wants_companion(backend)
            and payload.get("memory_type") == LEARNING_MEMORY_TYPE
            and self.service.play_payload(event)
        ):
            action_text = build_bot_action_payload(
                content=str(payload.get("content") or ""),
                source=str((payload.get("payload") or {}).get("source") or ""),
            )
            if action_text:
                try:
                    bridge = self._get_memory_companion_bridge()
                    if bridge is not None and hasattr(bridge, "record_bot_action"):
                        await bridge.record_bot_action(
                            content=action_text,
                            source_plugin=COMPANION_SOURCE_PLUGIN,
                        )
                        logger.info("自行刷手机记忆已计入 MemoryCompanion「个人记忆-行动」")
                except Exception as exc:  # noqa: BLE001 - 行动记录失败不影响主同步
                    logger.warning("写入个人记忆-行动失败：%s", exc)
        success = not failures and bool(targets)
        detail = "、".join(targets) if success else "；".join(failures)
        return success, detail

    @staticmethod
    def _request_text(req: Any) -> str:
        """拼出用于估算输入 token 的请求文本。"""
        parts = [
            str(getattr(req, "system_prompt", "") or ""),
            str(getattr(req, "prompt", "") or ""),
        ]
        for part in getattr(req, "extra_user_content_parts", None) or []:
            text = part if isinstance(part, str) else getattr(part, "text", "")
            if text:
                parts.append(str(text))
        contexts = getattr(req, "contexts", None) or []
        for context in contexts:
            if isinstance(context, dict):
                content = context.get("content") or context.get("text") or ""
            else:
                content = getattr(context, "content", None) or ""
            if content:
                parts.append(str(content))
        return "\n".join(part for part in parts if part)

    def _record_request_tokens(self, event: AstrMessageEvent, req: Any) -> None:
        """记录一次手机相关 LLM 请求的输入 token 估算（额度统计用）。

        运行中只记估算；Agent run 结束时再用 AstrBot ``provider_stats`` 的实际
        usage 对账补记差额（见 :meth:`_reconcile_phone_usage`）。
        """
        tokens = estimate_tokens(self._request_text(req))
        play = bool(self.service.play_payload(event))
        if play:
            self.service.ensure_play_budget(event)
        session = str(getattr(event, "unified_msg_origin", "") or "")
        self.service.record_tokens(
            tokens, session=session, source="llm_request", play=play
        )
        try:
            event.set_extra("phone_usage_tracked", True)
            event.set_extra("phone_run_started_wall", time.time())
            event.set_extra("phone_usage_initial_estimate", float(tokens))
            event.set_extra("phone_usage_recorded", float(tokens))
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            pass

    def _begin_llm_metric(self, event: AstrMessageEvent, req: Any) -> None:
        """Agent run 开始时记录指标起点：输入 token 估算 + 起始时间。

        AstrBot 的 ``on_llm_request`` 每个 Agent run 只触发一次，工具循环里的
        每次模型调用没有独立 usage 回调；这里以「工具调用边界」切分每次模型
        调用，先用输入 token 估算，最终响应再用真实 usage 校正。
        """
        try:
            tokens = float(estimate_tokens(self._request_text(req)))
            event.set_extra("phone_llm_started", time.monotonic())
            event.set_extra("phone_llm_est_tokens", tokens)
            event.set_extra("phone_metric_active", False)
        except Exception:  # noqa: BLE001 - 指标失败不影响对话
            pass

    def _record_llm_round_metric(
        self, event: AstrMessageEvent, tool_name: str, tool_args: Any = None
    ) -> None:
        """手机工具调用开始时，记录刚完成那一轮模型调用的输出 tokens/耗时。

        这里记录的是模型**生成**的 tokens（本轮输出是工具调用参数），tps 才与
        模型真实生成速度同量级；输入上下文（可能几万 tokens）只在计费里估算，
        不参与 token/s 曲线。
        """
        try:
            started = event.get_extra("phone_llm_started")
            if started is None:
                return
            now = time.monotonic()
            duration_ms = max(0.0, (now - float(started)) * 1000.0)
            input_tokens = float(event.get_extra("phone_llm_est_tokens") or 0.0)
            if duration_ms < 1:
                return
            try:
                args_text = json.dumps(tool_args or {}, ensure_ascii=False)
            except (TypeError, ValueError):
                args_text = str(tool_args or "")
            output_tokens = float(
                max(1, estimate_tokens(str(tool_name or "")) + estimate_tokens(args_text))
            )
            session = str(getattr(event, "unified_msg_origin", "") or "")
            self.telemetry.record_llm(
                tokens=output_tokens, ms=duration_ms, session=session
            )
            event.set_extra("phone_metric_active", True)
            try:
                growth = float(self.service.config.quota.per_step_tokens)
            except Exception:  # noqa: BLE001 - 配置异常按默认步长
                growth = 2500.0
            if growth < 1:
                growth = 2500.0
            # 下一轮请求会带上本轮工具调用与结果，按配置步长估算增长。
            event.set_extra("phone_llm_est_tokens", input_tokens + growth)
            event.set_extra("phone_llm_started", now)
        except Exception:  # noqa: BLE001 - 指标失败不影响工具调用
            pass

    @staticmethod
    def _usage_output(usage: Any) -> float:
        """兼容 TokenUsage 对象/dict，提取模型输出（生成）token 数。"""
        if usage is None:
            return 0.0
        if isinstance(usage, dict):
            for key in ("output_tokens", "completion_tokens", "output"):
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    return float(value)
            return 0.0
        value = getattr(usage, "output", None)
        if isinstance(value, (int, float)):
            return float(value)
        for key in ("output_tokens", "completion_tokens"):
            value = getattr(usage, key, None)
            if isinstance(value, (int, float)):
                return float(value)
        return 0.0

    @staticmethod
    def _usage_total(usage: Any) -> float:
        """兼容 TokenUsage 对象/dict，提取真实 usage 总量。"""
        if usage is None:
            return 0.0
        if isinstance(usage, dict):
            for key in ("total_tokens", "total", "tokens"):
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    return float(value)
            total = 0.0
            for key in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens"):
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    total += float(value)
            return total
        total = getattr(usage, "total", None)
        if isinstance(total, (int, float)):
            return float(total)
        value = 0.0
        for key in ("input_other", "input_cached", "output"):
            part = getattr(usage, key, None)
            if isinstance(part, (int, float)):
                value += float(part)
        return value

    @staticmethod
    def _tool_result_text(tool_result: Any) -> str:
        """提取工具结果中的文本内容（兼容 CallToolResult / dict / 字符串）。"""
        if isinstance(tool_result, str):
            return tool_result
        content: Any = None
        if isinstance(tool_result, dict):
            content = tool_result.get("content")
        else:
            content = getattr(tool_result, "content", None)
        parts: list[str] = []
        for item in content or []:
            text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)

    @classmethod
    def _phone_tool_result_failed(cls, tool_result: Any) -> bool:
        """工具结果是否失败（错误统一以 ``error:`` 开头）。"""
        if tool_result is None:
            return True
        return cls._tool_result_text(tool_result).lstrip().startswith("error:")

    def _should_capture_agent_usage(self, event: Any) -> bool:
        """是否采集本次 Agent run 的累计 usage（普通手机 run 或自主 cron 任务）。"""
        try:
            if event.get_extra("phone_usage_tracked"):
                return True
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            pass
        try:
            return self._is_autonomous_task(event)
        except Exception:  # noqa: BLE001
            return False

    def _schedule_usage_reconcile(self, event: AstrMessageEvent) -> None:
        """Agent 结束后异步对账：用 AstrBot provider_stats 的实际 usage 补记差额。"""
        try:
            tracked = False
            try:
                tracked = bool(event.get_extra("phone_usage_tracked"))
            except Exception:  # noqa: BLE001 - 鸭子类型事件
                tracked = False
            if not tracked and not self._is_autonomous_task(event):
                return
            asyncio.get_running_loop()
            asyncio.create_task(
                self._reconcile_phone_usage(event),
                name=f"{PLUGIN_NAME}_usage_reconcile",
            )
        except RuntimeError:  # 没有事件循环（单测/关闭阶段）
            return
        except Exception as exc:  # noqa: BLE001 - 对账调度失败不影响任务收尾
            logger.debug("token 对账调度失败：%s", exc)

    async def _await_event_agent_usage(
        self, event: AstrMessageEvent, *, timeout: float = 3.0
    ) -> usage_audit.AgentUsage | None:
        """等待 AgentRunner.step 包装写入的本次 run 累计 usage（含 cron 任务）。"""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            payload = None
            try:
                payload = event.get_extra(AGENT_USAGE_EXTRA)
            except Exception:  # noqa: BLE001 - 鸭子类型事件
                payload = None
            if isinstance(payload, dict):
                try:
                    usage = usage_audit.AgentUsage(
                        input_other=int(payload.get("input_other") or 0),
                        input_cached=int(payload.get("input_cached") or 0),
                        output=int(payload.get("output") or 0),
                        calls=1,
                        provider_models=(
                            (str(payload.get("model") or ""),)
                            if str(payload.get("model") or "")
                            else ()
                        ),
                    )
                except (TypeError, ValueError):
                    usage = None
                if usage is not None and usage.total > 0:
                    return usage
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.05)

    async def _reconcile_phone_usage(self, event: AstrMessageEvent) -> None:
        """按 umo + run 起始时间读取 provider_stats，补记实际 usage 与估算的差额。"""
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            started = float(event.get_extra("phone_run_started_wall") or 0.0)
            estimated = float(event.get_extra("phone_usage_recorded") or 0.0)
            if not umo:
                return
            play = bool(self.service.play_payload(event))
            # 优先用 runner stats（覆盖 cron 自主任务，精确且无需等 DB）；
            # 包装不可用时回退 AstrBot provider_stats（普通对话路径）。
            actual = await self._await_event_agent_usage(event)
            source = "runner_stats"
            if actual is None and started > 0:
                actual = await usage_audit.fetch_agent_run_usage(umo, started)
                source = "provider_stats"
            if actual is None or actual.total <= 0:
                logger.warning(
                    "token 对账：未取到本次手机运行的实际 usage（umo=%s，"
                    "已记估算 %.0f tokens，保留估算不虚构实际）",
                    umo,
                    estimated,
                )
                return
            delta = max(0.0, float(actual.total) - estimated)
            if play:
                self.service.ensure_play_budget(event)
            # 差额补记为 actual 条目；无差额时也落一条 actual 记录供 /phone quota 展示
            self.service.record_tokens(
                delta,
                session=umo,
                source="agent_usage",
                play=play,
                input_other=int(actual.input_other),
                input_cached=int(actual.input_cached),
                output=int(actual.output),
                actual=True,
            )
            self.service.last_usage_reconcile = {
                "ts": time.time(),
                "umo": umo,
                "actual": float(actual.total),
                "input_other": int(actual.input_other),
                "input_cached": int(actual.input_cached),
                "output": int(actual.output),
                "estimated": estimated,
                "delta": delta,
                "calls": int(actual.calls),
                "models": list(actual.provider_models),
                "play": play,
                "source": source,
            }
            logger.info(
                "token 对账（%s）：umo=%s 实际 %.0f（输入 %.0f/缓存 %.0f/输出 %.0f，"
                "%d 次模型调用）vs 估算 %.0f，补记 %.0f；模型=%s",
                source,
                umo,
                float(actual.total),
                float(actual.input_other),
                float(actual.input_cached),
                float(actual.output),
                int(actual.calls),
                estimated,
                delta,
                ",".join(actual.provider_models) or "未知",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 对账失败只记录，不影响主流程
            logger.warning("token 对账失败：%s", exc)

    async def _apply_task_provider_preference(self, job: Any) -> bool:
        """任务创建后为该 cron UMO 写入模型供应商偏好（run 结束还原）。

        cron 主动任务不经过 pipeline 的 on_waiting_llm_request 钩子，直接
        ``build_main_agent``，因此用会话级 Provider 偏好（AstrBot 支持的
        ``provider_perf_chat_completion``）确保自主任务使用配置的模型。
        投递到用户会话的任务（payload.session 非空）不改写用户会话模型。
        """
        try:
            config = self._config()
            provider_id = str(
                getattr(config.model, "provider_id", "") or ""
            ).strip()
            if not provider_id:
                return False
            payload = getattr(job, "payload", None) or {}
            if not isinstance(payload, dict):
                return False
            if str(payload.get("session") or "").strip():
                return False
            job_id = str(getattr(job, "job_id", "") or "")
            if not job_id:
                return False
            provider = self.context.get_provider_by_id(provider_id)
            if provider is None:
                if provider_id not in self._task_provider_warned_ids:
                    self._task_provider_warned_ids.add(provider_id)
                    logger.warning(
                        "自主任务模型供应商不存在：%s（本次回退默认模型）",
                        provider_id,
                    )
                return False
            from astrbot.core import sp as shared_prefs
            from astrbot.core.provider.entities import ProviderType

            umo = str(
                MessageSession(
                    platform_name="cron",
                    message_type=MessageType.OTHER_MESSAGE,
                    session_id=job_id,
                )
            )
            key = "provider_perf_chat_completion"
            previous = await shared_prefs.session_get(umo, key, "")
            await self.context.provider_manager.set_provider(
                provider_id, ProviderType.CHAT_COMPLETION, umo
            )
            self._task_provider_restore[umo] = str(previous or "")
            logger.info(
                "自主任务模型供应商：%s（任务 %s）", provider_id, job_id
            )
            return True
        except Exception as exc:  # noqa: BLE001 - 供应商应用失败回退默认
            logger.warning("应用自主任务模型供应商失败：%s", exc)
            return False

    async def _restore_task_provider_preference(self, umo: str) -> None:
        """还原任务 UMO 的会话 Provider 偏好（run 结束/任务完成后调用）。"""
        if not umo or umo not in self._task_provider_restore:
            return
        previous = self._task_provider_restore.pop(umo)
        try:
            from astrbot.core import sp as shared_prefs

            key = "provider_perf_chat_completion"
            if previous:
                await shared_prefs.session_put(umo, key, previous)
            else:
                await shared_prefs.session_remove(umo, key)
        except Exception as exc:  # noqa: BLE001 - 还原失败只记录（唯一 cron UMO）
            logger.warning("还原自主任务模型供应商失败（%s）：%s", umo, exc)
