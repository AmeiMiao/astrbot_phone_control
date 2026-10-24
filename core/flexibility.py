"""自主规则弹性的快照与配置迁移。"""

from dataclasses import dataclass
import copy
import json
from pathlib import Path

from .config_migration import _atomic_write, _read


@dataclass(frozen=True)
class FlexibilitySettings:
    enabled: bool = False
    max_comments_per_day: int = -1
    max_passive_replies_per_day: int = -1
    max_posts_per_day: int = -1
    ignore_interaction_cooldown: bool = True
    free_browse: bool = True
    relax_interest_novelty: bool = True
    extend_task_budget: bool = True

    @property
    def free_browse_active(self):
        return self.enabled and self.free_browse

    @property
    def interest_active(self):
        return self.enabled and self.relax_interest_novelty

    @property
    def budget_active(self):
        return self.enabled and self.extend_task_budget


def migrate_flexibility(raw):
    """独立保存自主规则弹性，明确选择优先。"""
    before = copy.deepcopy(dict(raw))
    auto = raw.get("auto")
    if isinstance(auto, dict) and "break_rule" in auto:
        enabled = auto.pop("break_rule")
        target = raw.setdefault("flexibility", {})
        if isinstance(target, dict):
            target.setdefault("enabled", enabled)
    return raw != before


def migrate_flexibility_file(path):
    path = Path(path)
    raw, original = _read(path, optional=True)
    if not original or not migrate_flexibility(raw):
        return False
    backup = path.with_name(path.name + ".flexibility-backup")
    if not backup.exists():
        _atomic_write(backup, original)
    _atomic_write(path, (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode())
    return True
