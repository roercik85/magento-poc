"""Core domain objects.

Deliberately plain dataclasses with ``slots=True``: these are allocated on the
hot path and attribute lookup on a ``__dict__``-backed object is measurable
when you are counting microseconds.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


class SignalKind(enum.Enum):
    SYMBOL = "symbol"        # a ticker on a centralised exchange
    CONTRACT = "contract"    # an on-chain token address
    UNKNOWN = "unknown"


class Side(enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExitReason(enum.Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    TIME_EXIT = "time_exit"
    RUN_END = "run_end"
    ABORTED = "aborted"


class RejectReason(enum.Enum):
    LOW_CONFIDENCE = "low_confidence"
    IGNORED_SYMBOL = "ignored_symbol"
    BLOCKED_SYMBOL = "blocked_symbol"
    UNKNOWN_SYMBOL = "unknown_symbol"
    COOLDOWN = "cooldown"
    MAX_POSITIONS = "max_concurrent_positions"
    MAX_TRADES = "max_trades_per_run"
    DRAWDOWN_STOP = "drawdown_stop"
    LOW_CHANNEL_SCORE = "low_channel_score"
    INSUFFICIENT_EQUITY = "insufficient_equity"
    ENTRY_CHASE = "entry_chase"          # price already ran away from us
    DUPLICATE = "duplicate_message"
    EXCHANGE_REJECT = "exchange_reject"


def _uid() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(slots=True)
class RawMessage:
    """A message as it came off the wire, before any interpretation."""

    chat_id: int
    message_id: int
    text: str
    received_ns: int
    received_wall_ms: float
    channel_name: str = ""
    posted_wall_ms: Optional[float] = None   # Telegram's own timestamp
    is_edit: bool = False
    source_session: str = "primary"

    @property
    def dedupe_key(self) -> str:
        return f"{self.chat_id}:{self.message_id}"

    @property
    def fanout_ms(self) -> Optional[float]:
        """How long the message took to reach us after Telegram stamped it.

        Telegram timestamps have 1-second resolution, so this is a coarse
        measure — useful in aggregate across hundreds of messages, useless for
        a single one.
        """
        if self.posted_wall_ms is None:
            return None
        return self.received_wall_ms - self.posted_wall_ms


@dataclass(slots=True)
class Signal:
    """A parsed trading intent extracted from a message."""

    signal_id: str
    raw: RawMessage
    kind: SignalKind
    symbol: Optional[str]            # e.g. "XYZUSDT"
    base: Optional[str]              # e.g. "XYZ"
    contract: Optional[str]
    chain: Optional[str]
    confidence: float
    matched_by: str                  # which pattern fired — for tuning
    target_pcts: List[float] = field(default_factory=list)
    stop_pct: Optional[float] = None

    @staticmethod
    def new(raw: RawMessage, **kw: Any) -> "Signal":
        return Signal(signal_id=_uid(), raw=raw, **kw)


@dataclass(slots=True)
class Fill:
    side: Side
    qty: float
    price: float
    fee_quote: float
    wall_ms: float
    latency_ms: float
    simulated: bool = True

    @property
    def notional(self) -> float:
        return self.qty * self.price


@dataclass(slots=True)
class Position:
    position_id: str
    signal: Signal
    symbol: str
    opened_wall_ms: float
    entry: Fill
    qty_open: float
    exits: List[Fill] = field(default_factory=list)
    peak_price: float = 0.0
    ladder_hit: int = 0
    trailing_armed: bool = False
    closed: bool = False
    exit_reason: Optional[ExitReason] = None

    @property
    def avg_exit_price(self) -> Optional[float]:
        qty = sum(f.qty for f in self.exits)
        if qty <= 0:
            return None
        return sum(f.qty * f.price for f in self.exits) / qty

    @property
    def realised_pnl_quote(self) -> float:
        """Realised PnL on the closed portion, net of all fees.

        Entry fee is charged in full at open, so a partially-closed position
        carries the whole entry fee against it. That is conservative and it is
        what the exchange actually does to your balance.
        """
        proceeds = sum(f.qty * f.price - f.fee_quote for f in self.exits)
        closed_qty = sum(f.qty for f in self.exits)
        cost = closed_qty * self.entry.price + self.entry.fee_quote
        return proceeds - cost

    @property
    def return_pct(self) -> float:
        cost = self.entry.qty * self.entry.price
        if cost <= 0:
            return 0.0
        return 100.0 * self.realised_pnl_quote / cost

    @property
    def hold_s(self) -> float:
        if not self.exits:
            return 0.0
        return (self.exits[-1].wall_ms - self.opened_wall_ms) / 1000.0


@dataclass(slots=True)
class RejectedSignal:
    signal: Signal
    reason: RejectReason
    detail: str = ""


@dataclass(slots=True)
class ScoredChannel:
    chat_id: int
    name: str
    signals: int
    hit_rate: float
    median_return_pct: float
    mean_return_pct: float
    median_mae_pct: float            # max adverse excursion
    originator_score: float          # 1.0 = first to call, 0.0 = pure relay
    consistency: float               # 1 - normalised dispersion of returns
    pre_pump_pct: float              # how much the price already moved pre-post
    pre_run_windows: Dict[int, float]  # the same, at several lookbacks
    best_horizon_s: int              # holding period with the best median return
    best_horizon_return_pct: float   # that median, net of costs
    composite: float
    verdict: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
