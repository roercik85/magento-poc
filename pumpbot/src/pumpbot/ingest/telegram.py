"""Telegram ingestion over MTProto.

Why MTProto and not the Bot API: a bot cannot read a channel it has not been
added to as an admin, and the Bot API adds a polling or webhook hop on top of
Telegram's own fanout. A user client receives the update push directly. That is
the difference between competing and watching.

**The raw-update path.** Telethon's friendly ``events.NewMessage`` resolves the
sender and chat into full entities before it calls your handler. Resolution can
issue RPCs, and an RPC on the critical path costs an entire round trip to a
Telegram data centre — routinely 40-200 ms, which is more than the rest of this
system spends combined. So we register a ``events.Raw`` handler and read the
integer ``channel_id`` and the message text straight off the wire. Names are
resolved lazily, off the hot path, purely for the report.

**Multiple sessions.** Telegram fans out to a large channel over some seconds,
and different accounts sit behind different data centres. Running two or three
sessions on separate accounts and keeping whichever copy arrives first is the
cheapest latency improvement available — usually far larger than anything you
can win by optimising Python.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from ..clock import now_ns, wall_ms
from ..config import TelegramConfig
from ..models import RawMessage

MessageHandler = Callable[[RawMessage], Awaitable[None] | None]


def prepare_session_path(session_name: str) -> str:
    """Make sure the session file's directory exists.

    Telethon stores the session in SQLite and opens it in the TelegramClient
    constructor, so a missing directory fails with "unable to open database
    file" before any Telegram code runs — which reads like a Telegram problem
    and is not one. The default lives under state/, which is gitignored and
    therefore absent from every fresh clone.
    """
    from pathlib import Path

    path = Path(session_name)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    return session_name


class TelegramIngest:
    """Fans raw channel messages into a single handler, deduplicated."""

    def __init__(
        self,
        cfg: TelegramConfig,
        handler: MessageHandler,
        *,
        dedupe_cap: int = 20_000,
    ) -> None:
        self._cfg = cfg
        self._handler = handler
        self._clients: List[Any] = []
        self._session_names: List[str] = []
        self._seen: Set[str] = set()
        self._seen_order: List[str] = []
        self._dedupe_cap = dedupe_cap
        self._allowed: Optional[Set[int]] = (
            {c.id for c in cfg.channels} if cfg.channels else None
        )
        self._blocked: Set[int] = {c.id for c in cfg.channels if c.tier == "blocked"}
        self._names: Dict[int, str] = {c.id: (c.name or str(c.id)) for c in cfg.channels}
        self.duplicates_dropped: int = 0
        self.messages_seen: int = 0

    # -- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        from telethon import TelegramClient, events

        specs = [(self._cfg.session_name, self._cfg.api_id, self._cfg.api_hash)]
        for extra in self._cfg.extra_sessions:
            specs.append(
                (
                    extra.session_name,
                    extra.api_id or self._cfg.api_id,
                    extra.api_hash or self._cfg.api_hash,
                )
            )

        for session_name, api_id, api_hash in specs:
            if not api_id or not api_hash:
                raise RuntimeError(
                    f"telegram session {session_name!r} is missing api_id/api_hash"
                )
            client = TelegramClient(
                prepare_session_path(session_name), api_id, api_hash
            )
            client.add_event_handler(
                self._make_raw_handler(session_name), events.Raw
            )
            await client.start()
            self._clients.append(client)
            self._session_names.append(session_name)

    async def run_until_disconnected(self) -> None:
        await asyncio.gather(*(c.run_until_disconnected() for c in self._clients))

    async def stop(self) -> None:
        for client in self._clients:
            await client.disconnect()
        self._clients.clear()

    # -- hot path -------------------------------------------------------
    def _make_raw_handler(self, session_name: str) -> Callable[[Any], Awaitable[None]]:
        from telethon.tl.types import (
            UpdateEditChannelMessage,
            UpdateNewChannelMessage,
            UpdateNewMessage,
        )

        new_types = (UpdateNewChannelMessage, UpdateNewMessage)
        edit_types = (UpdateEditChannelMessage,)
        watch_edits = self._cfg.watch_edits

        async def handler(update: Any) -> None:
            # Timestamp first: everything after this is our own overhead and we
            # want it measured, not hidden.
            recv_ns = now_ns()
            recv_wall = wall_ms()

            is_edit = isinstance(update, edit_types)
            if not (isinstance(update, new_types) or (watch_edits and is_edit)):
                return

            msg = getattr(update, "message", None)
            text = getattr(msg, "message", None)
            if not text:
                return

            chat_id = _peer_channel_id(msg)
            if chat_id is None:
                return
            if chat_id in self._blocked:
                return
            if self._allowed is not None and chat_id not in self._allowed:
                return

            self.messages_seen += 1

            raw = RawMessage(
                chat_id=chat_id,
                message_id=int(getattr(msg, "id", 0)),
                text=text,
                received_ns=recv_ns,
                received_wall_ms=recv_wall,
                channel_name=self._names.get(chat_id, str(chat_id)),
                posted_wall_ms=_date_ms(msg),
                is_edit=is_edit,
                source_session=session_name,
            )

            # Dedupe across sessions. An edit is a distinct event from the
            # original post, so it gets its own key.
            key = raw.dedupe_key + ("#e" if is_edit else "")
            if key in self._seen:
                self.duplicates_dropped += 1
                return
            self._remember(key)

            result = self._handler(raw)
            if result is not None:
                await result

        return handler

    def _remember(self, key: str) -> None:
        self._seen.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > self._dedupe_cap:
            # Drop the oldest quarter in one pass rather than one per message;
            # amortised, this keeps the common case to a set insert.
            drop, self._seen_order = (
                self._seen_order[: self._dedupe_cap // 4],
                self._seen_order[self._dedupe_cap // 4:],
            )
            self._seen.difference_update(drop)

    # -- off the hot path -----------------------------------------------
    async def resolve_names(self) -> Dict[int, str]:
        """Fill in human-readable channel names for the report."""
        if not self._clients:
            return self._names
        client = self._clients[0]
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            ent_id = getattr(entity, "id", None)
            if ent_id is None:
                continue
            cid = int(f"-100{ent_id}")
            self._names.setdefault(cid, getattr(entity, "title", "") or str(cid))
        return self._names


def _peer_channel_id(msg: Any) -> Optional[int]:
    """Extract the -100-prefixed chat id without resolving an entity."""
    peer = getattr(msg, "peer_id", None)
    if peer is None:
        return None
    channel_id = getattr(peer, "channel_id", None)
    if channel_id is not None:
        return int(f"-100{channel_id}")
    chat_id = getattr(peer, "chat_id", None)
    if chat_id is not None:
        return -int(chat_id)
    user_id = getattr(peer, "user_id", None)
    return int(user_id) if user_id is not None else None


def _date_ms(msg: Any) -> Optional[float]:
    date = getattr(msg, "date", None)
    if date is None:
        return None
    try:
        return date.timestamp() * 1000.0
    except Exception:                            # noqa: BLE001
        return None
