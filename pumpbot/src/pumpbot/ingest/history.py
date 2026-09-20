"""Reading channel history, and screening channels on structure alone.

Two ideas here, and the second is the useful one.

**History reading.** Telethon can page backwards through a channel's past
messages. Combined with one-minute candles — which KuCoin serves at least a
year back — this makes a *retrospective* scoring pass possible: a channel's
last month of calls can be judged this afternoon instead of after a month of
live recording. The resolution is coarser than a live tick capture, so it does
not replace one. It decides which channels are worth capturing.

**Structural screening.** Some channels can be rejected before looking at a
single price, because their structure gives away the business model:

* **It sells access.** A channel whose posts pitch a VIP tier is monetising
  subscriptions, not trading. Its incentive is a compelling-looking record, and
  the cheapest way to produce one is to post many calls and talk about the
  winners.
* **It forwards.** A high forward ratio means the calls originate elsewhere and
  arrive here late, which is the whole relay problem, visible without any
  market data at all.
* **It pre-announces.** "Next call in 10 minutes" hands insiders — including
  the people running the channel — a window to accumulate before the audience
  is allowed to know what the ticker is. That is the distribution pattern
  stated out loud.
* **It fires constantly.** Twenty calls a day is not twenty opportunities; it
  is a channel maximising the chance that *something* it named went up, so it
  has a screenshot for next week's marketing.

None of this proves a channel is unprofitable. It tells you where not to spend
the weeks of recording, which is the scarce resource.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from ..clock import now_ns
from ..models import RawMessage
from ..parsing.extractor import SignalExtractor

# Pitches for paid access. Deliberately narrow: "VIP" alone appears in plenty
# of innocent messages, so the patterns require it near a transactional verb.
_RE_PAID = re.compile(
    r"\b(?:"
    r"VIP\s*(?:group|channel|signals?|access|membership|tier)"
    r"|(?:join|upgrade|subscribe)\s+(?:to\s+)?(?:our\s+)?VIP"
    r"|(?:paid|premium|private)\s+(?:group|channel|signals?|membership)"
    r"|DM\s+(?:me|us)\s+(?:for|to)\s+(?:join|access|details|price)"
    r"|\$\s*\d+\s*/\s*(?:month|mo|week|year)"
    r"|(?:spots?|seats?)\s+(?:left|available|remaining)"
    r")\b",
    re.IGNORECASE,
)

# The accumulation window, announced.
_RE_PREANNOUNCE = re.compile(
    r"\b(?:next|new)\s+(?:call|pump|signal|gem)\b.{0,40}?"
    r"\b(?:in|at)\b.{0,20}?\b(?:\d+\s*(?:min|minute|sec|second|hour|hr)|\d{1,2}[:.]\d{2})"
    r"|\bget\s+your\s+(?:USDT|funds?|bags?)\s+ready\b"
    r"|\bprepare\s+your\s+(?:USDT|funds?)\b",
    re.IGNORECASE,
)


@dataclass
class ChannelHistory:
    chat_id: int
    name: str
    messages: List[RawMessage] = field(default_factory=list)
    forwards: int = 0
    edits: int = 0
    span_days: float = 0.0


@dataclass
class StructuralScreen:
    """What a channel's shape says, before any price is looked at."""

    chat_id: int
    name: str
    messages: int
    span_days: float
    calls: int
    tradable_calls: int
    calls_per_day: float
    forward_ratio: float
    paid_pitch_ratio: float
    preannounce_ratio: float
    edit_ratio: float
    repeat_symbol_ratio: float
    days_to_scoreable: Optional[float]
    flags: List[str] = field(default_factory=list)
    verdict: str = ""

    @property
    def rejected(self) -> bool:
        return self.verdict.startswith("REJECT")


async def read_history(
    client: Any,
    chat_id: int,
    *,
    name: str = "",
    days: int = 30,
    limit: int = 3_000,
) -> ChannelHistory:
    """Page backwards through a channel's messages.

    ``limit`` is a hard stop: some channels post thousands a week and pulling
    all of it costs flood waits that delay every other channel in the pass.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    history = ChannelHistory(chat_id=chat_id, name=name or str(chat_id))

    oldest: Optional[datetime] = None
    newest: Optional[datetime] = None

    async for msg in client.iter_messages(chat_id, limit=limit):
        date = getattr(msg, "date", None)
        if date is None:
            continue
        if date < since:
            break

        oldest = date if oldest is None else min(oldest, date)
        newest = date if newest is None else max(newest, date)

        if getattr(msg, "fwd_from", None) is not None:
            history.forwards += 1
        if getattr(msg, "edit_date", None) is not None:
            history.edits += 1

        text = getattr(msg, "message", None)
        if not text:
            continue

        posted_ms = date.timestamp() * 1000.0
        history.messages.append(RawMessage(
            chat_id=chat_id,
            message_id=int(getattr(msg, "id", 0)),
            text=text,
            received_ns=now_ns(),
            # For history there is no separate arrival time; the post time is
            # all we have, and pretending otherwise would fabricate latency.
            received_wall_ms=posted_ms,
            channel_name=history.name,
            posted_wall_ms=posted_ms,
            is_edit=False,
            source_session="history",
        ))

    if oldest and newest:
        history.span_days = max((newest - oldest).total_seconds() / 86400.0, 0.0)
    return history


# Below this, cadence is meaningless: one message over a few minutes divides
# out to millions of calls a day and trips the firehose rule, which is how a
# channel with a single post got rejected for posting too much.
_MIN_SPAN_DAYS_FOR_CADENCE = 0.5
_MIN_MESSAGES_FOR_CADENCE = 5


def screen(
    history: ChannelHistory,
    extractor: SignalExtractor,
    *,
    min_signals_for_score: int = 12,
    is_tradable=None,                            # noqa: ANN001 - Callable[[str], bool]
) -> StructuralScreen:
    """Judge a channel on structure alone.

    ``is_tradable`` filters calls down to symbols the venue actually lists.
    Without it a channel's prose false positives count as calls, which
    inflates its cadence and can reject it for volume it never had — or, worse,
    let it through on a call count that is entirely noise.
    """
    total = len(history.messages)
    if total == 0:
        return StructuralScreen(
            chat_id=history.chat_id, name=history.name, messages=0,
            span_days=history.span_days, calls=0, tradable_calls=0,
            calls_per_day=0.0,
            forward_ratio=0.0, paid_pitch_ratio=0.0, preannounce_ratio=0.0,
            edit_ratio=0.0, repeat_symbol_ratio=0.0, days_to_scoreable=None,
            flags=["no messages in window"],
            verdict="REJECT — nothing posted in the window.",
        )

    paid = sum(1 for m in history.messages if _RE_PAID.search(m.text))
    pre = sum(1 for m in history.messages if _RE_PREANNOUNCE.search(m.text))

    parsed: List[str] = []
    symbols: List[str] = []
    for m in history.messages:
        sig = extractor.extract(m)
        if sig is None or not sig.symbol:
            continue
        parsed.append(sig.symbol)
        if is_tradable is None or is_tradable(sig.symbol):
            symbols.append(sig.symbol)

    calls = len(symbols)

    # Cadence needs enough span and enough messages to mean anything.
    if (
        history.span_days >= _MIN_SPAN_DAYS_FOR_CADENCE
        and total >= _MIN_MESSAGES_FOR_CADENCE
    ):
        calls_per_day = calls / history.span_days
    else:
        calls_per_day = 0.0

    counts = Counter(symbols)
    repeated = sum(n for n in counts.values() if n > 1)
    repeat_ratio = (repeated / calls) if calls else 0.0

    days_to_scoreable = (
        min_signals_for_score / calls_per_day if calls_per_day > 0 else None
    )
    thin = (
        history.span_days < _MIN_SPAN_DAYS_FOR_CADENCE
        or total < _MIN_MESSAGES_FOR_CADENCE
    )

    s = StructuralScreen(
        chat_id=history.chat_id,
        name=history.name,
        messages=total,
        span_days=history.span_days,
        calls=calls,
        tradable_calls=calls,
        calls_per_day=calls_per_day,
        forward_ratio=history.forwards / max(total, 1),
        paid_pitch_ratio=paid / total,
        preannounce_ratio=pre / total,
        edit_ratio=history.edits / max(total, 1),
        repeat_symbol_ratio=repeat_ratio,
        days_to_scoreable=days_to_scoreable,
    )
    _apply_verdict(s, thin=thin, parsed=len(parsed))
    return s


def _apply_verdict(s: StructuralScreen, *, thin: bool = False,
                   parsed: int = 0) -> None:
    flags = s.flags

    if thin:
        s.verdict = (
            f"INSUFFICIENT — {s.messages} message(s) over {s.span_days:.1f}d is "
            f"not enough to judge anything. Export a wider date range."
        )
        return

    # A channel whose parses are mostly symbols the venue does not list is
    # either calling tokens you cannot trade here, or is not making calls at
    # all and the parser is reading prose.
    if parsed and s.calls == 0:
        s.verdict = (
            f"REJECT — {parsed} apparent call(s), none naming a symbol listed "
            f"on this venue. Either it trades elsewhere, or those are not calls."
        )
        return
    if parsed >= 5 and s.calls / parsed < 0.35:
        flags.append(
            f"only {s.calls}/{parsed} apparent calls name a listed symbol"
        )

    if s.paid_pitch_ratio > 0.05:
        flags.append(
            f"sells access ({100*s.paid_pitch_ratio:.0f}% of posts pitch a paid tier)"
        )
    if s.forward_ratio > 0.40:
        flags.append(f"forwards {100*s.forward_ratio:.0f}% of its content")
    if s.preannounce_ratio > 0.02:
        flags.append(
            f"pre-announces calls ({100*s.preannounce_ratio:.0f}% of posts) — "
            f"an accumulation window for whoever knows the ticker"
        )
    if s.calls_per_day > 20:
        flags.append(f"{s.calls_per_day:.0f} calls/day — volume, not selection")
    if s.repeat_symbol_ratio > 0.5:
        flags.append(
            f"{100*s.repeat_symbol_ratio:.0f}% of calls repeat an earlier symbol"
        )
    if s.edit_ratio > 0.25:
        flags.append(f"edits {100*s.edit_ratio:.0f}% of posts — check for rewritten calls")

    # Rejections, in order of how conclusive they are.
    if s.calls == 0:
        s.verdict = "REJECT — no parseable calls; this is not a signal channel."
        return
    if s.paid_pitch_ratio > 0.15:
        s.verdict = (
            "REJECT — the product is the subscription. Its incentive is a record "
            "that looks good, not one that trades well."
        )
        return
    if s.forward_ratio > 0.60:
        s.verdict = "REJECT — mostly a relay. Find what it forwards from."
        return
    if s.calls_per_day > 40:
        s.verdict = (
            "REJECT — firehose. At this rate something it named always went up, "
            "which is the point."
        )
        return
    if s.days_to_scoreable is not None and s.days_to_scoreable > 45:
        s.verdict = (
            f"REJECT — too quiet: {s.calls_per_day:.2f} calls/day needs "
            f"{s.days_to_scoreable:.0f} days to reach a scoreable sample."
        )
        return

    if flags:
        s.verdict = f"WATCH — {len(flags)} structural flag(s); record, but expect little."
        return
    s.verdict = (
        f"RECORD — clean structure, {s.calls_per_day:.1f} calls/day, "
        f"scoreable in ~{s.days_to_scoreable:.0f} days."
        if s.days_to_scoreable else "RECORD — clean structure."
    )


def summarise(screens: Sequence[StructuralScreen]) -> Dict[str, Any]:
    kept = [s for s in screens if not s.rejected]
    return {
        "channels": len(screens),
        "rejected": len(screens) - len(kept),
        "kept": len(kept),
        "median_calls_per_day": (
            statistics.median([s.calls_per_day for s in kept]) if kept else 0.0
        ),
    }
