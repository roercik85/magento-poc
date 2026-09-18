"""Append-only JSONL event log.

Two rules:

* **Never write from the hot path.** Records go onto an unbounded in-memory
  deque and a background task drains it. A blocking ``write()`` inside the
  Telegram update handler would be the single slowest thing in the system.
* **Write everything.** Rejected signals, unparsed messages, latency traces.
  Disk is cheap; an unexplainable run is not.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional

try:                                             # pragma: no cover - optional dep
    import orjson

    def _dump(obj: Dict[str, Any]) -> bytes:
        return orjson.dumps(obj, option=orjson.OPT_APPEND_NEWLINE)
except ImportError:                              # pragma: no cover
    def _dump(obj: Dict[str, Any]) -> bytes:
        return (json.dumps(obj, default=str, separators=(",", ":")) + "\n").encode()


class Logbook:
    def __init__(self, path: str | Path, flush_every: int = 64) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: Deque[Dict[str, Any]] = deque()
        self._flush_every = flush_every
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    def record(self, kind: str, **fields: Any) -> None:
        """Hot-path safe: one dict allocation and an append."""
        fields["kind"] = kind
        self._queue.append(fields)

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._drain_loop(), name="logbook-drain")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            await self._task
            self._task = None
        self._flush()

    async def _drain_loop(self) -> None:
        while not self._stopping:
            if len(self._queue) >= self._flush_every:
                self._flush()
            await asyncio.sleep(0.25)
        self._flush()

    def _flush(self) -> None:
        if not self._queue:
            return
        with self.path.open("ab") as fh:
            while self._queue:
                fh.write(_dump(self._queue.popleft()))

    def read_all(self) -> list[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
