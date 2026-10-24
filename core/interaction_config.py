"""互动次数配置的版本化转换与持久化备份。"""

import json
from pathlib import Path

from .config_migration import _atomic_write, _read

LIMIT_KEYS = ("max_comments_per_day", "max_passive_replies_per_day", "max_posts_per_day")


def migrate_interaction_limits(raw):
    """一次性将未标记配置的无限次数转换为 -1；标记后的 0 保持禁止。"""
    if raw.get("interaction_limits_version") == 1:
        return False
    for card in (raw, raw.get("security", {})):
        if isinstance(card, dict):
            for key in LIMIT_KEYS:
                if key in card and str(card[key]).strip() == "0":
                    card[key] = -1
    raw["interaction_limits_version"] = 1
    return True


def migrate_interaction_file(path):
    """在框架整理配置之前备份并转换次数单位。"""
    path = Path(path)
    raw, original = _read(path, optional=True)
    if not original or not migrate_interaction_limits(raw):
        return False
    backup = path.with_name(path.name + ".interaction-limits-backup")
    if not backup.exists():
        _atomic_write(backup, original)
    _atomic_write(path, (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode())
    return True
