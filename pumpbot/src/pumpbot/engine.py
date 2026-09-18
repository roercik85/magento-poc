"""The engine: message in, trade out, report at the end.

One code path serves both simulation and live trading. That is a deliberate
constraint — the moment the backtest and the live bot are different programs,
the backtest stops predicting anything. The only things that vary are the
message source, the price feed, and the executor.

Ordering on the hot path, cheapest first:

    raw message → extract → risk check → entry guard → submit → position

and a separate, slower loop manages exits. Entry latency is what you compete
on; exit latency is what you survive on, and the two have different budgets.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .clock import LatencyHistogram, LatencyTrace, now_ns
from .config import Config
from .execution.base import Executor, OrderRejected, OrderRequest
from .logbook import Logbook
from .models import (
    ExitReason,
    Fill,
    Position,
    RawMessage,
    RejectReason,
    RejectedSignal,
    ScoredChannel,
    Side,
    Signal,
)
from .parsing.extractor import SignalExtractor
from .risk.manager import RiskManager
from .scoring.channels import ChannelScorer, SignalObservation
from .strategy.pump import PumpStrategy


@dataclass
class RunResult:
    run_id: str
    label: str
    mode: str
    started_at: str
    finished_at: str
    duration_s: float

    messages_seen: int = 0
    signals_parsed: int = 0
    signals_rejected: int = 0
    orders_submitted: int = 0
    orders_rejected: int = 0

    positions: List[Position] = field(default_factory=list)
    rejections: Dict[str, int] = field(default_factory=dict)
    channel_scores: List[ScoredChannel] = field(default_factory=list)

    starting_equity: float = 0.0
    ending_equity: float = 0.0
    latency: Dict[str, Optional[float]] = field(default_factory=dict)
    stage_latency_ms: Dict[str, float] = field(default_factory=dict)
    halted_reason: str = ""
    scoring_horizon_s: int = 0

    # -- derived -------------------------------------------------------
    @property
    def trades(self) -> int:
        return len(self.positions)

    @property
    def closed(self) -> List[Position]:
        return [p for p in self.positions if p.exits]

    @property
    def net_pnl_quote(self) -> float:
        return self.ending_equity - self.starting_equity

    @property
    def net_return_pct(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return 100.0 * self.net_pnl_quote / self.starting_equity

    @property
    def wins(self) -> int:
        return sum(1 for p in self.closed if p.realised_pnl_quote > 0)

    @property
    def losses(self) -> int:
        return sum(1 for p in self.closed if p.realised_pnl_quote <= 0)

    @property
    def win_rate(self) -> float:
        n = len(self.closed)
        return (self.wins / n) if n else 0.0

    @property
    def profit_factor(self) -> Optional[float]:
        gains = sum(p.realised_pnl_quote for p in self.closed if p.realised_pnl_quote > 0)
        pains = -sum(p.realised_pnl_quote for p in self.closed if p.realised_pnl_quote < 0)
        if pains <= 0:
            return None if gains <= 0 else float("inf")
        return gains / pains

    @property
    def max_drawdown_pct(self) -> float:
        """Peak-to-trough on the realised equity curve."""
        equity = self.starting_equity
        peak = equity
        worst = 0.0
        for p in sorted(self.closed, key=lambda x: x.exits[-1].wall_ms):
            equity += p.realised_pnl_quote
            peak = max(peak, equity)
            if peak > 0:
                worst = max(worst, 100.0 * (peak - equity) / peak)
        return worst

    @property
    def exit_breakdown(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for p in self.closed:
            key = p.exit_reason.value if p.exit_reason else "open"
            out[key] = out.get(key, 0) + 1
        return out


class Engine:
    def __init__(
        self,
        cfg: Config,
        *,
        executor: Executor,
        feed,                                   # noqa: ANN001 - PriceFeed protocol
        logbook: Optional[Logbook] = None,
        channel_scores: Optional[Dict[int, float]] = None,
        run_id: Optional[str] = None,
    ) -> None:
        self.cfg = cfg
        self.executor = executor
        self.feed = feed
        self.log = logbook
        self.run_id = run_id or uuid.uuid4().hex[:10]

        self.extractor = SignalExtractor(
            ignore_symbols=cfg.parsing.ignore_symbols,
            quote_assets=cfg.parsing.quote_assets,
            accept_contracts=cfg.parsing.accept_contracts,
            min_confidence=cfg.parsing.min_confidence,
        )
        self.risk = RiskManager(cfg.risk, channel_scores)
        self.strategy = PumpStrategy(cfg.strategy)
        self.scorer = ChannelScorer(cfg.scoring)

        self.open_positions: Dict[str, Position] = {}
        self.all_positions: List[Position] = []
        self.rejected: List[RejectedSignal] = []

        self.messages_seen = 0
        self.signals_parsed = 0
        self.orders_submitted = 0
        self.orders_rejected = 0

        self._latency = LatencyHistogram()
        self._stage_totals: Dict[str, float] = {}
        self._stage_n = 0
        self._started_ns = now_ns()
        self._started_at = _utcnow()

    # ------------------------------------------------------------------
    # hot path
    # ------------------------------------------------------------------
    async def handle_message(self, raw: RawMessage) -> Optional[Position]:
        self.messages_seen += 1
        trace = LatencyTrace(trace_id=f"{raw.chat_id}:{raw.message_id}")

        signal = self.extractor.extract(raw)
        trace.mark("parse")
        if signal is None:
            return None

        self.signals_parsed += 1
        if self.log is not None:
            self.log.record(
                "signal",
                run_id=self.run_id,
                signal_id=signal.signal_id,
                chat_id=raw.chat_id,
                channel=raw.channel_name,
                symbol=signal.symbol or signal.contract,
                confidence=signal.confidence,
                matched_by=signal.matched_by,
                wall_ms=raw.received_wall_ms,
            )

        # Record the call for scoring regardless of whether we trade it. A
        # channel we are currently ignoring still generates the evidence that
        # decides whether to stop ignoring it.
        self._observe_for_scoring(signal)

        ok, reason, detail = self.risk.check(signal, raw.received_wall_ms)
        trace.mark("risk")
        if not ok:
            assert reason is not None
            self._reject(signal, reason, detail)
            return None

        position = await self._open_position(signal, trace)
        self._record_trace(trace)
        return position

    async def _open_position(self, signal: Signal, trace: LatencyTrace) -> Optional[Position]:
        symbol = signal.symbol or signal.contract or ""
        signal_ms = signal.raw.received_wall_ms

        # Ensure the feed knows this instrument. In simulation this mints the
        # pump path; live, it is a no-op.
        register = getattr(self.feed, "register", None)
        if register is not None:
            register(symbol, signal.raw.posted_wall_ms or signal_ms)

        reference_price = self.feed.price(symbol, signal_ms)
        if reference_price is None:
            self._reject(signal, RejectReason.UNKNOWN_SYMBOL, f"no price for {symbol}")
            return None

        notional = self.risk.notional_for(signal)
        req = OrderRequest(
            symbol=symbol,
            side=Side.BUY,
            quote_notional=notional,
            signal_wall_ms=signal_ms,
            reference_price=reference_price,
        )

        try:
            fill = await self.executor.submit(req)
        except OrderRejected as exc:
            self.orders_rejected += 1
            self._reject(signal, RejectReason.EXCHANGE_REJECT, str(exc))
            return None
        finally:
            trace.mark("submit")

        # Chase guard, applied to the price we actually got. Checking the
        # pre-trade quote instead would miss exactly the case that matters:
        # the book running away from us *during* the round trip.
        allowed, why = self.strategy.entry_allowed(reference_price, fill.price)
        if not allowed:
            self.orders_rejected += 1
            self._reject(signal, RejectReason.ENTRY_CHASE, why)
            # We are already long. Unwind immediately rather than hold an
            # entry the strategy has just disowned.
            await self._emergency_unwind(symbol, fill, signal)
            return None

        self.orders_submitted += 1
        position = Position(
            position_id=uuid.uuid4().hex[:10],
            signal=signal,
            symbol=symbol,
            opened_wall_ms=fill.wall_ms,
            entry=fill,
            qty_open=fill.qty,
            peak_price=fill.price,
        )
        self.open_positions[position.position_id] = position
        self.all_positions.append(position)
        self.risk.on_position_opened(symbol, fill.wall_ms)

        if self.log is not None:
            self.log.record(
                "entry",
                run_id=self.run_id,
                position_id=position.position_id,
                symbol=symbol,
                qty=fill.qty,
                price=fill.price,
                notional=fill.notional,
                latency_ms=fill.latency_ms,
                slippage_pct=100.0 * (fill.price - reference_price) / reference_price,
                channel=signal.raw.channel_name,
            )
        return position

    async def _emergency_unwind(self, symbol: str, fill: Fill, signal: Signal) -> None:
        try:
            await self.executor.submit(
                OrderRequest(
                    symbol=symbol,
                    side=Side.SELL,
                    qty=fill.qty,
                    signal_wall_ms=fill.wall_ms,
                )
            )
        except OrderRejected:
            # Nothing further we can do automatically; the position is logged
            # and shows up in the report as an unclosed entry.
            if self.log is not None:
                self.log.record(
                    "unwind_failed",
                    run_id=self.run_id,
                    symbol=symbol,
                    qty=fill.qty,
                    signal_id=signal.signal_id,
                )

    # ------------------------------------------------------------------
    # exits
    # ------------------------------------------------------------------
    async def tick(self, now_ms: float) -> None:
        if not self.open_positions:
            return
        for position in list(self.open_positions.values()):
            price = self.feed.price(position.symbol, now_ms)
            if price is None:
                continue
            for instruction in self.strategy.evaluate(position, price, now_ms):
                await self._execute_exit(position, instruction, now_ms)
                if position.closed:
                    break

    async def _execute_exit(self, position: Position, instruction, now_ms: float) -> None:  # noqa: ANN001
        qty = min(position.qty_open, position.entry.qty * instruction.fraction)
        if qty <= 0:
            return

        try:
            fill = await self.executor.submit(
                OrderRequest(
                    symbol=position.symbol,
                    side=Side.SELL,
                    qty=qty,
                    signal_wall_ms=now_ms,
                )
            )
        except OrderRejected as exc:
            if self.log is not None:
                self.log.record(
                    "exit_rejected",
                    run_id=self.run_id,
                    position_id=position.position_id,
                    reason=str(exc),
                )
            return

        position.exits.append(fill)
        position.qty_open -= fill.qty
        position.exit_reason = instruction.reason

        if self.log is not None:
            self.log.record(
                "exit",
                run_id=self.run_id,
                position_id=position.position_id,
                symbol=position.symbol,
                qty=fill.qty,
                price=fill.price,
                reason=instruction.reason.value,
                note=instruction.note,
            )

        # Treat dust as closed: a residue worth a fraction of a cent is not a
        # position, and chasing it costs more in fees than it is worth.
        if position.qty_open <= position.entry.qty * 1e-6:
            self._close(position)

    def _close(self, position: Position) -> None:
        position.closed = True
        position.qty_open = 0.0
        self.open_positions.pop(position.position_id, None)
        self.risk.on_position_closed(position.realised_pnl_quote)

    async def close_all(self, now_ms: float, reason: ExitReason = ExitReason.RUN_END) -> None:
        for position in list(self.open_positions.values()):
            price = self.feed.price(position.symbol, now_ms)
            if price is None:
                # No mark available: close at entry so the run does not book a
                # fictional profit or loss on an unpriceable leftover.
                position.exits.append(
                    Fill(
                        side=Side.SELL,
                        qty=position.qty_open,
                        price=position.entry.price,
                        fee_quote=0.0,
                        wall_ms=now_ms,
                        latency_ms=0.0,
                        simulated=self.executor.simulated,
                    )
                )
                position.exit_reason = reason
                self._close(position)
                continue

            from .strategy.pump import ExitInstruction

            await self._execute_exit(
                position, ExitInstruction(1.0, reason, "run ended"), now_ms
            )
            if not position.closed:
                self._close(position)

    # ------------------------------------------------------------------
    # scoring / bookkeeping
    # ------------------------------------------------------------------
    def _observe_for_scoring(self, signal: Signal) -> None:
        """Record what the market did around this call, for channel scoring.

        Measured from the *post* price with zero latency — the best case a
        follower could ever achieve. If a channel is unprofitable on this
        basis, no amount of infrastructure will rescue it.
        """
        symbol = signal.symbol or signal.contract or ""
        post_ms = signal.raw.posted_wall_ms or signal.raw.received_wall_ms

        register = getattr(self.feed, "register", None)
        if register is not None:
            register(symbol, post_ms)

        price_at_post = self.feed.price(symbol, post_ms)
        if price_at_post is None or price_at_post <= 0:
            return

        horizons = self.cfg.marketdata.return_horizons_s
        returns: Dict[int, float] = {}
        for h in horizons:
            p = self.feed.price(symbol, post_ms + h * 1000.0)
            if p is not None:
                returns[h] = 100.0 * (p - price_at_post) / price_at_post

        # Max adverse excursion inside the primary horizon, sampled every 5s.
        primary = self.cfg.scoring.primary_horizon_s
        worst = 0.0
        for offset in range(0, primary * 1000 + 1, 5_000):
            p = self.feed.price(symbol, post_ms + offset)
            if p is not None:
                worst = min(worst, 100.0 * (p - price_at_post) / price_at_post)

        pre = self.feed.price(symbol, post_ms - 300_000)
        pre_run = 100.0 * (price_at_post - pre) / pre if pre and pre > 0 else 0.0

        self.scorer.observe(
            SignalObservation(
                chat_id=signal.raw.chat_id,
                channel_name=signal.raw.channel_name,
                symbol=symbol,
                post_ms=post_ms,
                price_at_post=price_at_post,
                returns_pct=returns,
                mae_pct=worst,
                pre_run_pct=pre_run,
                cost_pct=self._round_trip_cost_pct(),
            )
        )

    def _round_trip_cost_pct(self) -> float:
        sim = self.cfg.execution.simulation
        notional = self.cfg.risk.position_notional_quote
        impact_bps = sim.slippage_impact_coeff * (notional / max(sim.assumed_depth_quote, 1.0))
        one_way_bps = sim.slippage_bps_base + impact_bps + sim.taker_fee_bps
        return 2.0 * one_way_bps / 100.0

    def _reject(self, signal: Signal, reason: RejectReason, detail: str) -> None:
        self.rejected.append(RejectedSignal(signal=signal, reason=reason, detail=detail))
        self.risk.record_rejection(reason)
        if self.log is not None:
            self.log.record(
                "reject",
                run_id=self.run_id,
                signal_id=signal.signal_id,
                chat_id=signal.raw.chat_id,
                symbol=signal.symbol or signal.contract,
                reason=reason.value,
                detail=detail,
            )

    def _record_trace(self, trace: LatencyTrace) -> None:
        self._latency.add(trace.total_ms)
        self._stage_n += 1
        for stage, ms in trace.as_ms().items():
            self._stage_totals[stage] = self._stage_totals.get(stage, 0.0) + ms

    # ------------------------------------------------------------------
    # result
    # ------------------------------------------------------------------
    def result(self) -> RunResult:
        stage_avg = {
            k: v / self._stage_n for k, v in self._stage_totals.items()
        } if self._stage_n else {}

        return RunResult(
            run_id=self.run_id,
            label=self.cfg.run_label,
            mode=self.cfg.mode,
            started_at=self._started_at,
            finished_at=_utcnow(),
            duration_s=(now_ns() - self._started_ns) / 1e9,
            messages_seen=self.messages_seen,
            signals_parsed=self.signals_parsed,
            signals_rejected=len(self.rejected),
            orders_submitted=self.orders_submitted,
            orders_rejected=self.orders_rejected,
            positions=self.all_positions,
            rejections=self.risk.rejection_summary(),
            channel_scores=self.scorer.score_all(),
            starting_equity=self.cfg.risk.starting_equity,
            ending_equity=self.risk.state.equity,
            latency=self._latency.summary(),
            stage_latency_ms=stage_avg,
            halted_reason=self.risk.state.halt_reason,
            scoring_horizon_s=self.cfg.scoring.primary_horizon_s,
        )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
