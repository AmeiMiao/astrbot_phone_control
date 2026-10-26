"""当前聊天附件、手机文件及手机端录制工具。"""
import json
from pydantic import Field
from pydantic.dataclasses import dataclass
from .base import _PhoneToolBase, _wrap_call
from ..errors import ActionFailedError


async def send_file(event, path):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import File
    chain = MessageChain()
    chain.chain.append(File(name=path.name, file=str(path)))
    await event.send(chain)


@dataclass
class PhoneFilesTool(_PhoneToolBase):
    name: str = 'phone_files'
    description: str = ('双向文件传输：download从手机共享存储下载，upload将当前消息文件附件或已缓存file_id上传到手机；'
                        'list查看缓存ID，delete删除指定本地传输缓存。单文件128 MiB，传输后校验SHA256。'
                        'send_to_chat=true将下载文件发送到当前聊天，不支持其他会话目标。')
    parameters: dict = Field(default_factory=lambda:{'type':'object','properties':{
        'action':{'type':'string','enum':['list','download','upload','delete']},
        'phone_path':{'type':'string','description':'手机共享存储中的绝对文件路径，含文件名'},
        'file_id':{'type':'string'},'attachment_index':{'type':'integer','minimum':0},
        'send_to_chat':{'type':'boolean','default':False}},'required':['action']})

    @_wrap_call
    async def call(self, context, **kwargs):
        action = kwargs.get('action')
        event = await (self._guard_light(context) if action in ('list','delete') else self._guard(context))
        files = self.service.files
        if action == 'list':result = {'files':files.recent()}
        elif action == 'delete':
            files.path(kwargs.get('file_id')).unlink();result={'status':'deleted'}
        elif action == 'download':
            result = await files.download(kwargs.get('phone_path') or '')
            if kwargs.get('send_to_chat'):await send_file(event,files.path(result['file_id']))
        elif action == 'upload':
            identity = kwargs.get('file_id') or await files.attachment(event,kwargs.get('attachment_index',0))
            result = await files.upload(identity,kwargs.get('phone_path') or '')
        else:raise ActionFailedError('文件action需为list/download/upload/delete')
        return json.dumps(result,ensure_ascii=False)


@dataclass
class PhoneRecordMediaTool(_PhoneToolBase):
    name: str = 'phone_record_media'
    description: str = ('在Relay手机内录制并封装音视频：start开始，status查看，stop结束并下载成品，cancel停止并清理。'
                        'mode=both音视频、video画面、audio声音；声音源按media.audio_source配置。'
                        '1–900秒且最多128 MiB，自动到期后调用stop下载。send_to_chat=true发送到当前聊天。'
                        'start不代表已理解内容；理解媒体仍用phone_watch_media。')
    parameters: dict = Field(default_factory=lambda:{'type':'object','properties':{
        'action':{'type':'string','enum':['start','status','stop','cancel']},
        'mode':{'type':'string','enum':['audio','video','both'],'default':'both'},
        'seconds':{'type':'number','minimum':1,'maximum':900,'default':60},
        'send_to_chat':{'type':'boolean','default':False}},'required':['action']})

    @_wrap_call
    async def call(self, context, **kwargs):
        action = kwargs.get('action')
        event = await (self._guard_light(context) if action in ('status','stop','cancel') else self._guard(context))
        recorder = self.service.recordings
        if recorder is None:raise ActionFailedError('手机端录制需要使用Relay App控制')
        owner = self.service.contents.owner(event)
        if action == 'start':
            if kwargs.get('mode') != 'video' and any(task.get('_audio') is not None for task in self.service.contents.active.values()):
                raise ActionFailedError('请先结束当前媒体理解的音频采集，再开始独立录音')
            result = await recorder.start(owner,mode=kwargs.get('mode') or 'both',seconds=float(kwargs.get('seconds',60)),source=self.service.config.media.audio_source)
        elif action == 'status':result = await recorder.status(owner)
        elif action == 'cancel':result = await recorder.cancel(owner)
        elif action == 'stop':
            result = await recorder.stop(owner)
            if kwargs.get('send_to_chat'):await send_file(event,self.service.files.path(result['file_id']))
        else:raise ActionFailedError('录制action需为start/status/stop/cancel')
        return json.dumps(result,ensure_ascii=False)
