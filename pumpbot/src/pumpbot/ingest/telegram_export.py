"""Reading Telegram Desktop's JSON export.

Telegram's own API is the fast path, but it is not always an available one:
login codes for freshly created applications are sometimes accepted by the
server and then silently never delivered, which leaves nothing to debug and no
way through.

Telegram Desktop can export chat history directly — Settings → Advanced →
Export Telegram data, with "Machine-readable JSON" and the channels selected.
That produces exactly what triage needs (message text, timestamps, forward
markers) with no API credentials at all, so the whole retrospective pass works
while the login problem is someone else's to solve.

What it cannot give you is live data, so recording still needs a working API
login eventually. This unblocks the part that decides whether any of it is
worth doing.

Format notes, all of which bite:

* ``text`` is either a plain string or a list mixing strings and entity
  objects (links, bold runs, mentions). A call posted with the ticker in bold
  arrives as a list, and treating it as a string silently drops the ticker —
  the one part that matters.
* ``id`` is the bare channel id. The rest of the system uses the -100-prefixed
  form, so they have to be reconciled or every channel looks like a new one.
* Service entries (joins, pins, photo changes) share the array with real
  messages and have no text.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ..clock import now_ns
from ..models import RawMessage
from .history import ChannelHistory


class ExportError(ValueError):
    pass


def flatten_text(value: Any) -> str:
    """Collapse Telegram's mixed text representation into a plain string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return str(value)


def _chat_id(raw_id: Any) -> Optional[int]:
    """Normalise an export id to the -100-prefixed form used everywhere else."""
    try:
        value = int(raw_id)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return value
    return int(f"-100{value}")


def _timestamp_ms(message: Dict[str, Any]) -> Optional[float]:
    unix = message.get("date_unixtime")
    if unix is not None:
        try:
            return float(unix) * 1000.0
        except (TypeError, ValueError):
            pass
    date = message.get("date")
    if not date:
        return None
    try:
        # Exports write naive local time; treat it as UTC rather than guessing
        # a zone. An hour of skew is harmless for cadence and structure, and
        # the alternative is inventing a timezone the file does not state.
        return datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp() * 1000.0
    except ValueError:
        return None


def iter_chats(path: str | Path) -> Iterator[Dict[str, Any]]:
    """Yield chat objects from either a full export or a single-chat export."""
    file = Path(path)
    if file.is_dir():
        candidate = file / "result.json"
        if not candidate.exists():
            raise ExportError(
                f"{file} contains no result.json. Point at the export folder "
                f"Telegram Desktop created, or at result.json itself."
            )
        file = candidate

    if not file.exists():
        raise ExportError(f"export not found: {file}")

    try:
        with file.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ExportError(f"{file} is not valid JSON: {exc}") from exc

    chats = data.get("chats")
    if isinstance(chats, dict) and isinstance(chats.get("list"), list):
        yield from chats["list"]                 # full export
    elif isinstance(data.get("messages"), list):
        yield data                               # single-chat export
    else:
        raise ExportError(
            f"{file} does not look like a Telegram export. Expected a 'chats' "
            f"list or a 'messages' array. In Telegram Desktop choose "
            f"Settings -> Advanced -> Export Telegram data, format "
            f"'Machine-readable JSON'."
        )


def read_export(
    path: str | Path,
    *,
    days: int = 30,
    channels_only: bool = True,
) -> List[ChannelHistory]:
    """Build per-channel histories from an export, newest ``days`` only."""
    cutoff_ms = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).timestamp() * 1000.0

    out: List[ChannelHistory] = []
    for chat in iter_chats(path):
        if channels_only and chat.get("type") not in (
            "public_channel", "private_channel", "channel"
        ):
            continue

        chat_id = _chat_id(chat.get("id"))
        if chat_id is None:
            continue

        history = ChannelHistory(
            chat_id=chat_id, name=chat.get("name") or str(chat_id)
        )
        oldest: Optional[float] = None
        newest: Optional[float] = None

        for message in chat.get("messages", []):
            if message.get("type") != "message":
                continue                         # service entries carry no call

            posted_ms = _timestamp_ms(message)
            if posted_ms is None or posted_ms < cutoff_ms:
                continue

            oldest = posted_ms if oldest is None else min(oldest, posted_ms)
            newest = posted_ms if newest is None else max(newest, posted_ms)

            if message.get("forwarded_from") is not None:
                history.forwards += 1
            if message.get("edited") or message.get("edited_unixtime"):
                history.edits += 1

            text = flatten_text(message.get("text"))
            if not text:
                continue

            history.messages.append(RawMessage(
                chat_id=chat_id,
                message_id=int(message.get("id") or 0),
                text=text,
                received_ns=now_ns(),
                # An export has no arrival time, only the post time. Using it
                # for both is honest; inventing a delivery delay would fabricate
                # latency that was never measured.
                received_wall_ms=posted_ms,
                channel_name=history.name,
                posted_wall_ms=posted_ms,
                is_edit=False,
                source_session="export",
            ))

        if oldest is not None and newest is not None:
            history.span_days = max((newest - oldest) / 86_400_000.0, 0.0)
        if history.messages:
            out.append(history)

    return out
