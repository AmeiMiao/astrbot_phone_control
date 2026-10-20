"""插件装配与生命周期 mixin：组件装配、初始化/终止、配置持久化与热应用。"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import weakref
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
    ensure_executable,
    PHONE_KEYWORDS,
    plugin_root_dir,
    _GUIDANCE,
    _format_time,
    _llm_response_text,
    _INBOX_KEYWORDS,
    _INBOX_EXCLUDE_WORDS,
)

from ..service import PhoneService
from ..verification_controller import PhoneVerification
from .compat import plugin_main_module

# 存活的插件实例登记。AstrBot 的 ``PluginManager._terminate_plugin`` 只检查
# 插件类**自身** ``__dict__`` 里是否有 ``terminate``（见
# astrbot/core/star/star_manager.py），定义在 mixin 里的 terminate 不会被探测到，
# 插件 reload/禁用后旧实例的 keepalive 等后台循环仍在运行并操作同一设备。
# 新实例激活时据此先关闭旧实例；模块级弱引用不会阻止旧实例被回收。
LIVE_PLUGIN_INSTANCES: "weakref.WeakSet[Any]" = weakref.WeakSet()
PLUGIN_INSTANCE_CLOSE_TIMEOUT = 20.0
# 插件后台任务统一命名前缀：初始化时取消其它（含历史版本）实例遗留的循环。
PLUGIN_TASK_PREFIX = "astrbot_phone_control_"


async def close_stale_instances(
    current: Any, *, timeout: float = PLUGIN_INSTANCE_CLOSE_TIMEOUT
) -> int:
    """关闭登记表里仍然存活的旧插件实例；返回处理的实例数。

    正常 reload 路径由 AstrBot 调用 ``terminate``；这里兜底其它未调用 terminate
    的重载路径，避免旧实例的 keepalive/随机/持续刷循环继续访问同一台设备。
    """
    stale = [
        instance
        for instance in list(LIVE_PLUGIN_INSTANCES)
        if instance is not current
    ]
    if not stale:
        return 0
    logger.warning(
        "检测到 %d 个旧插件实例仍存活，先停用旧实例，避免后台循环重复操作设备",
        len(stale),
    )
    for instance in stale:
        terminate = getattr(instance, "terminate", None)
        if not callable(terminate):
            LIVE_PLUGIN_INSTANCES.discard(instance)
            continue
        try:
            await asyncio.wait_for(terminate(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "停用旧插件实例超时（%.0f 秒），已取消其终止流程", timeout
            )
        except Exception as exc:  # noqa: BLE001 - 停用旧实例失败不阻塞新实例
            logger.warning(
                "停用旧插件实例失败：%s: %s", type(exc).__name__, exc
            )
        finally:
            LIVE_PLUGIN_INSTANCES.discard(instance)
    logger.info("已停用 %d 个旧插件实例", len(stale))
    return len(stale)


async def cancel_stale_background_tasks(*, timeout: float = 10.0) -> int:
    """取消其它插件实例遗留的后台循环（跨模块版本的兜底）。

    历史版本的实例可能未登记到本模块的弱引用表，但后台任务名统一带
    ``astrbot_phone_control_`` 前缀；初始化阶段取消所有这类任务即可停止
    旧实例的 keepalive/随机/持续刷循环，防止升级后仍反复重试抓包。
    """
    current = asyncio.current_task()
    stale: list[asyncio.Task] = []
    for task in asyncio.all_tasks():
        if task is current or task.done():
            continue
        try:
            name = task.get_name()
        except Exception:  # noqa: BLE001 - 任务对象异常时跳过
            continue
        if isinstance(name, str) and name.startswith(PLUGIN_TASK_PREFIX):
            stale.append(task)
    if not stale:
        return 0
    names = "、".join(sorted({task.get_name() for task in stale}))
    logger.warning("检测到 %d 个遗留插件后台循环，正在停止：%s", len(stale), names)
    for task in stale:
        task.cancel()
    _, pending = await asyncio.wait(stale, timeout=timeout)
    if pending:
        logger.warning("有 %d 个遗留后台循环未在 %.0f 秒内退出", len(pending), timeout)
    stopped = len(stale) - len(pending)
    logger.info("已停止 %d 个遗留插件后台循环", stopped)
    return stopped


class BootstrapMixin:
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict | None = None,
    ) -> None:
        super().__init__(context)
        self._terminated = False
        self._live_instance_registered = False
        self.raw_config: Any = config if config is not None else {}
        initial = PhoneConfig.from_mapping(
            self.raw_config,
            warn=lambda message: logger.warning(message),
        )
        # 必填配置缺失时直接抛异常：插件加载失败（不构造设备对象、不注册工具）。
        require_configured(initial)
        self.data_dir = plugin_main_module()._resolve_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        adb_home = self.data_dir / "adbhome"
        adb_home.mkdir(parents=True, exist_ok=True)

        self._ensure_bundled_executables()
        adb_path = self._resolve_adb_path(initial)
        self.adb = AdbClient(
            adb_path,
            initial.adb.host,
            initial.adb.port,
            adb_home,
            serial=initial.adb.serial,
            connect_timeout=initial.adb.connect_timeout,
            command_timeout=initial.adb.command_timeout,
            logger=self._device_log,
        )
        self.actions = DeviceActions(self.adb, logger=self._device_log)
        self.diary = PhoneDiary(self.data_dir / "diary")
        self.quota = TokenQuota(self.data_dir)
        self.play_budgets = PlayBudgetRegistry()
        self.chat_memory = ChatMemory(self.data_dir / "chat")
        self.interactions = InteractionStore(self.data_dir)
        self.telemetry = MetricsStore(self.data_dir)
        self._started_at = time.time()
        self._scrcpy_warmup_task: asyncio.Task | None = None
        self._last_scrcpy_warmup = 0.0
        self._screenshot_cleanup_task: asyncio.Task | None = None
        self._traffic_keepalive_task: asyncio.Task | None = None
        self._last_traffic_log = 0.0
        self._version = _read_plugin_version()
        # 自主任务模型供应商失效告警去重 / 运行前写入的会话偏好（run 后还原）
        self._task_provider_warned_ids: set[str] = set()
        self._task_provider_restore: dict[str, str] = {}
        # 自主任务（cron 路径）不写 provider_stats，用 runner stats 补精确 usage
        self._agent_usage_patch_installed = install_agent_usage_capture(
            lambda message: logger.info("%s", message),
            should_capture=self._should_capture_agent_usage,
        )
        self.service = PhoneService(
            raw_config=self.raw_config,
            adb=self.adb,
            actions=self.actions,
            diary=self.diary,
            data_dir=self.data_dir,
            device_log=self._device_log,
            quota=self.quota,
            play_budgets=self.play_budgets,
            chat_memory=self.chat_memory,
            interactions=self.interactions,
            telemetry=self.telemetry,
        )
        self.service.app_context = self.context
        self.service.task_provider_hook = self._apply_task_provider_preference
        # 流媒体内容识别：设备播放音频采集（scrcpy 音频通道，按需启动独立 server）。
        self._media_capture = MediaAudioCapture(
            adb=self.adb,
            server_jar=lambda: self._scrcpy_server_jar_path(self.service.config),
            data_dir=self.data_dir / "media",
            logger=self._device_log,
        )
        self.service.media = self._media_capture
        # 手机流量抓包（ADB + mitmproxy）：生命周期跟随插件。
        self._traffic = TrafficCapture(
            adb=self.adb,
            data_dir=self.data_dir,
            logger=logger.info,
            mitmdump=self.service.config.traffic.mitmdump_path,
        )
        self.service.traffic = self._traffic
        # 验证会话/人工接管控制器（先加载持久化状态，再启动调度器）。
        self._verification = PhoneVerification(self)
        self.service.verification = self._verification
        # 统一设备执行入口：命令、工具与定时任务接触设备前都经过它。
        self.device_gate = DeviceExecutionGate(
            authorize=self._device_authorize,
            blocking_reason=self._device_blocking_reason,
            deadline_seconds=self._device_deadline_seconds,
            autonomy_of=self._device_is_autonomous,
            log=self._device_log,
        )
        self.service.gate = self.device_gate
        # 设备动作批次内也复查门禁（暂停/取消代次/期限变化立即中止）。
        self.actions.guard = self._guard_device_action
        # App 使用经验记忆（工具/main 注入共用）与用户任务优先状态。
        self.app_memory = AppMemoryStore(self.data_dir)
        self.service.app_memory = self.app_memory
        self.service.memory_sync_hook = self._sync_memory
        # 插件加载时立即按配置清理一次残留截图（可能已占 GB 级磁盘）。
        try:
            self.service.cleanup_screenshots(force=True)
        except Exception:  # noqa: BLE001 - 清理失败不影响插件加载
            pass
        self._memory_companion_bridge_cache: Any = None
        self._livingmemory_plugin_cache: Any = None
        self._user_priority = self.service.user_priority
        self.live_screen = LiveScreenWatcher(
            capture=self._capture_live_frame,
            settings_provider=self._live_screen_settings,
            logger=self._device_log,
            interval_ms=initial.perception.live_screen_interval_ms,
            max_frames=initial.perception.live_screen_frames,
        )
        self.service.live_screen = self.live_screen
        self.backends = BackendManager(
            config_provider=lambda: self.service.config,
            actions=self.actions,
            adb=self.adb,
            data_dir=self.data_dir,
            adb_path=self._resolve_adb_path(initial),
            scrcpy_factory=self._create_scrcpy_client,
            minitouch_factory=self._create_minitouch_client,
            log=lambda message: logger.info("%s", message),
            warn=lambda message: logger.warning("%s", message),
            on_started=self._on_backends_started,
        )
        self.service.backends = self.backends
        self._backend_idle_task: asyncio.Task | None = None
        # 实时屏幕按「玩手机/后台任务会话」懒启动、任务结束/空闲自动停止。
        self._live_task_sessions: set[str] = set()
        self._live_last_activity = 0.0
        self._live_idle_task: asyncio.Task | None = None
        self.scheduler = AutoBrowseScheduler(
            self.context,
            config=initial,
            config_provider=lambda: self.service.config,
            run_browse=self._auto_browse,
        )
        self._random_task: asyncio.Task | None = None
        self._random_stop = asyncio.Event()
        # 持续刷手机：配置开启时后台循环一轮接一轮创建静默任务；/phone stop 停止。
        self._continuous_enabled = bool(initial.auto.continuous_enabled)
        self._continuous_stop = asyncio.Event()
        self._continuous_task: asyncio.Task | None = None
        self._continuous_active_job_id = ""
        self._continuous_active_since = 0.0
        self._continuous_last_finished = 0.0
        self._continuous_round_index = 0
        self._continuous_missing_learn = False
        # 正在运行的插件任务 Agent 引用（umo -> (task, job_id, origin)），/phone stop 用。
        self._active_agent_tasks: dict[str, tuple[asyncio.Task, str, str]] = {}
        self._register_tools()
        self._register_dashboard_apis()
        logger.info(
            "astrbot_phone_control 已加载：ADB %s，数据目录 %s",
            initial.adb.target,
            self.data_dir,
        )

    async def initialize(self) -> None:
        """插件激活：先停用残留旧实例，再异步同步自主浏览任务。

        AstrBot 在插件 reload 之后才启动 cron 管理器，因此这里用后台任务重试，
        避免 ``add_active_job`` 在调度器尚未 start 时抛错。
        """
        # 纵深防御：AstrBot 只探测插件类自身 __dict__ 里的 terminate，历史实例
        # 可能未被停用；先关闭登记过的旧实例并取消其余遗留后台循环，再启动本实例。
        # 同一实例重复 initialize 时不再重复清理（避免取消自己刚启动的循环）。
        if not getattr(self, "_live_instance_registered", False):
            await close_stale_instances(self)
            await cancel_stale_background_tasks()
            LIVE_PLUGIN_INSTANCES.add(self)
            self._live_instance_registered = True
        if not self.service.configured:
            logger.warning("未配置 ADB 地址/端口，跳过自主浏览任务同步")
            return
        old_task = getattr(self, "_auto_config_task", None)
        if old_task is not None and not old_task.done():
            old_task.cancel()
        self._auto_config_task = asyncio.create_task(
            self._apply_auto_config_when_ready(),
            name="astrbot_phone_control_auto_config",
        )
        # 验证状态必须先恢复：存在人工等待/待确认会话时，不启动任何手机调度器。
        try:
            await self._verification.startup()
            self._verification.start_watchdog()
        except Exception as exc:  # noqa: BLE001 - 恢复失败按暂停处理
            logger.warning("验证状态恢复失败：%s", exc)
        if self._verification.paused:
            logger.warning(
                "验证：存在待人工协助/待确认会话，手机自动操作保持暂停（/phone verify status 查看）"
            )
        # 抓包必须先于自刷/随机循环就位：App 进程需在 CA 注入后启动才能被解密。
        try:
            await asyncio.wait_for(self._ensure_traffic_started(), timeout=60)
        except asyncio.TimeoutError:
            logger.warning("抓包启动超时（60s），先启动自刷循环；后续保活会重试")
        except Exception as exc:  # noqa: BLE001 - 抓包失败不能阻塞插件启用
            logger.warning("抓包启动失败：%s", exc)
        self._start_traffic_keepalive()
        if not self._verification.paused:
            self._start_random_play_loop()
            self._start_screenshot_cleanup_loop()
            if self._continuous_enabled:
                self._start_continuous_loop()
        else:
            logger.warning("验证暂停中：不启动随机/持续刷手机，等待用户处理")
        # 实时屏幕不常驻：等玩手机/后台任务会话第一次调用手机工具时再懒启动。
        logger.debug("实时屏幕：未启动（仅在任务运行期间按需启动）")

    async def terminate(self) -> None:
        """插件停用/重载：停止后台任务并释放 adb 子进程资源（幂等）。"""
        if getattr(self, "_terminated", False):
            logger.info("插件实例已停用，跳过重复终止")
            return
        self._terminated = True
        try:
            await self._terminate_impl()
        finally:
            LIVE_PLUGIN_INSTANCES.discard(self)
            self._live_instance_registered = False

    async def _terminate_impl(self) -> None:
        """终止流程实现：取消后台任务、恢复设备状态并关闭资源。"""
        if getattr(self, "_agent_usage_patch_installed", False):
            try:
                uninstall_agent_usage_capture()
            except Exception:  # noqa: BLE001 - 还原失败不影响卸载
                pass
            self._agent_usage_patch_installed = False
        random_task = getattr(self, "_random_task", None)
        self._random_task = None
        self._random_stop.set()
        if random_task is not None and not random_task.done():
            random_task.cancel()
            try:
                await random_task
            except asyncio.CancelledError:
                pass
        self._continuous_stop.set()
        continuous_task = getattr(self, "_continuous_task", None)
        self._continuous_task = None
        if continuous_task is not None and not continuous_task.done():
            continuous_task.cancel()
            try:
                await continuous_task
            except asyncio.CancelledError:
                pass
        task = getattr(self, "_auto_config_task", None)
        self._auto_config_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        traffic_task = getattr(self, "_traffic_keepalive_task", None)
        self._traffic_keepalive_task = None
        if traffic_task is not None and not traffic_task.done():
            traffic_task.cancel()
            try:
                await traffic_task
            except asyncio.CancelledError:
                pass
        try:
            await self._traffic.stop()
        except Exception as exc:  # noqa: BLE001 - 抓包清理失败不影响停用
            logger.warning("抓包停止失败：%s", exc)
        try:
            await self._verification.shutdown()
        except Exception as exc:  # noqa: BLE001 - 验证状态保存失败不影响停用
            logger.warning("验证状态保存失败：%s", exc)
        cleanup_task = getattr(self, "_screenshot_cleanup_task", None)
        self._screenshot_cleanup_task = None
        if cleanup_task is not None and not cleanup_task.done():
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
        backend_task = getattr(self, "_backend_idle_task", None)
        self._backend_idle_task = None
        if backend_task is not None and not backend_task.done():
            backend_task.cancel()
            try:
                await backend_task
            except asyncio.CancelledError:
                pass
        try:
            self.service.user_priority.clear()
            self.device_gate.force_release("plugin_terminate")
        except Exception:  # noqa: BLE001 - 清理失败不影响停用
            pass
        try:
            await self._stop_live_screen("插件停用")
        except Exception as exc:  # noqa: BLE001 - 停止失败只记录
            logger.warning("停止实时屏幕缓存失败：%s", exc)
        warmup_task = getattr(self, "_scrcpy_warmup_task", None)
        self._scrcpy_warmup_task = None
        if warmup_task is not None and not warmup_task.done():
            warmup_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await warmup_task
        backends = getattr(self, "backends", None)
        if backends is not None:
            try:
                await backends.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("停止控制后端失败：%s", exc)
        try:
            await self.scheduler.close()
        finally:
            if self.adb is not None:
                try:
                    await self.adb.close()
                except Exception as exc:  # noqa: BLE001 - 关闭失败只记录
                    logger.warning("关闭 ADB 客户端失败：%s", exc)

    def _ensure_bundled_executables(self) -> None:
        """修复市场/zip 安装丢失的可执行位（adb 需要 +x 才能启动）。"""
        bundled = plugin_root_dir() / BUNDLED_ADB
        if bundled.is_file() and not ensure_executable(bundled):
            logger.warning("无法设置插件自带 adb 的可执行权限：%s", bundled)

    def _resolve_adb_path(self, config: PhoneConfig) -> Path:
        """解析 adb 可执行文件：配置优先，其次插件自带二进制。"""
        configured = str(config.adb.path or "").strip()
        if configured:
            candidate = Path(configured).expanduser()
            if candidate.is_file():
                return candidate
            logger.warning("配置的 adb.path 不存在：%s，回退到插件自带 adb", candidate)
        bundled = plugin_root_dir() / BUNDLED_ADB
        if bundled.is_file():
            return bundled
        logger.warning("插件自带 adb 不存在：%s，尝试从 PATH 查找 adb", bundled)
        return Path("adb")

    def _device_log(self, message: str) -> None:
        """设备层日志回调（受 debug.log_actions 控制）。"""
        try:
            enabled = PhoneConfig.from_mapping(self.raw_config).debug.log_actions
        except Exception:  # noqa: BLE001 - 配置异常时退化为 debug 日志
            enabled = False
        if enabled:
            logger.info("[手机] %s", message)
        else:
            logger.debug("[手机] %s", message)

    def _register_tools(self) -> None:
        """构造并注册全部 LLM 工具。"""
        try:
            tools = build_phone_tools(self.service)
            self.context.add_llm_tools(*tools)
            logger.info("astrbot_phone_control 已注册 %d 个 LLM 工具", len(tools))
        except Exception:  # noqa: BLE001 - 工具注册失败时命令仍可用，必须留痕
            logger.exception("注册手机 LLM 工具失败")

    def _remember_event_session(self, event: AstrMessageEvent) -> None:
        """把用户会话 UMO 记录到插件数据目录（跳过 cron / webchat 匿名会话）。"""
        try:
            platform = str(event.get_platform_name() or "").lower()
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            platform = ""
        if platform in {"cron", "webchat"}:
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if umo:
            self.service.remember_session(umo)

    def _start_screenshot_cleanup_loop(self) -> None:
        """启动截图定时清理循环（60 秒一次，读实时配置）。"""
        task = getattr(self, "_screenshot_cleanup_task", None)
        if task is not None and not task.done():
            return
        try:
            self._screenshot_cleanup_task = asyncio.create_task(
                self._screenshot_cleanup_loop(),
                name="astrbot_phone_control_screenshot_cleanup",
            )
        except RuntimeError:  # 没有运行中的事件循环（单测直接调用）
            self._screenshot_cleanup_task = None

    async def _screenshot_cleanup_loop(self) -> None:
        """每 60 秒按配置清理一次过期截图（关闭时跳过）。"""
        while True:
            try:
                await asyncio.sleep(60)
                self.service.cleanup_screenshots()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 清理失败只记录
                logger.warning("截图自动清理循环异常：%s", exc)

    def _config_snapshot(self) -> PhoneConfig:
        """把当前内存配置解析为快照（保存前用于比较变化）。"""
        try:
            return PhoneConfig.from_mapping(
                self._plain_config(self.raw_config),
                warn=lambda message: logger.warning(message),
            )
        except Exception as exc:  # noqa: BLE001 - 配置异常时按默认快照比较
            logger.warning("读取当前配置快照失败：%s", exc)
            return PhoneConfig()

    def _persist_config(self, merged: dict[str, Any]) -> None:
        """把完整配置写回（命令/原生配置页共用）。

        优先复用 AstrBot 下发的 ``AstrBotConfig.save_config``：与框架原生保存同格式、
        同原子写、同步 save revision，避免插件直接写文件绕过框架；只有测试或框架下发的
        ``raw_config`` 是普通 dict 时才回退到原子写文件。
        """
        target = self.raw_config
        if isinstance(target, dict):
            target.clear()
            target.update(merged)
        else:  # pragma: no cover - AstrBot 正常下发 dict
            target = merged
            self.raw_config = target
        save = getattr(target, "save_config", None)
        if callable(save):
            try:
                save()
                self.service._raw_config = target
                return
            except Exception as exc:  # noqa: BLE001 - 回退到直接写文件
                logger.warning(
                    "AstrBotConfig.save_config 失败，回退直接写配置文件：%s", exc
                )
        path = self._config_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
        self.service._raw_config = self.raw_config

    async def _apply_runtime_config(
        self, previous: PhoneConfig, notes: list[str]
    ) -> None:
        """把保存后的配置变化同步应用到已启动的组件（保存即生效）。

        AstrBot 原生插件配置页保存后框架会重载插件；插件运行中的命令（如
        ``/phone continuous``、``/phone traffic``）不能重启自己，因此按
        :mod:`core.config_apply` 的差异清单重建受影响组件：ADB 连接参数、控制/感知
        后端、持续刷手机循环、实时屏幕 watcher、随机玩手机排程与自主浏览 cron。
        其余配置项每次使用时实时读取，无需额外动作。
        """
        try:
            current = self.service.config
        except Exception as exc:  # noqa: BLE001 - 配置异常时只记录
            logger.warning("配置热应用失败（读取新配置）：%s", exc)
            notes.append("配置已保存，但读取新配置失败，请重载插件")
            return
        plan = build_apply_plan(previous, current)
        notes.extend(plan.notes)

        # 1) ADB 连接参数：断开旧目标并按新参数重配，后端一并重置。
        if plan.adb_changed:
            await self._reset_backends(notes)
            if current.adb.configured:
                try:
                    await self.adb.reconfigure(
                        adb_path=self._resolve_adb_path(current),
                        host=current.adb.host,
                        port=current.adb.port,
                        serial=current.adb.serial,
                        connect_timeout=current.adb.connect_timeout,
                        command_timeout=current.adb.command_timeout,
                    )
                    logger.info("配置热应用：ADB 目标已切换为 %s", current.adb.target)
                except Exception as exc:  # noqa: BLE001 - 重配失败如实报告
                    logger.warning("配置热应用：重配 ADB 失败：%s", exc)
                    notes.append(f"ADB 连接参数已保存，但重配失败：{exc}（可重载插件）")
            else:
                logger.warning("配置热应用：ADB 地址/端口未配置完整，手机能力已停用")

        # 2) 控制/感知后端参数变化：重置当前会话，下次操作按新配置重建。
        elif plan.backends_changed:
            await self._reset_backends(notes)

        # 3) 持续刷手机开关。
        if plan.continuous_action == ACTION_START:
            self._continuous_enabled = True
            self._continuous_last_finished = 0.0
            try:
                self._start_continuous_loop()
                logger.info("配置热应用：持续刷手机已开启，后台循环已启动")
            except Exception as exc:  # noqa: BLE001 - 启动失败只记录
                logger.warning("配置热应用：启动持续刷手机失败：%s", exc)
                notes.append(f"持续刷手机已保存，但循环启动失败：{exc}")
        elif plan.continuous_action == ACTION_STOP:
            self._continuous_enabled = False
            logger.info("配置热应用：持续刷手机已关闭（当前一轮结束后不再开新轮）")

        # 4) 任务期间持续截图（实时屏幕 watcher）。
        if plan.live_screen_action == ACTION_STOP:
            try:
                await self._stop_live_screen("配置关闭任务期间持续截图")
                logger.info("配置热应用：实时屏幕 watcher 已停止")
            except Exception as exc:  # noqa: BLE001 - 停止失败只记录
                logger.warning("配置热应用：停止实时屏幕失败：%s", exc)
        elif plan.live_screen_action == ACTION_START:
            if self._live_task_sessions:
                try:
                    if not self.live_screen.running:
                        self.live_screen.start()
                        self._start_live_idle_monitor()
                    logger.info("配置热应用：实时屏幕 watcher 已启动")
                except Exception as exc:  # noqa: BLE001 - 启动失败只记录
                    logger.warning("配置热应用：启动实时屏幕失败：%s", exc)
            else:
                notes.append("当前没有运行中的手机任务，持续截图将在下次任务开始时启用")

        # 5) 随机玩手机：按新间隔/概率重新算下一次触发时间。
        if plan.random_changed:
            await self._restart_random_play_loop()

        # 6) 自主浏览 cron：按新配置同步任务（关闭时不删除已有任务）。
        if plan.scheduler_changed and current.auto.enabled:
            try:
                await self.scheduler.apply_config()
                logger.info("配置热应用：自主浏览 cron 任务已按新配置同步")
            except Exception as exc:  # noqa: BLE001 - 同步失败只记录
                logger.warning("配置热应用：同步自主浏览任务失败：%s", exc)
                notes.append(f"自主浏览配置已保存，但同步 cron 任务失败：{exc}")

        # 7) 截图清理参数变化：立即按新保留时长执行一次清理。
        if (
            previous.perception.screenshot_auto_clean
            != current.perception.screenshot_auto_clean
            or previous.perception.screenshot_retention_minutes
            != current.perception.screenshot_retention_minutes
        ):
            try:
                cleaned = self.service.cleanup_screenshots(force=True)
                notes.append(
                    f"截图自动清理：已按保留 {current.perception.screenshot_retention_minutes} "
                    f"分钟清理 {cleaned.get('deleted', 0)} 个旧截图"
                )
            except Exception as exc:  # noqa: BLE001 - 清理失败只记录
                logger.warning("配置热应用：截图清理失败：%s", exc)

        # 8) 流量抓包：开关/端口/正文长度变化 → 停止后按新配置重启。
        if (
            previous.traffic.enabled != current.traffic.enabled
            or previous.traffic.port != current.traffic.port
            or previous.traffic.body_chars != current.traffic.body_chars
        ):
            try:
                if current.traffic.enabled:
                    # 配置变化后允许重新尝试（清除确定性不可用/退避状态）。
                    self._traffic.reset_unavailable()
                if self._traffic.running:
                    await self._traffic.stop()
                await self._ensure_traffic_started()
                if current.traffic.enabled and self._traffic.running:
                    notes.append(
                        f"抓包：已按新配置启动（端口 {current.traffic.port}）"
                    )
                elif current.traffic.enabled:
                    status = self._traffic.status()
                    reason = str(
                        status.get("unavailable_reason")
                        or status.get("error")
                        or "未提供原因"
                    )
                    notes.append(f"抓包：启动失败（{reason}）")
                else:
                    notes.append("抓包：已停止")
            except Exception as exc:  # noqa: BLE001 - 抓包重启失败只记录
                logger.warning("配置热应用：抓包重启失败：%s", exc)
                notes.append("抓包：重启失败（详见插件日志）")

        if plan.changed:
            logger.info("配置热应用完成：%s", "；".join(plan.notes) or "运行态已更新")

    async def _reset_backends(self, notes: list[str]) -> None:
        """停止当前控制/感知后端会话，让下次手机操作按新配置重建。"""
        manager = getattr(self, "backends", None)
        if manager is None:
            return
        try:
            await manager.stop()
        except Exception as exc:  # noqa: BLE001 - 重置失败如实报告
            logger.warning("配置热应用：重置控制/感知后端失败：%s", exc)
            notes.append(f"控制/感知后端重置失败：{exc}（可重载插件）")

    async def _apply_config_patch(
        self, patch: dict[str, Any], *, notes: list[str] | None = None
    ) -> list[str]:
        """把配置补丁持久化并热应用（命令/原生配置页共用的统一入口）。"""
        result: list[str] = notes if notes is not None else []
        saved = False
        try:
            previous = self._config_snapshot()
            current = self._plain_config(self.raw_config)
            merged = self._deep_merge(current, patch)
            self._persist_config(merged)
            await self._apply_runtime_config(previous, result)
            saved = True
        except Exception as exc:  # noqa: BLE001 - 持久化失败只记录，内存仍生效
            logger.warning("保存插件配置失败：%s", exc)
            result.append(f"配置保存失败：{exc}")
        if saved:
            result.append("配置已保存并即时生效")
        for note in result:
            logger.info("配置更新：%s", note)
        return result

    def _config(self) -> PhoneConfig:
        return self.service.config

    def _config_file_path(self) -> Path:
        try:
            base = Path(get_astrbot_config_path())
        except Exception:  # noqa: BLE001 - 路径异常回退数据目录
            base = self.data_dir
        return base / f"{PLUGIN_NAME}_config.json"

    @staticmethod
    def _plain_config(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): BootstrapMixin._plain_config(item)  # type: ignore[name-defined]
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [BootstrapMixin._plain_config(item) for item in value]  # type: ignore[name-defined]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        try:
            return str(value)
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _deep_merge(base: dict, patch: dict) -> dict:
        result = dict(base)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = BootstrapMixin._deep_merge(result[key], value)  # type: ignore[name-defined]
            else:
                result[key] = value
        return result
