"""自主模式的配置迁移、互斥选择与补丁归一化。"""

import copy
import json
from pathlib import Path

from .config_storage import _atomic_write, _read

MODES = ("随机玩", "自动刷", "定时玩")
FLAGS = {"random_enabled": "随机玩", "continuous_enabled": "自动刷", "enabled": "定时玩"}


def _enabled(value):
    return value is True or (isinstance(value, (str, int)) and str(value).lower() in {"1", "true", "yes", "on", "y", "t"})


def selected_mode(auto):
    """显式模式优先，导入独立开关时按持续、定时、随机顺序归组。"""
    if "mode" in auto:
        mode = auto["mode"]
        if mode not in MODES:
            raise ValueError("auto.mode 必须选择随机玩、自动刷或定时玩")
        return mode, _enabled(auto.get("activity_enabled", True))
    if _enabled(auto.get("continuous_enabled", False)):
        return "自动刷", True
    if _enabled(auto.get("enabled", False)):
        return "定时玩", True
    return "随机玩", _enabled(auto.get("random_enabled", True))


def migrate_autonomy(raw):
    before = copy.deepcopy(dict(raw))
    auto = raw.setdefault("auto", {})
    if not isinstance(auto, dict):
        return False
    mode, active = selected_mode(auto)
    auto.update(mode=mode, activity_enabled=active)
    for key in FLAGS:
        auto.pop(key, None)
    return raw != before


def migrate_autonomy_file(path):
    """在框架整理 schema 前备份配置并原子迁移。"""
    path = Path(path)
    value, original = _read(path, optional=True)
    if not original or not migrate_autonomy(value):
        return False
    backup = path.with_name(path.name + ".autonomy-backup")
    if not backup.exists():
        _atomic_write(backup, original)
    _atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
    return True


def normalize_autonomy_patch(current, patch):
    """部分补丁保留当前模式；导入的独立开关转换成显式模式。"""
    patch = copy.deepcopy(patch)
    auto = patch.get("auto")
    if not isinstance(auto, dict):
        return patch
    if "mode" not in auto and any(key in auto for key in FLAGS):
        # 开启某个模式的命令同时完成选择；关闭只影响当前选择。
        if any(_enabled(auto.get(key, False)) for key in FLAGS):
            auto["mode"], auto["activity_enabled"] = selected_mode(auto)
        else:
            mode, active = selected_mode(current.get("auto", {}))
            chosen_key = next(key for key, label in FLAGS.items() if label == mode)
            auto["mode"] = mode
            auto["activity_enabled"] = False if chosen_key in auto else active
    for key in FLAGS:
        auto.pop(key, None)
    return patch
