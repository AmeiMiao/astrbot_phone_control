"""休闲模式的任务提示在各自主入口明确精读与学习均可选。"""
from core.continuous import build_continuous_note
from core.random_play import build_play_note, build_random_play_note


def test_free_browsing_in_every_autonomous_entry():
    notes = [build_continuous_note(break_rule=True), build_play_note(100, break_rule=True), build_random_play_note(break_rule=True)]
    for note in notes:
        assert '自由浏览' in note and 'phone_watch_media' in note
        assert 'phone_content 精读流程与 phone_learn 按需使用' in note
        assert '每篇内容独立任务' not in note
        assert '必须至少调用一次 phone_learn' not in note


def test_default_continuous_mode_keeps_learning_requirement():
    assert '必须至少调用一次 phone_learn' in build_continuous_note()
    assert '每篇内容独立任务' in build_play_note(100)
