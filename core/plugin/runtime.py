"""插件运行时后端 mixin：实时屏幕、scrcpy/minitouch 控制后端、抓包生命周期。"""

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
from astrbot.api.web import error_response, json_response, request
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
from ..app_memory import AppMemoryStore, AppMemoryError
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
)
from ..config_apply import ACTION_RESTART, ACTION_START, ACTION_STOP, build_apply_plan
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
    PHONE_KEYWORDS,
    plugin_root_dir,
    _GUIDANCE,
    _format_time,
    _llm_response_text,
    _INBOX_KEYWORDS,
    _INBOX_EXCLUDE_WORDS,
)


class RuntimeMixin:
    async def _capture_live_frame(self) -> tuple[bytes, Path, dict[str, Any]]:
        """实时屏幕抓帧回调：截图 + 当前前台 App/Activity。"""
        data, path, meta = await self.service.capture(
            quiet=True,
            record_metric=False,
            persist=False,
        )
        foreground = ""
        activity = ""
        try:
            foreground, activity = await self.actions.foreground()
        except Exception:  # noqa: BLE001 - 前台信息失败不影响帧
            pass
        enriched = dict(meta or {})
        enriched["foreground"] = foreground
        enriched["activity"] = activity
        return data, path, enriched

    def _live_screen_settings(self) -> tuple[bool, int, int]:
        """实时屏幕运行参数（每轮循环实时读配置）。"""
        try:
            perception = self.service.config.perception
        except Exception:  # noqa: BLE001 - 配置异常时按默认继续
            return True, 1200, 3
        return (
            bool(perception.live_screen_enabled),
            int(perception.live_screen_interval_ms),
            int(perception.live_screen_frames),
        )

    @staticmethod
    def _filter_factory_kwargs(factory: Any, candidates: dict[str, Any]) -> dict[str, Any]:
        """按构造函数签名裁剪参数，兼容 scrcpy 客户端接口小改。"""
        try:
            params = inspect.signature(factory).parameters
        except (TypeError, ValueError):
            return {}
        if any(
            param.kind == inspect.Parameter.VAR_KEYWORD
            for param in params.values()
        ):
            return dict(candidates)
        return {
            name: value
            for name, value in candidates.items()
            if name in params and value is not None and value != ""
        }

    def _scrcpy_server_jar_path(self, config: PhoneConfig) -> str:
        """解析 scrcpy server jar：配置显式路径优先，否则用插件自带版本。"""
        server_jar = str(config.control.scrcpy.server_jar or "").strip()
        if server_jar:
            return server_jar
        return str(
            plugin_root_dir() / "bin" / "scrcpy-server-v5.0.1"
        )

    async def _create_scrcpy_client(self) -> Any:
        """懒加载 ``core.scrcpy.ScrcpyClient``（模块未就绪时抛错由管理器回退 ADB）。"""
        from ..scrcpy import ScrcpyClient

        config = self.service.config
        server_jar = self._scrcpy_server_jar_path(config)
        candidates = {
            "adb_path": str(self._resolve_adb_path(config)),
            "adb": self.adb,
            "serial": str(getattr(self.adb, "serial", "") or ""),
            "host": config.adb.host,
            "ip": config.adb.host,
            "port": config.adb.port,
            "max_size": config.control.scrcpy.max_size,
            "server_jar": server_jar,
            "data_dir": str(self.data_dir / "scrcpy"),
            "logger": self._device_log,
            "log": self._device_log,
        }
        kwargs = self._filter_factory_kwargs(ScrcpyClient, candidates)
        return ScrcpyClient(**kwargs)

    async def _create_minitouch_client(self, binary_path: str) -> Any:
        """加载用户自备的 minitouch binary（可选后端，失败继续 scrcpy/ADB）。"""
        from ..minitouch import MinitouchClient

        config = self.service.config
        candidates = {
            "binary_path": binary_path,
            "adb_path": str(self._resolve_adb_path(config)),
            "adb": self.adb,
            "serial": str(getattr(self.adb, "serial", "") or ""),
            "host": config.adb.host,
            "port": config.adb.port,
            "data_dir": str(self.data_dir / "minitouch"),
            "logger": self._device_log,
            "log": self._device_log,
        }
        kwargs = self._filter_factory_kwargs(MinitouchClient, candidates)
        client = MinitouchClient(**kwargs)
        start = getattr(client, "start", None)
        if callable(start):
            value = start()
            if inspect.isawaitable(value):
                await value
        return client

    def _on_backends_started(self) -> None:
        """启动后维护所选连接，并执行空闲停止与经验补写。"""
        try:
            task = getattr(self, "_backend_idle_task", None)
            if task is not None and not task.done():
                return
            self._backend_idle_task = asyncio.create_task(
                self._backend_idle_loop(),
                name=f"{PLUGIN_NAME}_backend_idle",
            )
        except RuntimeError:  # 没有运行中的事件循环（例如单测直接调用）
            self._backend_idle_task = None

    async def _backend_idle_loop(self) -> None:
        """维护常驻连接；关闭常驻时按空闲时限停止。"""
        try:
            while True:
                try:
                    idle = int(
                        self.service.config.control.scrcpy.idle_stop_seconds
                    )
                except Exception:  # noqa: BLE001 - 配置异常按默认
                    idle = 120
                await asyncio.sleep(max(1.0, min(5.0, idle / 3.0)))
                manager = getattr(self, "backends", None)
                if manager is None:
                    return
                try:
                    await self.service.experiences.flush()
                except (OSError, ValueError, AppMemoryError) as exc:
                    logger.warning("App经验待写队列不可用：%s", exc)
                if getattr(self, "_phone_stopped", False):
                    continue
                if self.service.config.control.scrcpy.keep_alive:
                    await manager.ensure()
                if not manager.scrcpy_running:
                    continue
                await manager.maybe_stop_idle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 空闲检查失败只记录
            logger.warning("控制后端空闲检查失败：%s", exc)

    def _note_backend_activity(self) -> None:
        """记录一次手机工具活动，供 scrcpy 空闲停止判断。"""
        manager = getattr(self, "backends", None)
        if manager is None:
            return
        manager.note_activity()
        if manager.scrcpy_running:
            self._on_backends_started()

    def _live_idle_seconds(self) -> int:
        """任务会话空闲自动停秒数（30-600）。"""
        try:
            value = int(
                self.service.config.perception.live_screen_idle_stop_seconds
            )
        except Exception:  # noqa: BLE001 - 配置异常时按默认
            value = 90
        return max(30, min(600, value))

    def _ensure_live_screen_started(self, event: AstrMessageEvent) -> bool:
        """任务会话第一次调用手机工具时懒启动 watcher（普通聊天不启动）。"""
        try:
            perception = self.service.config.perception
        except Exception:  # noqa: BLE001
            return False
        if not perception.live_screen_enabled:
            return False
        if not self.service.is_task_session(event):
            return False
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if umo:
            self._live_task_sessions.add(umo)
        self._live_last_activity = time.monotonic()
        if self.live_screen.running:
            return True
        try:
            self.live_screen.start()
        except Exception as exc:  # noqa: BLE001 - 启动失败只记录，不影响任务
            logger.warning("实时屏幕：任务启动失败：%s", exc)
            return False
        logger.info(
            "实时屏幕：任务开始，watcher 启动（间隔 %sms，缓存 %s 帧）",
            perception.live_screen_interval_ms,
            perception.live_screen_frames,
        )
        self._start_live_idle_monitor()
        return True

    def _start_live_idle_monitor(self) -> None:
        """启动任务空闲检查协程（已存在则复用）。"""
        task = getattr(self, "_live_idle_task", None)
        if task is not None and not task.done():
            return
        self._live_idle_task = asyncio.create_task(
            self._live_idle_loop(), name=f"{PLUGIN_NAME}_live_idle"
        )

    async def _live_idle_loop(self) -> None:
        """任务会话空闲超时后停止 watcher（不常驻）。"""
        try:
            while True:
                idle = self._live_idle_seconds()
                await asyncio.sleep(max(1.0, min(5.0, idle / 3.0)))
                if not self.live_screen.running:
                    return
                if await self._check_live_screen_idle():
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 空闲检查失败只记录
            logger.warning("实时屏幕空闲检查失败：%s", exc)

    async def _check_live_screen_idle(
        self, now: float | None = None
    ) -> bool:
        """检查并执行空闲停止；返回是否已停止。"""
        if not self.live_screen.running:
            return False
        current = float(now) if now is not None else time.monotonic()
        last = float(getattr(self, "_live_last_activity", 0.0) or 0.0)
        if last <= 0:
            self._live_last_activity = current
            return False
        if current - last < self._live_idle_seconds():
            return False
        await self._stop_live_screen("任务会话空闲超时")
        return True

    async def _stop_live_screen(self, reason: str) -> None:
        """停止 watcher、取消空闲检查并清空任务会话/缓冲。"""
        monitor = getattr(self, "_live_idle_task", None)
        self._live_idle_task = None
        if monitor is not None and not monitor.done():
            if monitor is not asyncio.current_task():
                monitor.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await monitor
        if self.live_screen.running:
            try:
                await self.live_screen.stop()
            except Exception as exc:  # noqa: BLE001 - 停止失败只记录
                logger.warning("实时屏幕停止失败：%s", exc)
            logger.info("实时屏幕：%s，已停止", reason)
        self._live_task_sessions.clear()
        self._live_last_activity = 0.0

    async def _ensure_traffic_started(self) -> None:
        """按配置启动/重启抓包（禁用时确保停止）。

        ``TrafficCapture.start`` 内部持锁且幂等；确定性不可用（设备无 root、
        系统证书挂载被拒绝、mitmdump 缺失等）已记录原因，这里只输出一次 WARN
        并停止自动重试；临时失败按 start 的退避时间重试。
        """
        if getattr(self, "_terminated", False) or getattr(self, "_phone_stopped", False):
            return
        # 假开启（未配置 ADB）：不启动任何设备侧抓包/代理。
        if not self.service.configured:
            if self._traffic.running:
                await self._traffic.stop()
            return
        config = self.service.config
        traffic_config = getattr(config, "traffic", None)
        if traffic_config is None or not traffic_config.enabled:
            if self._traffic.running:
                await self._traffic.stop()
            return
        if self._traffic.running:
            await self._traffic.ensure_device()
            return
        if self._traffic.unavailable:
            return
        if not self._traffic.retry_ready:
            return
        result = await self._traffic.start(
            port=traffic_config.port,
            body_chars=traffic_config.body_chars,
        )
        if result.get("ok"):
            logger.info(
                "抓包：已启动（端口 %s，日志 %s）",
                result.get("port"),
                result.get("log_path"),
            )
        elif result.get("unavailable"):
            logger.warning(
                "抓包不可用：%s；本次运行不再自动重试（/phone traffic status 查看）",
                result.get("reason") or "未提供原因",
            )
        else:
            logger.warning(
                "抓包启动失败：%s；%.0f 秒后重试",
                result.get("reason") or "未提供原因",
                float(result.get("retry_in") or 0.0),
            )

    def _start_traffic_keepalive(self) -> None:
        """启动抓包保活循环（90 秒一次，读实时配置）。"""
        task = getattr(self, "_traffic_keepalive_task", None)
        if task is not None and not task.done():
            return
        try:
            self._traffic_keepalive_task = asyncio.create_task(
                self._traffic_keepalive_loop(),
                name=f"{PLUGIN_NAME}_traffic_keepalive",
            )
        except RuntimeError:
            self._traffic_keepalive_task = None

    @staticmethod
    def _traffic_log_lines(
        flows: list[dict[str, Any]], *, body_chars: int = 300, limit: int = 3
    ) -> list[str]:
        """把最近抓包记录压成日志行（URL 打码、正文截断、附页面推断/可分享链接）。"""
        lines: list[str] = []
        for flow in list(flows or [])[: max(0, int(limit))]:
            try:
                when = time.strftime(
                    "%H:%M:%S", time.localtime(float(flow.get("ts") or 0))
                )
                url = mask_url(flow.get("url"))
                hint = infer_flow_hints(flow)
                head = (
                    f"[{when}] {flow.get('method') or 'GET'} {url}"
                    f" → {flow.get('status')}"
                )
                if hint:
                    head += f" | {hint}"
                lines.append(head)
                body = " ".join(str(flow.get("body") or "").split())
                if body:
                    chars = max(80, int(body_chars))
                    if len(body) > chars:
                        body = body[:chars] + "…"
                    lines.append(f"  响应：{body}")
                links = extract_share_links(flow)
                if links:
                    lines.append("  可分享链接：" + " | ".join(links[:3]))
            except Exception:  # noqa: BLE001 - 单条日志渲染失败跳过
                continue
        return lines

    async def _traffic_keepalive_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(90)
                config = self.service.config
                traffic_config = getattr(config, "traffic", None)
                if traffic_config is None or not traffic_config.enabled:
                    if self._traffic.running:
                        await self._traffic.stop()
                    continue
                # 确定性不可用或临时失败退避未到期：静默跳过，不刷 WARN。
                if self._traffic.unavailable or not self._traffic.retry_ready:
                    continue
                if not self._traffic.running:
                    await self._ensure_traffic_started()
                    continue
                await self._traffic.ensure_device()
                if not self._traffic.running:
                    continue
                now = time.time()
                if now - self._last_traffic_log >= 300:
                    status = self._traffic.status()
                    recent = self._traffic.recent(limit=100, since_seconds=300)
                    self._last_traffic_log = now
                    logger.info(
                        "抓包：运行中（端口 %s，已记录 %s 条，近 5 分钟 %s 条）",
                        status.get("port"),
                        status.get("flows"),
                        len(recent),
                    )
                    body_chars = int(
                        getattr(traffic_config, "body_chars", 1200) or 1200
                    )
                    for line in self._traffic_log_lines(
                        recent, body_chars=min(body_chars, 600), limit=3
                    ):
                        logger.info("抓包：%s", line)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 保活失败只记录
                logger.warning("抓包保活异常：%s", exc)

    # ---- WireGuard 组网 -------------------------------------------------

    async def _ensure_wireguard_started(self, *, force: bool = False) -> bool:
        """按配置启动/保活 WireGuard helper（禁用时确保停止）。

        确定性不可用（helper 缺失、配置非法等）由 manager 记录原因并停止自动重试，
        临时失败按 manager 的退避时间重试；本方法只负责驱动，不重复告警。
        """
        manager = getattr(self, "_wireguard", None)
        if manager is None:
            return False
        # 假开启（未配置 ADB）：不启动 WireGuard helper。
        if not self.service.configured:
            if manager.running or manager.adb_over_wg:
                await manager.stop()
            return False
        try:
            settings = self.service.config.wireguard
        except Exception as exc:  # noqa: BLE001 - 配置解析失败时不启动
            logger.warning("WireGuard：读取配置失败：%s", exc)
            return False
        if not settings.enabled:
            if manager.running or manager.adb_over_wg:
                await manager.stop()
            return False
        if force:
            manager.reset_unavailable("配置变化")
        if manager.running:
            await manager.ensure_healthy()
            return manager.running
        if manager.unavailable or not manager.retry_ready:
            return False
        result = await manager.start()
        return bool(result.get("ok"))

    def _start_wireguard_keepalive(self) -> None:
        """启动 WireGuard 保活循环（15 秒一次，读实时配置）。"""
        task = getattr(self, "_wireguard_keepalive_task", None)
        if task is not None and not task.done():
            return
        try:
            self._wireguard_keepalive_task = asyncio.create_task(
                self._wireguard_keepalive_loop(),
                name=f"{PLUGIN_NAME}_wireguard_keepalive",
            )
        except RuntimeError:  # 没有运行中的事件循环（单测直接调用）
            self._wireguard_keepalive_task = None

    async def _wireguard_keepalive_loop(self) -> None:
        """保活 + 退避重试：状态文件过期/进程退出按临时失败处理。"""
        try:
            while True:
                await asyncio.sleep(15)
                manager = getattr(self, "_wireguard", None)
                if manager is None:
                    continue
                try:
                    settings = self.service.config.wireguard
                except Exception:  # noqa: BLE001 - 配置异常时跳过本轮
                    continue
                if not settings.enabled:
                    if manager.running or manager.adb_over_wg:
                        await manager.stop()
                    continue
                # 确定性不可用或临时失败退避未到期：静默跳过，不刷 WARN。
                if manager.unavailable or not manager.retry_ready:
                    continue
                if not manager.running:
                    await self._ensure_wireguard_started()
                    continue
                health = manager.health()
                if not health.get("ok"):
                    await manager.ensure_healthy()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 保活失败只记录
            logger.warning("WireGuard 保活异常：%s", exc)

    async def _apply_wireguard_config(self, action: str, notes: list[str]) -> None:
        """配置热应用：按动作停止/启动/重启 WireGuard helper。"""
        manager = getattr(self, "_wireguard", None)
        if manager is None:
            return
        if action == ACTION_STOP:
            await manager.stop()
            notes.append("WireGuard：helper 已停止并恢复 ADB 目标")
            return
        if action not in {ACTION_START, ACTION_RESTART}:
            return
        manager.reset_unavailable("配置变化")
        if action == ACTION_RESTART:
            result = await manager.restart()
        else:
            result = await manager.start()
        if result.get("ok"):
            notes.append("WireGuard：helper 已按新配置就绪")
        else:
            reason = str(
                result.get("reason")
                or result.get("unavailable_reason")
                or "未提供原因"
            )
            notes.append(f"WireGuard：启动失败（{reason}）")

    def _maybe_warm_scrcpy(self, event: AstrMessageEvent, req: Any) -> None:
        """手机相关请求时后台预热 scrcpy（60s 最多一次，不阻塞 LLM）。"""
        try:
            if getattr(self, "_terminated", False):
                return
            if getattr(self, "_phone_stopped", False):
                if not self._looks_like_phone_request(event, req):
                    return
                self._phone_stopped = False
            manager = getattr(self.service, "backends", None)
            if manager is None or manager.scrcpy_running:
                return
            # 假开启（未配置 ADB）时面板只读，不预热任何设备后端。
            if not self.service.configured:
                return
            config = self.service.config
            if (
                config.control.backend == "adb"
                and config.perception.screen_source == "adb"
            ):
                return
            if not self._looks_like_phone_request(event, req):
                return
            now = time.monotonic()
            if now - self._last_scrcpy_warmup < 60:
                return
            self._last_scrcpy_warmup = now
            task = getattr(self, "_scrcpy_warmup_task", None)
            if task is not None and not task.done():
                return
            need_control = config.control.backend in {"auto", "scrcpy"}
            need_perception = config.perception.screen_source in {"auto", "scrcpy"}
            self._scrcpy_warmup_task = asyncio.create_task(
                manager.ensure(
                    need_control=need_control, need_perception=need_perception
                ),
                name=f"{PLUGIN_NAME}_scrcpy_warmup",
            )
        except Exception as exc:  # noqa: BLE001 - 预热失败不影响对话
            logger.debug("scrcpy 预热任务创建失败：%s", exc)

    def _maybe_warm_backends_from_dashboard(self) -> None:
        """Dashboard 页面打开时后台预热 scrcpy（60s 节流，不阻塞概览）。

        控制后端配置为 auto/scrcpy 时，页面一打开就懒启动 scrcpy（感知来源为
        adb 时仅控制通道），避免「为什么默认显示 adb」的误解；失败仍回退 ADB。
        """
        try:
            if getattr(self, "_phone_stopped", False) or getattr(self, "_terminated", False):
                return
            manager = getattr(self.service, "backends", None)
            if manager is None or manager.scrcpy_running:
                return
            # 假开启（未配置 ADB）时面板只读，不预热任何设备后端。
            if not self.service.configured:
                return
            config = self.service.config
            need_control = config.control.backend in {"auto", "scrcpy"}
            need_perception = config.perception.screen_source in {"auto", "scrcpy"}
            if not (need_control or need_perception):
                return
            now = time.monotonic()
            if now - self._last_scrcpy_warmup < 60:
                return
            task = getattr(self, "_scrcpy_warmup_task", None)
            if task is not None and not task.done():
                return
            self._last_scrcpy_warmup = now
            self._scrcpy_warmup_task = asyncio.create_task(
                manager.ensure(
                    need_control=need_control, need_perception=need_perception
                ),
                name=f"{PLUGIN_NAME}_dashboard_warmup",
            )
        except Exception as exc:  # noqa: BLE001 - 预热失败不影响页面
            logger.debug("Dashboard scrcpy 预热失败：%s", exc)


    def _start_mesh_keepalive(self):
        if getattr(self, "_mesh_task", None) is not None:
            return
        async def loop():
            while not getattr(self, "_terminated", False):
                try:
                    await self._mesh.reconcile()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._mesh.last_error = "组网启动或连接失败，请通过 /phone mesh status 检查配置及授权"
                await asyncio.sleep(15)
        self._mesh_task = asyncio.create_task(loop(), name=f"{PLUGIN_NAME}_mesh_keepalive")
