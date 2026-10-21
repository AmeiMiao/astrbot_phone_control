"""帖子回顶、过期截图与页面切换保护回归。"""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core.share_screenshot as module
from core.errors import ActionFailedError
from core.models import Rect, ScreenSnapshot, UiNode


def service_fixture(monkeypatch, *, position=3, change_page=False):
    state={'position':position,'swipes':0,'captures':0}
    async def foreground():
        return ('com.other.app' if change_page and state['swipes'] else 'com.example.posts','Article')
    async def swipe(*args,**kwargs):state.update(position=max(0,state['position']-1),swipes=state['swipes']+1)
    async def dump(*args,**kwargs):
        return ScreenSnapshot(package='com.example.posts',nodes=[UiNode(ref=1,text='顶部标题' if state['position']==0 else str(state['position']),package='com.example.posts',scrollable=True,bounds=Rect(0,50,600,900))])
    async def capture(**kwargs):
        assert state['position']==0
        state['captures']+=1
        return b'image',Path('top.png'),{}
    async def sleep(*args):pass
    monkeypatch.setattr(module,'dump_ui',dump)
    monkeypatch.setattr(module.asyncio,'sleep',sleep)
    service=SimpleNamespace(actions=SimpleNamespace(foreground=foreground,swipe=swipe),adb=None,guard_device=lambda op:None,capture=capture)
    return service,state


def test_mid_article_returns_to_top_then_confirms_stable_boundary(monkeypatch):
    service,state=service_fixture(monkeypatch)
    _,path,meta=asyncio.run(module.capture_post_top(service))
    assert path.name=='top.png' and meta['position']=='post_top'
    assert state['swipes']==5 and state['captures']==1


def test_page_switch_during_scroll_does_not_capture_wrong_post(monkeypatch):
    service,state=service_fixture(monkeypatch,change_page=True)
    with pytest.raises(ActionFailedError,match='页面已切换'):asyncio.run(module.capture_post_top(service))
    assert state['captures']==0


def test_long_post_does_not_send_unconfirmed_middle_region(monkeypatch):
    service,state=service_fixture(monkeypatch,position=20)
    with pytest.raises(ActionFailedError,match='未确认帖子顶部'):asyncio.run(module.capture_post_top(service,max_swipes=2))
    assert state['captures']==0
