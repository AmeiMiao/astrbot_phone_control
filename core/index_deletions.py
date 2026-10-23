"""持久化索引删除记录，使旧快照和并发重建遵守同一删除结果。"""

import json
import os
import threading
from pathlib import Path

from .errors import PhoneControlError

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class IndexDeletions:
    def __init__(self, root):
        self.path = Path(root) / "deletions.json"
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(str(self.path.resolve()), threading.RLock())

    def read(self):
        with self.lock:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {"documents": [], "sources": []}
            except (OSError, ValueError) as exc:
                raise PhoneControlError("索引删除记录读取失败，请检查存储") from exc
            if (
                not isinstance(value, dict)
                or value.get("version") != 1
                or not isinstance(value.get("documents"), list)
                or not all(isinstance(v, str) for v in value["documents"])
                or not isinstance(value.get("sources"), list)
                or not all(
                    isinstance(v, list)
                    and len(v) == 2
                    and all(isinstance(s, str) for s in v)
                    for v in value["sources"]
                )
            ):
                raise PhoneControlError("索引删除记录格式错误")
            return value

    def _write(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"version": 1, **value}, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, self.path)

    def predicate(self):
        value = self.read()
        documents = set(value["documents"])
        sources = {tuple(v) for v in value["sources"]}
        return lambda doc: doc.id in documents or (doc.source, doc.source_id) in sources

    def commit_source_delete(self, source, source_id, writer):
        """先屏蔽旧快照，再原子删除源记录；源写入失败时恢复删除标记。"""
        with self.lock:
            before = self.read()
            pair = [source, source_id]
            after = {
                "documents": list(before["documents"]),
                "sources": list(before["sources"]),
            }
            if pair not in after["sources"]:
                after["sources"].append(pair)
            self._write(after)
            try:
                return writer()
            except BaseException:
                self._write(before)
                raise
