"""手机文件与聊天附件的有界传输目录。"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex

from .app_files import MAX_FILE_BYTES, phone_path
from .errors import ActionFailedError

FILE_ID = re.compile(r"[0-9a-f]{32}(?:\.[A-Za-z0-9]{1,8})?\Z")


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


class TransferFiles:
    def __init__(self, adb, directory):
        self.adb, self.root = adb, Path(directory)

    def allocate(self, suffix=''):
        self.root.mkdir(parents=True, exist_ok=True); self.root.chmod(0o700)
        total = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
        if total + MAX_FILE_BYTES > 512 * 1024 * 1024:
            raise ActionFailedError('传输目录空间额度不足，请清理已有文件后重试')
        extension = suffix if re.fullmatch(r'\.[A-Za-z0-9]{1,8}', suffix) else '.bin'
        return self.root / (secrets.token_hex(16) + extension)

    def path(self, file_id):
        if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
            raise ActionFailedError('传输文件ID无效')
        path = self.root / file_id
        if path.is_symlink() or path.resolve().parent != self.root.resolve() or not path.is_file():
            raise ActionFailedError('传输文件不存在')
        return path

    async def describe(self, path):
        return {'file_id':path.name, 'size':path.stat().st_size, 'sha256':await asyncio.to_thread(file_hash,path)}

    def recent(self):
        if not self.root.is_dir():return []
        paths = [p for p in self.root.iterdir() if FILE_ID.fullmatch(p.name) and p.is_file() and not p.is_symlink()]
        return [{'file_id':p.name,'size':p.stat().st_size} for p in sorted(paths,key=lambda p:p.stat().st_mtime,reverse=True)[:20]]

    async def attachment(self, event, index=0):
        from astrbot.api.message_components import File
        parts = [part for part in event.get_messages() if isinstance(part, File)]
        if type(index) is not int or not 0 <= index < len(parts):
            raise ActionFailedError('当前消息没有对应的文件附件')
        value = parts[index].get_file()
        source = Path(await value if inspect.isawaitable(value) else value)
        if not source.is_file() or source.stat().st_size > MAX_FILE_BYTES:
            raise ActionFailedError('附件不存在或超过128 MiB')
        target = self.allocate(Path(parts[index].name).suffix)
        def copy():
            size = 0
            try:
                with source.open('rb') as reader, target.open('xb') as writer:
                    target.chmod(0o600)
                    for chunk in iter(lambda:reader.read(65536),b''):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:raise ActionFailedError('附件超过128 MiB')
                        writer.write(chunk)
            except BaseException:
                target.unlink(missing_ok=True);raise
        await asyncio.to_thread(copy)
        return target.name

    async def download(self, remote):
        remote = phone_path(remote)
        value = (await self.adb.shell('test -f '+shlex.quote(remote)+' && stat -c %s '+shlex.quote(remote),timeout=10)).strip()
        if not value.isdigit() or int(value) > MAX_FILE_BYTES:
            raise ActionFailedError('手机文件不存在或超过128 MiB')
        target = self.allocate(PurePosixPath(remote).suffix)
        partial = target.with_suffix(target.suffix+'.part')
        try:
            await self.adb.run(['pull',remote,str(partial)],timeout=180)
            if partial.stat().st_size != int(value):raise ActionFailedError('手机文件大小在传输期间发生变化')
            remote_hash = (await self.adb.shell('sha256sum '+shlex.quote(remote),timeout=30)).split()[0]
            if await asyncio.to_thread(file_hash,partial) != remote_hash:raise ActionFailedError('手机下载文件SHA256校验失败')
            partial.chmod(0o600);partial.replace(target)
            return await self.describe(target)
        finally:partial.unlink(missing_ok=True)

    async def upload(self, file_id, remote):
        source = self.path(file_id);remote = phone_path(remote)
        if source.stat().st_size > MAX_FILE_BYTES:raise ActionFailedError('上传文件超过128 MiB')
        await self.adb.run(['push',str(source),remote],timeout=180)
        remote_hash = (await self.adb.shell('sha256sum '+shlex.quote(remote),timeout=30)).split()[0]
        result = await self.describe(source)
        if result['sha256'] != remote_hash:raise ActionFailedError('手机上传文件SHA256校验失败')
        return {**result,'phone_path':remote}
