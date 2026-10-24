"""次数迁移保留既有无限额度，显式 0 在更新后持续生效。"""

import json
from core.interaction_config import migrate_interaction_file, migrate_interaction_limits
from core.config import PhoneConfig


def test_limits_migrate_once_and_preserve_backup(tmp_path):
    path = tmp_path / "config.json"
    original = json.dumps({"security": {"max_comments_per_day": 0, "max_posts_per_day": 2, "max_passive_replies_per_day": "0"}}).encode()
    path.write_bytes(original)
    assert migrate_interaction_file(path)
    assert path.with_name(path.name + ".interaction-limits-backup").read_bytes() == original
    raw = json.loads(path.read_text())
    assert raw["security"] == {"max_comments_per_day": -1, "max_posts_per_day": 2, "max_passive_replies_per_day": -1}
    raw["security"]["max_comments_per_day"] = 0
    assert not migrate_interaction_limits(raw)
    assert PhoneConfig.from_mapping(raw).security.max_comments_per_day == 0
    assert not migrate_interaction_file(path)
