"""配置迁移的独立面板接口，运行配置仍由宿主插件装配。"""

from __future__ import annotations

import asyncio
import copy
import json

from ..config import PhoneConfig, require_configured
from ..errors import PhoneControlError
from ..config_migration import ConfigMigrationError, apply_migration, prepare_migration
from ..identity import PLUGIN_NAME, PLUGIN_ROOT
from .compat import error_response, json_response, plugin_main_module


class ConfigMigrationMixin:
    def _config_migration_routes(self, base):
        return (
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
