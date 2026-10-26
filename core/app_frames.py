"""手机主动推送JPEG帧，缓存一帧并按采集时间和操作代次读取。"""
from __future__ import annotations
import asyncio
import contextvars
import io
import json
import struct
import time
from .errors import ActionFailedError, DeviceOfflineError
from .device_gate import current_scope
from .identity import PLUGIN_NAME


class AppFrames:
    def __init__(self, adb):
        self.adb = adb
        self.task = None
        self.latest = None
        self.changed = asyncio.Event()
        self.error = ''
        self.retry_at = 0.0

    def start(self):
        if self.task is not None and not self.task.done():return
        if time.monotonic() < self.retry_at:return
        self.latest = None;self.error = ''
        self.task = asyncio.create_task(self.receive(),name=f'{PLUGIN_NAME}_app_frames',context=contextvars.Context())

    async def receive(self):
        bridge = self.adb.bridge
        session = bridge.session
        try:
            async with bridge.streams.connect('frames:') as (reader,_writer):
                while bridge.session == session:
                    sync=getattr(self.adb,'refresh_video_clock',None)
                    if callable(sync) and time.monotonic()-self.adb._video_clock_synced_at>60:
                        await sync()
                    length = struct.unpack('!I',await asyncio.wait_for(reader.readexactly(4),20))[0]
                    if not 0 < length <= 4096:raise ValueError('视频帧头过长')
                    meta = json.loads(await reader.readexactly(length))
                    if (not isinstance(meta,dict) or type(meta.get('generation')) is not int or meta['generation'] < 0
                            or type(meta.get('captured_at_ms')) is not int or meta['captured_at_ms']<0
                            or type(meta.get('age_ms')) is not int or not 0 <= meta['age_ms'] <= 2000):
                        raise ValueError('视频帧时间或操作代次无效')
                    length = struct.unpack('!I',await reader.readexactly(4))[0]
                    if not 0 < length <= 8*1024*1024:raise ValueError('视频帧大小无效')
                    data = await asyncio.wait_for(reader.readexactly(length),15)
                    if not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):raise ValueError('视频帧不是JPEG')
                    self.latest = {'meta':meta,'data':data,'received':time.monotonic(),'session':session,'validated':False}
                    self.changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = '手机视频推流结束：'+str(exc)
            self.retry_at = time.monotonic()+3
            self.changed.set()

    @staticmethod
    def validate(data):
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            if image.format != 'JPEG' or image.width*image.height > 9000000:
                raise ActionFailedError('手机视频帧尺寸或格式无效')
            image.load()

    @staticmethod
    def restore_size(data, meta):
        """低带宽帧恢复原始屏幕像素尺寸，统一模型与触控坐标。"""
        from PIL import Image
        width, height = meta.get('screen_width'), meta.get('screen_height')
        if width is None and height is None:return data
        if type(width) is not int or type(height) is not int or not 0 < width <= 8192 or not 0 < height <= 8192 or width*height > 9000000:
            raise ActionFailedError('手机视频原始尺寸无效')
        with Image.open(io.BytesIO(data)) as image:
            if (image.width,image.height)==(width,height):return data
            # 手机采用偶数YUV尺寸；缩放舍入误差最多2像素。
            ratio=min(width/image.width,height/image.height)
            if image.width>width or image.height>height or abs(image.width*ratio-width)>2*ratio or abs(image.height*ratio-height)>2*ratio:
                raise ActionFailedError('手机视频缩放比例无效')
            out=io.BytesIO()
            image.resize((width,height),Image.Resampling.BILINEAR).save(out,format='JPEG',quality=90)
            return out.getvalue()

    def age(self, frame):
        offset=getattr(self.adb,'_video_clock_offset_ms',None)
        if offset is None:return float('inf')
        age=time.monotonic()*1000-(frame['meta']['captured_at_ms']+offset)
        if age<0:return float('inf')
        return max(age,frame['meta']['age_ms']+(time.monotonic()-frame['received'])*1000)

    async def capture(self, timeout):
        started = time.monotonic();deadline = started+timeout
        if getattr(self.adb.bridge,'writer',None) is None:raise DeviceOfflineError('Relay App 已断线')
        if timeout<=0:raise ActionFailedError('截图期限无效')
        scope=current_scope()
        if scope:
            scope.checkpoint('screen')
            deadline=min(deadline,scope.deadline)
        self.start()
        while True:
            if getattr(self.adb.bridge,'writer',None) is None:raise DeviceOfflineError('Relay App 已断线')
            if scope:scope.checkpoint('screen')
            self.changed.clear()
            if self.error:raise ActionFailedError(self.error)
            frame = self.latest
            if frame and frame['session'] == self.adb.bridge.session:
                age = self.age(frame)
                limit=getattr(self.adb,'_frame_max_age_ms',250)
                if age <= limit and frame['meta']['generation'] >= self.adb._video_generation:
                    if not frame['validated']:
                        await asyncio.to_thread(self.validate,frame['data'])
                        frame['data']=await asyncio.to_thread(self.restore_size,frame['data'],frame['meta'])
                        frame['validated']=True
                    # 解码校验期间可能又完成了设备操作，再核对一次代次与时间。
                    age = self.age(frame)
                    if age <= limit and frame['meta']['generation'] >= self.adb._video_generation:
                        if getattr(self.adb.bridge,'writer',None) is None:raise DeviceOfflineError('Relay App 已断线')
                        if scope:scope.checkpoint('screen')
                        self.adb.last_screen_meta={'frame_age_ms':round(age,1),'generation':frame['meta']['generation'], 'frame_age_limit_ms':limit, 'clock_rtt_ms':getattr(self.adb,'_video_clock_rtt_ms',None)}
                        return frame['data'],'jpeg',round((time.monotonic()-started)*1000,1)
            remaining = deadline-time.monotonic()
            if remaining <= 0:raise ActionFailedError('等待当前操作后的手机视频帧超时')
            try:await asyncio.wait_for(self.changed.wait(),min(.1,remaining))
            except asyncio.TimeoutError:continue

    async def close(self):
        task = self.task;self.task=None
        if task is not None:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.latest=None;self.error='';self.retry_at=0
