"""Offline message sources: recorded corpora and a synthetic generator.

``record`` mode writes every observed message to JSONL. Replay feeds it back
through the exact same parse → risk → strategy path the live engine uses, which
is the only way a backtest result means anything.

:class:`SyntheticSource` exists so the pipeline can be exercised end to end with
no Telegram account and no network — for tests, and for seeing the machinery
work before committing to weeks of recording. Its message mix is drawn from the
shapes these channels actually post: mostly noise, some ambiguous chatter, and
a minority of genuine calls.
"""
from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import AsyncIterator, Iterator, List, Optional, Sequence

from ..clock import now_ns, wall_ms
from ..models import RawMessage


class RecordedSource:
    """Replays a JSONL corpus written by ``record`` mode."""

    def __init__(self, path: str | Path, speed: float = 0.0) -> None:
        self.path = Path(path)
        # speed 0 = as fast as possible; 1.0 = original wall-clock pacing.
        self.speed = speed

    def __iter__(self) -> Iterator[RawMessage]:
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("kind") != "message":
                    continue
                yield RawMessage(
                    chat_id=rec["chat_id"],
                    message_id=rec["message_id"],
                    text=rec["text"],
                    received_ns=now_ns(),
                    received_wall_ms=rec.get("received_wall_ms", wall_ms()),
                    channel_name=rec.get("channel_name", ""),
                    posted_wall_ms=rec.get("posted_wall_ms"),
                    is_edit=rec.get("is_edit", False),
                    source_session="replay",
                )

    async def stream(self) -> AsyncIterator[RawMessage]:
        prev: Optional[float] = None
        for msg in self:
            if self.speed > 0 and prev is not None:
                gap_s = max(0.0, (msg.received_wall_ms - prev) / 1000.0) / self.speed
                if gap_s > 0:
                    await asyncio.sleep(min(gap_s, 5.0))
            prev = msg.received_wall_ms
            yield msg


# ---------------------------------------------------------------------------
# Synthetic
# ---------------------------------------------------------------------------
_NOISE = [
    "gm everyone, markets looking choppy today",
    "Remember to DYOR. NFA.",
    "Our VIP group has 3 spots left, DM for details",
    "BTC holding 60k support, watching for a breakout",
    "Congrats to everyone who caught the last one 🔥🔥",
    "What are you all watching this week?",
    "Chart looks bullish on the 4h",
    "Anyone else still holding from last month?",
    "Join our partner channel for more alpha",
    "Market update: alts bleeding against BTC",
]

_CALL_TEMPLATES = [
    "🚀 BUY ${sym} NOW\nTarget: {t1}% {t2}%\nSL: {sl}%",
    "#{sym} /USDT — entry now, moving fast ⚡",
    "Coin: {sym}\nEntry: market\nTP1: {t1}%\nTP2: {t2}%\nSL: {sl}%",
    "APE INTO ${sym} 🔥 pump starting",
    "Next call is LIVE: {sym}USDT — go go go",
    "LONG {sym} here. Tight stop at {sl}%.",
]

_AMBIGUOUS = [
    "Big things coming for our next pick 👀",
    "Next call in 10 minutes. Get your USDT ready.",
    "$BTC and $ETH both looking strong here",
    "That ATH call was a clean 40% for anyone who followed",
]


# A fixed epoch, so a synthetic corpus is identical between processes. Using
# the current wall clock here would make every run a different scenario and the
# word "backtest" meaningless.
SYNTHETIC_EPOCH_MS = 1_700_000_000_000.0


class SyntheticSource:
    """Generates a plausible channel message stream.

    ``call_rate`` is the fraction of messages that carry a real signal. The
    default of 8% is generous; most channels are noisier than that.
    """

    def __init__(
        self,
        channels: Sequence[tuple[int, str]],
        *,
        count: int = 400,
        call_rate: float = 0.08,
        seed: int = 0,
        start_wall_ms: Optional[float] = None,
        gap_ms_mean: float = 4_000.0,
    ) -> None:
        self._channels = list(channels)
        self._count = count
        self._call_rate = call_rate
        self._rng = random.Random(seed)
        self._t = start_wall_ms if start_wall_ms is not None else SYNTHETIC_EPOCH_MS
        self._gap = gap_ms_mean
        # Per-channel quality, so the scorer has something real to discriminate.
        # Index 0 is the best channel, the last is the worst. The scorer is
        # never told any of this and has to recover it from outcomes alone.
        self._quality = {
            cid: 1.0 - (i / max(1, len(self._channels) - 1))
            for i, (cid, _name) in enumerate(self._channels)
        }
        # symbol -> quality of the channel that first called it. The scenario
        # builder feeds this to the price feed; nothing downstream sees it.
        self.ground_truth: dict[str, float] = {}

    def channel_quality(self, chat_id: int) -> float:
        return self._quality.get(chat_id, 0.5)

    def __iter__(self) -> Iterator[RawMessage]:
        symbols_used: List[str] = []
        for i in range(self._count):
            self._t += max(50.0, self._rng.expovariate(1.0 / self._gap))
            chat_id, name = self._rng.choice(self._channels)
            roll = self._rng.random()

            # Worse channels post *more*, not fewer. Volume is not quality;
            # the highest-frequency channels are usually the ones churning
            # followers, and the scorer has to see enough of their calls to
            # convict them.
            effective_rate = self._call_rate * (1.5 - 0.5 * self._quality[chat_id])
            if roll < effective_rate:
                sym = self._new_symbol(symbols_used, self._quality[chat_id])
                text = self._rng.choice(_CALL_TEMPLATES).format(
                    sym=sym,
                    t1=self._rng.choice([5, 8, 10, 15]),
                    t2=self._rng.choice([20, 25, 30, 50]),
                    sl=self._rng.choice([5, 6, 8, 10]),
                )
            elif roll < effective_rate + 0.05:
                text = self._rng.choice(_AMBIGUOUS)
            else:
                text = self._rng.choice(_NOISE)

            yield RawMessage(
                chat_id=chat_id,
                message_id=1000 + i,
                text=text,
                received_ns=now_ns(),
                received_wall_ms=self._t,
                channel_name=name,
                posted_wall_ms=self._t - self._rng.uniform(20.0, 400.0),
                is_edit=False,
                source_session="synthetic",
            )

    def _new_symbol(self, used: List[str], quality: float) -> str:
        # Low-quality channels repost a symbol somebody already called, and do
        # it late. High-quality ones mint a fresh one. This is the signal the
        # originator score is supposed to recover.
        if used and self._rng.random() > quality:
            return self._rng.choice(used[-8:])
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        sym = "".join(self._rng.choice(alphabet) for _ in range(self._rng.randint(3, 5)))
        used.append(sym)
        self.ground_truth[sym] = quality
        return sym
