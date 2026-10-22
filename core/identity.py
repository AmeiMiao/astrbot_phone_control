"""从插件元数据解析配置、数据和任务的命名空间。"""

from __future__ import annotations

import re
from pathlib import Path


def plugin_name(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("name:"):
            value = line.partition(":")[2].strip().strip("\"'")
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                return value
            break
    raise ValueError("metadata.yaml 的 name 必须是合法 Python 标识符")


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_NAME = plugin_name(PLUGIN_ROOT / "metadata.yaml")
