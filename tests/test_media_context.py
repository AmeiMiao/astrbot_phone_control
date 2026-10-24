"""连续观看保留同一任务的已理解上下文，控制长度和时间归属。"""
from core.media_context import video_context


def test_followup_contains_previous_summary_with_time_range():
    task={'summaries':['前段出现小林，正在等待开门。'],'summary_ranges':[[0,8]]}
    text=video_context(task)
    assert '小林' in text and '0–8 秒' in text
    assert '不是本段新素材' in text
    assert video_context({'summaries':[]}) == ''


def test_long_context_keeps_opening_and_recent_segments_with_bound():
    task={'summaries':[f'片段{i}：'+str(i)*2000 for i in range(12)],'summary_ranges':[[i*8,(i+1)*8] for i in range(12)]}
    text=video_context(task)
    assert len(text)<=3500
    assert '片段0：' in text and '片段9：' in text and '片段11：' in text
    assert '片段5：' not in text
    assert '88–96 秒' in text
