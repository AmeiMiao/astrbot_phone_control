"""下载并校验固定依赖，将类与本地库展开到构建目录。"""

import hashlib
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parent
output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
for item in json.loads((root / "dependencies.json").read_text()):
    path = output / item["file"]
    if not path.exists():
        with urllib.request.urlopen(item["url"], timeout=120) as response:
            data = response.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024:
            raise ValueError("依赖文件过大")
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("依赖摘要不匹配：" + item["file"])
        path.write_bytes(data)
    if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
        raise ValueError("缓存依赖摘要不匹配：" + item["file"])
    if path.suffix == ".aar":
        with zipfile.ZipFile(path) as archive:
            path.with_suffix(".jar").write_bytes(archive.read("classes.jar"))
            for name in archive.namelist():
                if name.startswith("jni/") and name.endswith(".so"):
                    relative = Path(name)
                    if len(relative.parts) != 3 or ".." in relative.parts:
                        raise ValueError("本地库路径无效")
                    target = output / "native/lib" / Path(*relative.parts[1:])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(name))
