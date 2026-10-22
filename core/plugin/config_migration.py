"""配置迁移的独立面板接口，运行配置仍由宿主插件装配。"""

from __future__ import annotations

import asyncio
import copy
import json
from contextlib import AsyncExitStack, ExitStack

from ..data_migration import (
    DataMigrationError,
    apply_data_migration,
    prepare_data_migration,
)
from ..config_migration import SOURCE_PLUGIN
from ..saved_images import image_library

from ..config import PhoneConfig, require_configured
from ..errors import PhoneControlError
from ..config_migration import ConfigMigrationError, apply_migration, prepare_migration
from ..identity import PLUGIN_NAME, PLUGIN_ROOT
from .compat import error_response, json_response, plugin_main_module


class ConfigMigrationMixin:
    def _config_migration_routes(self, base):
        return (
            (
                f"{base}/data-migration",
                self.dashboard_data_migration_preview,
                ["GET"],
                "预览原插件持久化资料迁移",
            ),
            (
                f"{base}/data-migration",
                self.dashboard_data_migration_apply,
                ["POST"],
                "导入原插件持久化资料",
            ),
            (
                f"{base}/config-migration",
                self.dashboard_config_migration_preview,
                ["GET"],
                "预览原插件配置迁移",
            ),
            (
                f"{base}/config-migration",
                self.dashboard_config_migration_apply,
                ["POST"],
                "导入原插件配置",
            ),
        )

    def _migration_inputs(self):
        target = self._config_file_path()
        if target.name != f"{PLUGIN_NAME}_config.json":
            raise ConfigMigrationError("当前配置文件不支持自动定位原插件配置")
        schema = json.loads(
            (PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )
        return target.parent, schema

    @staticmethod
    def _validate_migration(value):
        warnings = []
        config = PhoneConfig.from_mapping(value, warn=warnings.append)
        try:
            require_configured(config)
        except PhoneControlError as exc:
            raise ConfigMigrationError("配置的 ADB 地址或端口无效") from exc
        if warnings:
            raise ConfigMigrationError("配置中存在无效类型、枚举或数值，请先修正原配置")

    async def dashboard_config_migration_preview(self):
        try:
            directory, schema = self._migration_inputs()
            plan = prepare_migration(
                directory, PLUGIN_NAME, schema, validate=self._validate_migration
            )
            return json_response(plan.summary())
        except ConfigMigrationError as exc:
            return error_response(str(exc), status_code=400)
        except (ValueError, OSError):
            return error_response(
                "无法预览迁移，请检查原配置文件及字段格式", status_code=400
            )

    async def dashboard_config_migration_apply(self):
        request = plugin_main_module().request
        try:
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("token"), str):
                raise ConfigMigrationError("请先预览原插件配置，再确认导入")
            if not hasattr(self, "_config_migration_lock"):
                self._config_migration_lock = asyncio.Lock()
            async with self._config_migration_lock:
                directory, schema = self._migration_inputs()
                previous = self._config_snapshot()
                before = copy.deepcopy(dict(self.raw_config))

                def persist(merged):
                    self.raw_config.clear()
                    self.raw_config.update(copy.deepcopy(merged))
                    try:
                        save = getattr(self.raw_config, "save_config", None)
                        if callable(save):
                            save()
                        else:
                            from ..config_migration import _atomic_write

                            payload = (
                                json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
                            ).encode()
                            _atomic_write(
                                directory / f"{PLUGIN_NAME}_config.json", payload
                            )
                    except Exception as exc:
                        self.raw_config.clear()
                        self.raw_config.update(before)
                        raise ConfigMigrationError(
                            "保存迁移配置失败，运行配置保持原值"
                        ) from exc
                    self.service._raw_config = self.raw_config

                plan = apply_migration(
                    directory,
                    PLUGIN_NAME,
                    schema,
                    body["token"],
                    validate=self._validate_migration,
                    persist=persist,
                )
                if plan.changed:
                    await self._apply_runtime_config(previous, [])
                result = plan.summary()
                result["message"] = (
                    f"已导入 {len(plan.changed)} 项配置；当前自定义设置与原插件配置保持完整"
                )
                return json_response(result)
        except ConfigMigrationError as exc:
            return error_response(str(exc), status_code=400)
        except (ValueError, OSError):
            return error_response(
                "导入配置失败，请检查原配置与存储权限", status_code=500
            )

    def _data_migration_inputs(self):
        if self.data_dir.name != PLUGIN_NAME:
            raise DataMigrationError("当前数据目录不支持自动定位原插件资料")
        return self.data_dir.parent

    def _check_data_migration_idle(self):
        lookup = getattr(self.context, "get_registered_star", None)
        if not callable(lookup):
            raise DataMigrationError("无法查询原插件状态，请检查 AstrBot 插件管理器")
        source = lookup(SOURCE_PLUGIN)
        if source is not None and getattr(source, "activated", True):
            raise DataMigrationError("请先在插件管理中停用原插件，再导入资料")
        config = self.service.config
        if any(
            (
                config.auto.enabled,
                config.auto.random_enabled,
                config.auto.continuous_enabled,
                config.wireguard.enabled,
                config.traffic.enabled,
            )
        ):
            raise DataMigrationError("请先关闭自主任务、WireGuard 和抓包，再导入资料")
        active = getattr(self, "_active_agent_tasks", {})
        contents = getattr(self.service, "_contents", None)
        if (
            self.device_gate.busy
            or self.service.user_priority.active
            or any(not item[0].done() for item in active.values())
            or (contents is not None and contents.active)
        ):
            raise DataMigrationError("手机任务正在运行，请完成或停止任务后再导入资料")

    def _reload_migrated_data(self):
        for store in (
            self.service.learning,
            self.app_memory,
            self.service.novelty,
            self.telemetry,
        ):
            store._entries.clear()
            store._load()

    async def dashboard_data_migration_preview(self):
        try:
            query = plugin_main_module().request.query
            option = query.get("include_keys", "false")
            if option not in ("true", "false"):
                raise DataMigrationError("密钥选项必须为 true 或 false")
            plan = prepare_data_migration(
                self._data_migration_inputs(),
                PLUGIN_NAME,
                include_keys=option == "true",
            )
            return json_response(plan.summary())
        except (ConfigMigrationError, ValueError, OSError) as exc:
            # 路径和格式错误只显示可预期文案，不回显资料内容。
            message = (
                str(exc)
                if isinstance(exc, ConfigMigrationError)
                else "无法读取迁移资料，请检查格式与文件权限"
            )
            return error_response(message, status_code=400)

    async def dashboard_data_migration_apply(self):
        try:
            body = await plugin_main_module().request.json()
            if (
                not isinstance(body, dict)
                or not isinstance(body.get("token"), str)
                or not isinstance(body.get("include_keys", False), bool)
            ):
                raise DataMigrationError("请先预览资料，再确认导入")
            if not hasattr(self, "_config_migration_lock"):
                self._config_migration_lock = asyncio.Lock()
            async with self._config_migration_lock:
                self._check_data_migration_idle()
                async with AsyncExitStack() as locks:
                    await locks.enter_async_context(self.diary._lock)
                    await locks.enter_async_context(image_library(self.service).lock)
                    await locks.enter_async_context(self.service.experiences.lock)
                    self._check_data_migration_idle()
                    # 后续文件事务为同步操作，事件循环内不会交错写入；线程存储锁同时保护缓存。
                    with ExitStack() as threads:
                        for store in (
                            self.service.learning,
                            self.app_memory,
                            self.service.novelty,
                            self.telemetry,
                        ):
                            threads.enter_context(store._lock)
                        plan = apply_data_migration(
                            self._data_migration_inputs(),
                            PLUGIN_NAME,
                            body["token"],
                            include_keys=body.get("include_keys", False),
                            reload=self._reload_migrated_data,
                        )
                result = plan.summary()
                result["message"] = (
                    f"已导入 {len(plan.writes)} 个资料文件，缓存已刷新；当前记录与原插件资料保持完整"
                )
                return json_response(result)
        except ConfigMigrationError as exc:
            return error_response(str(exc), status_code=400)
        except (ValueError, OSError):
            return error_response("资料导入失败，请检查存储权限与格式", status_code=500)
