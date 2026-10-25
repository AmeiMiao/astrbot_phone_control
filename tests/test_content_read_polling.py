"""内容 read 在 Agent 中轮询同一分析请求，结束即取消并清理。"""
import asyncio
import json
from types import SimpleNamespace

from core.content_task import ContentTasks
from core.tools.content_task import PhoneContentTool
from test_content_tasks import service
from test_tools_gate import FakeService, FakeEvent, FakeToolContext


def test_content_tool_poll_keeps_one_reader_and_same_result(tmp_path):
    async def run():
        s = service(tmp_path)
        manager = ContentTasks(s)
        event = FakeEvent()
        event.set_extra('phone_agent_run_active', True)
        task = await manager.begin(event, kind='post', content_id='post', capture=False)
        ready = asyncio.Event()
        calls = []
        async def read(event, task_id='', seconds=0):
            calls.append(task_id)
            await ready.wait()
            task['cursor'] = 1
            task['summaries'].append('实际理解')
            return {'task_id': task['id'], 'summary': '实际理解'}
        manager._read = read
        original_poll = manager.poll_watch
        async def poll(*args, **kwargs):
            return await original_poll(*args, **kwargs, wait_seconds=0)
        manager.poll_watch = poll
        tool_service = FakeService()
        tool_service.contents = manager
        tool = PhoneContentTool(service=tool_service)
        context = FakeToolContext(event)
        first = json.loads(await tool.call(context, action='read', task_id=task['id']))
        await asyncio.sleep(0)
        second = json.loads(await tool.call(context, action='read', task_id=task['id']))
        assert first['status'] == second['status'] == 'processing'
        assert 'phone_content(action=read)' in first['next']
        assert calls == [task['id']] and task['cursor'] == 0
        ready.set()
        await asyncio.sleep(0)
        result = json.loads(await tool.call(context, action='read', task_id=task['id']))
        assert result['summary'] == '实际理解' and task['summaries'] == ['实际理解']
        assert not manager.watches and '_reader' not in task
        await manager.close()
    asyncio.run(run())


def test_finish_cancels_inflight_content_read_and_reclaims_material(tmp_path):
    async def run():
        manager = ContentTasks(service(tmp_path))
        event = FakeEvent()
        task = await manager.begin(event, kind='post', content_id='post', capture=False)
        material = manager.root / task['id'] / 'image.png'
        material.write_bytes(b'image')
        cancelled = asyncio.Event()
        async def read(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        manager._read = read
        async def run_read():
            return json.dumps(await manager.read(event))
        await manager.poll_watch(event, run_read, wait_seconds=0, tool_name='phone_content(action=read)')
        await asyncio.sleep(0)
        assert manager.watches and task['_reader']
        await manager.finish_owner(manager.owner(event), abnormal=True)
        assert cancelled.is_set() and not manager.watches and not manager.active
        assert not material.parent.exists()
        await manager.close()
    asyncio.run(run())
