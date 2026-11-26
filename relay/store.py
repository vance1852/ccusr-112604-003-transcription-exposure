"""追加式日志：每个被受理的决定落盘一条，重启后按序重放恢复状态。"""

from __future__ import annotations

import json
import os
from pathlib import Path


class Journal:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sink = self.path.open("a", encoding="utf-8")

    def append(self, entry: dict) -> None:
        self._sink.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        self._sink.flush()
        os.fsync(self._sink.fileno())

    def close(self) -> None:
        self._sink.close()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_entries(path: Path | str):
    path = Path(path)
    if not path.exists():
        return
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError as error:
                raise ValueError(f"{path.name}:{number} 日志行无法解析") from error
            if not isinstance(entry, dict) or "op" not in entry:
                raise ValueError(f"{path.name}:{number} 日志行应为带 op 的对象")
            yield entry
