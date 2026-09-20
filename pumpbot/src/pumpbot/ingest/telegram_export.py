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
from dataclasses import dataclass
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


def find_export_files(path: str | Path) -> List[Path]:
    """Locate every ``result.json`` under ``path``.

    Three shapes have to work, because which one you get depends on which
    Telegram app you happen to have:

    * ``result.json`` itself.
    * One export folder containing it — Telegram Desktop's whole-account
      export.
    * A folder of export folders. The macOS App Store app has no global
      export, only per-chat, so exporting twenty channels leaves twenty
      folders. Merging those by hand is busywork and easy to get wrong.
    """
    root = Path(path)
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise ExportError(f"export not found: {root}")

    direct = root / "result.json"
    if direct.exists():
        return [direct]

    # Bounded depth: deep enough for a folder of export folders, shallow
    # enough not to crawl an entire home directory by accident.
    found = sorted({*root.glob("*/result.json"), *root.glob("*/*/result.json")})
    if not found:
        raise ExportError(
            f"{root} contains no result.json, and neither do the folders "
            f"inside it. Point at the export folder Telegram created, at the "
            f"folder holding several of them, or at result.json itself."
        )
    return found


def _chats_in(file: Path) -> Iterator[Dict[str, Any]]:
    try:
        with file.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ExportError(
            f"{file} is not valid JSON: {exc}. Telegram exports HTML by "
            f"default — re-export choosing format 'Machine-readable JSON'."
        ) from exc

    chats = data.get("chats")
    if isinstance(chats, dict) and isinstance(chats.get("list"), list):
        yield from chats["list"]                 # whole-account export
    elif isinstance(data.get("messages"), list):
        yield data                               # single-chat export
    else:
        raise ExportError(
            f"{file} does not look like a Telegram export. Expected a 'chats' "
            f"list or a 'messages' array. Re-export choosing format "
            f"'Machine-readable JSON'."
        )


def iter_chats(path: str | Path) -> Iterator[Dict[str, Any]]:
    """Yield chat objects from one export, or from a folder of exports."""
    for file in find_export_files(path):
        yield from _chats_in(file)


@dataclass
class ExportStats:
    """What an export actually contained, for diagnosing an empty one."""

    files: int = 0
    chats_seen: int = 0
    channels_seen: int = 0
    channels_with_text: int = 0
    messages_in_window: int = 0
    messages_outside_window: int = 0

    @property
    def looks_like_only_my_messages(self) -> bool:
        """Telegram's bulk export restriction, as it appears in the data.

        The account-wide export offers public channels as "only my messages",
        and it cannot be unchecked — the content belongs to the channel owner.
        In a broadcast channel you have posted nothing, so the chats arrive
        present but completely empty.

        The absence of text *inside the window* is not enough to conclude
        this: an export that is simply too old looks identical there, and
        sending someone to redo twenty exports over a date range they could
        have widened is the worse error. So this requires that no message
        carried text at any date.
        """
        return (
            self.channels_seen > 0
            and self.channels_with_text == 0
            and self.messages_outside_window == 0
        )


def explain_empty(stats: "ExportStats", days: int) -> str:
    """Say why an export yielded nothing, in terms of what to do about it."""
    if stats.files == 0:
        return "No result.json was found at that path."
    if stats.chats_seen == 0:
        return (
            "The export contains no chats at all. Re-export with the channels "
            "selected."
        )
    if stats.looks_like_only_my_messages:
        return (
            f"The export lists {stats.channels_seen} channel(s) but not one "
            f"message of text.\n\n"
            f"  This is Telegram's bulk-export restriction, not a problem with "
            f"the file. In\n"
            f"  Settings -> Advanced -> Export, public channels are fixed at "
            f"\"only my messages\"\n"
            f"  and cannot be unchecked, so a broadcast channel you only read "
            f"exports empty.\n\n"
            f"  Export each channel on its own instead: right-click it in the "
            f"chat list ->\n"
            f"  Export chat history -> format JSON, media off. That path has no "
            f"such limit.\n"
            f"  Put the resulting folders in one directory and point "
            f"--from-export at it."
        )
    if stats.messages_in_window == 0 and stats.messages_outside_window > 0:
        return (
            f"All {stats.messages_outside_window} message(s) fall outside the "
            f"last {days} days. Raise --days, or export a wider date range."
        )
    return (
        f"No channel has messages inside {days} days. Check that the export "
        f"included the channels and their message text."
    )


def read_export(
    path: str | Path,
    *,
    days: int = 30,
    channels_only: bool = True,
    stats: Optional["ExportStats"] = None,
) -> List[ChannelHistory]:
    """Build per-channel histories from an export, newest ``days`` only.

    Pass ``stats`` to have the counts needed to explain an empty result filled
    in; an export that parses fine and yields nothing is the common case, and
    the reason is never visible from the empty list alone.
    """
    cutoff_ms = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).timestamp() * 1000.0

    st = stats if stats is not None else ExportStats()
    st.files = len(find_export_files(path))

    by_id: Dict[int, ChannelHistory] = {}
    spans: Dict[int, List[float]] = {}
    for chat in iter_chats(path):
        st.chats_seen += 1
        if channels_only and chat.get("type") not in (
            "public_channel", "private_channel", "channel"
        ):
            continue
        st.channels_seen += 1

        chat_id = _chat_id(chat.get("id"))
        if chat_id is None:
            continue

        # A channel can appear in more than one file — per-chat exports taken
        # on different days overlap. Merge rather than emit it twice, or the
        # scorer sees one channel as several and none reaches a sample.
        history = by_id.get(chat_id)
        if history is None:
            history = ChannelHistory(
                chat_id=chat_id, name=chat.get("name") or str(chat_id)
            )
            by_id[chat_id] = history
            spans[chat_id] = []
        seen_ids = {m.message_id for m in history.messages}

        for message in chat.get("messages", []):
            if message.get("type") != "message":
                continue                         # service entries carry no call

            posted_ms = _timestamp_ms(message)
            if posted_ms is None:
                continue
            if posted_ms < cutoff_ms:
                # Counted only when it carries text, so it distinguishes "too
                # old" from "exported without content".
                if flatten_text(message.get("text")):
                    st.messages_outside_window += 1
                continue

            message_id = int(message.get("id") or 0)
            if message_id in seen_ids:
                continue                         # already read from another file

            spans[chat_id].append(posted_ms)

            if message.get("forwarded_from") is not None:
                history.forwards += 1
            if message.get("edited") or message.get("edited_unixtime"):
                history.edits += 1

            text = flatten_text(message.get("text"))
            if not text:
                continue
            st.messages_in_window += 1

            seen_ids.add(message_id)
            history.messages.append(RawMessage(
                chat_id=chat_id,
                message_id=message_id,
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

    out: List[ChannelHistory] = []
    for chat_id, history in by_id.items():
        stamps = spans.get(chat_id) or []
        if stamps:
            history.span_days = max((max(stamps) - min(stamps)) / 86_400_000.0, 0.0)
        if history.messages:
            history.messages.sort(key=lambda m: m.posted_wall_ms or 0.0)
            st.channels_with_text += 1
            out.append(history)

    return out
