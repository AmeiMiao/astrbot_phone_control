"""原生自主排程使用实际弹性配置与直播决策提示。"""

from core.config import PhoneConfig
from core.scheduler import AutoBrowseScheduler


def test_native_auto_note_uses_live_and_effective_flexibility_limits():
    config = PhoneConfig.from_mapping({"security": {"allow_posts": True, "max_posts_per_day": 1},
                                       "flexibility": {"enabled": True, "max_posts_per_day": 4}})
    scheduler = AutoBrowseScheduler(None, config=config)
    note = scheduler._note("bilibili", 6, "")
    assert "直播没有可回放的0秒起点" in note
    assert "post：已授权，每日上限4条，冷却0秒" in note
    assert "自由浏览已开启" in note
    assert "无需等待用户另外下任务" in note
