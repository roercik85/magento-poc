"""Pre-trade risk checks and run-level equity tracking.

Every rejection is recorded with a reason. The rejection log is as valuable as
the trade log: a run that took two trades out of two hundred signals is telling
you something about either your filters or your channels, and you cannot see
which without the breakdown.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from ..config import RiskConfig
from ..models import RejectReason, Signal


@dataclass
class RiskState:
    equity: float
    peak_equity: float
    realised_pnl: float = 0.0
    trades_taken: int = 0
    open_positions: int = 0
    halted: bool = False
    halt_reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskConfig, channel_scores: Optional[Dict[int, float]] = None) -> None:
        self._cfg = cfg
        self._scores = channel_scores or {}
        self._blocked = {s.upper() for s in cfg.blocked_symbols}
        self._cooldown_until_ms: Dict[str, float] = {}
        self.state = RiskState(equity=cfg.starting_equity, peak_equity=cfg.starting_equity)
        self.rejections: Dict[RejectReason, int] = {}

    # -- checks ---------------------------------------------------------
    def check(self, signal: Signal, now_ms: float) -> Tuple[bool, Optional[RejectReason], str]:
        """Return ``(allowed, reason, detail)``.

        Ordered cheapest-first: this runs on the hot path and the common case
        is rejection.
        """
        st = self.state

        if st.halted:
            return self._no(RejectReason.DRAWDOWN_STOP, st.halt_reason)

        if st.trades_taken >= self._cfg.max_trades_per_run:
            return self._no(RejectReason.MAX_TRADES, f"{st.trades_taken} trades taken")

        if st.open_positions >= self._cfg.max_concurrent_positions:
            return self._no(
                RejectReason.MAX_POSITIONS, f"{st.open_positions} positions open"
            )

        symbol = signal.symbol or signal.contract or ""
        if not symbol:
            return self._no(RejectReason.UNKNOWN_SYMBOL, "signal names no tradable instrument")

        if (signal.base or "").upper() in self._blocked or symbol.upper() in self._blocked:
            return self._no(RejectReason.BLOCKED_SYMBOL, symbol)

        until = self._cooldown_until_ms.get(symbol)
        if until is not None and now_ms < until:
            return self._no(
                RejectReason.COOLDOWN, f"{(until - now_ms) / 1000:.0f}s remaining on {symbol}"
            )

        if self._cfg.min_channel_score > 0.0:
            score = self._scores.get(signal.raw.chat_id)
            # An unscored channel has no track record. During discovery that is
            # fine; once a minimum score is set, unknown means no.
            if score is None or score < self._cfg.min_channel_score:
                return self._no(
                    RejectReason.LOW_CHANNEL_SCORE,
                    f"channel score {score if score is not None else 'unknown'} "
                    f"< {self._cfg.min_channel_score}",
                )

        if st.equity < self._cfg.position_notional_quote:
            return self._no(
                RejectReason.INSUFFICIENT_EQUITY,
                f"equity {st.equity:.2f} < notional {self._cfg.position_notional_quote:.2f}",
            )

        return True, None, ""

    @staticmethod
    def _no(reason: RejectReason, detail: str) -> Tuple[bool, RejectReason, str]:
        return False, reason, detail

    def record_rejection(self, reason: RejectReason) -> None:
        """The single counting point for rejections.

        :meth:`check` deliberately does not count its own verdicts. Rejections
        also originate at the parser, the entry guard and the venue, and having
        two places increment the same tally is how a report ends up disagreeing
        with itself.
        """
        self.rejections[reason] = self.rejections.get(reason, 0) + 1

    # -- bookkeeping ----------------------------------------------------
    def notional_for(self, signal: Signal) -> float:
        return min(self._cfg.position_notional_quote, self.state.equity)

    def on_position_opened(self, symbol: str, now_ms: float) -> None:
        st = self.state
        st.open_positions += 1
        st.trades_taken += 1
        self._cooldown_until_ms[symbol] = now_ms + self._cfg.symbol_cooldown_s * 1000.0

    def on_position_closed(self, pnl_quote: float) -> None:
        st = self.state
        st.open_positions = max(0, st.open_positions - 1)
        st.realised_pnl += pnl_quote
        st.equity += pnl_quote
        st.peak_equity = max(st.peak_equity, st.equity)

        limit = self._cfg.daily_drawdown_stop_pct
        if limit > 0:
            dd_pct = 100.0 * (st.peak_equity - st.equity) / max(st.peak_equity, 1e-9)
            if dd_pct >= limit:
                st.halted = True
                st.halt_reason = (
                    f"drawdown {dd_pct:.2f}% from peak {st.peak_equity:.2f} "
                    f"hit the {limit:.2f}% stop"
                )

    # -- reporting ------------------------------------------------------
    @property
    def net_return_pct(self) -> float:
        start = self._cfg.starting_equity
        if start <= 0:
            return 0.0
        return 100.0 * (self.state.equity - start) / start

    def rejection_summary(self) -> Dict[str, int]:
        return {r.value: n for r, n in sorted(
            self.rejections.items(), key=lambda kv: -kv[1]
        )}
