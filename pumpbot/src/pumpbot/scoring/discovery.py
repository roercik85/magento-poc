"""Candidate channel discovery.

Finding channels is the easy part; Telegram's own search returns hundreds for
any pump-adjacent query. The hard part is that **a channel's reputation tells
you nothing useful**. Subscriber counts are bought, pinned "track records" are
screenshots, and the loudest channels are the most heavily astroturfed.

So discovery here is deliberately thin: it enumerates candidates and hands them
to :mod:`pumpbot.scoring.channels`, which judges them on observed outcomes
only. The workflow is:

1. ``discover`` — enumerate joined channels and, optionally, search results.
2. Run the engine in ``record`` mode for a week or more. No trading.
3. ``score`` — rank what was recorded.
4. Promote only channels that score well, into ``telegram.channels`` as
   ``tier: trusted``.

Step 2 is the one people skip. It is the only one that produces information.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

DEFAULT_QUERIES: Sequence[str] = (
    "pump", "crypto signals", "calls", "gem calls", "moonshot",
    "altcoin signals", "binance pump", "solana calls", "degen calls",
)


@dataclass(slots=True)
class CandidateChannel:
    chat_id: int
    title: str
    username: Optional[str]
    participants: Optional[int]
    source: str            # "joined" | "search:<query>"

    def as_config_entry(self) -> dict:
        return {"id": self.chat_id, "name": self.username or self.title, "tier": "candidate"}


async def discover(
    client: Any,
    queries: Sequence[str] = DEFAULT_QUERIES,
    *,
    include_joined: bool = True,
    include_search: bool = True,
    min_participants: int = 500,
) -> List[CandidateChannel]:
    """Enumerate candidate channels via an authenticated Telethon client.

    Requires an account that has already joined the channels you care about;
    Telegram's global search only surfaces public ones, and the channels worth
    watching are frequently invite-only.
    """
    from telethon.tl.functions.contacts import SearchRequest
    from telethon.tl.types import Channel

    seen: dict[int, CandidateChannel] = {}

    def _add(entity: Any, source: str) -> None:
        if not isinstance(entity, Channel):
            return
        count = getattr(entity, "participants_count", None)
        if count is not None and count < min_participants:
            return
        cid = int(f"-100{entity.id}")
        seen.setdefault(
            cid,
            CandidateChannel(
                chat_id=cid,
                title=entity.title or "",
                username=entity.username,
                participants=count,
                source=source,
            ),
        )

    if include_joined:
        async for dialog in client.iter_dialogs():
            _add(dialog.entity, "joined")

    if include_search:
        for q in queries:
            try:
                result = await client(SearchRequest(q=q, limit=50))
            except Exception:                    # noqa: BLE001 - flood waits, bad queries
                continue
            for chat in result.chats:
                _add(chat, f"search:{q}")

    return sorted(seen.values(), key=lambda c: -(c.participants or 0))
