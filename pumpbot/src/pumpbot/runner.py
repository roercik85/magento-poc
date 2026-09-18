"""Run loops.

Simulation runs on *virtual* time driven by message timestamps, not wall clock.
A month of channel traffic replays in seconds, and two runs of the same seed
produce byte-identical results. Live runs on the real clock with a tick task.

Both drive the same :class:`~pumpbot.engine.Engine`.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .clock import wall_ms
from .config import Config
from .engine import Engine, RunResult
from .execution.base import Executor
from .execution.simulator import SimulatedExecutor
from .ingest.replay import RecordedSource, SyntheticSource
from .logbook import Logbook
from .marketdata.feed import SyntheticPumpFeed
from .models import RawMessage

# Granularity at which open positions are re-evaluated in simulation. 1 s is
# well inside the strategy's shortest reaction (a 5% trailing stop on a 40 s
# spike) while keeping a long replay cheap.
_SIM_TICK_MS = 1_000.0


class SimulationRunner:
    """Deterministic, virtual-time replay."""

    def __init__(
        self,
        cfg: Config,
        messages: Iterable[RawMessage],
        *,
        seed: int = 0,
        logbook: Optional[Logbook] = None,
        symbol_quality: Optional[Dict[str, float]] = None,
        channel_scores: Optional[Dict[int, float]] = None,
        feed=None,                              # noqa: ANN001 - PriceFeed protocol
    ) -> None:
        self.cfg = cfg
        self._messages = messages
        self._seed = seed
        self._log = logbook

        # A real corpus needs real prices. Falling back to the synthetic feed
        # here would produce confident numbers about symbols it has never heard
        # of, which is worse than no backtest because it looks like one.
        self.feed = feed if feed is not None else SyntheticPumpFeed(
            seed=seed, depth_quote=cfg.execution.simulation.assumed_depth_quote
        )
        self.synthetic_feed = feed is None
        # Scenario ground truth, when replaying a synthetic corpus. Applied to
        # the feed under every quote asset the extractor might pair the base
        # with, since the engine trades "PEPEUSDT" while the scenario knows
        # only "PEPE".
        if self.synthetic_feed:
            for base, quality in (symbol_quality or {}).items():
                self.feed.set_symbol_quality(base, quality)
                for quote in cfg.parsing.quote_assets:
                    self.feed.set_symbol_quality(f"{base}{quote}", quality)
        self.executor: Executor = SimulatedExecutor(
            cfg.execution.simulation, self.feed, seed=seed
        )
        self.engine = Engine(
            cfg,
            executor=self.executor,
            feed=self.feed,
            logbook=logbook,
            channel_scores=channel_scores,
        )

    async def run(self) -> RunResult:
        if self._log is not None:
            await self._log.start()

        virtual_ms: Optional[float] = None
        try:
            for msg in self._messages:
                if virtual_ms is not None and msg.received_wall_ms > virtual_ms:
                    await self._advance_to(virtual_ms, msg.received_wall_ms)
                virtual_ms = msg.received_wall_ms

                await self.engine.handle_message(msg)

                if self.engine.risk.state.halted:
                    break

            # Let open positions play out to their natural exits rather than
            # marking them flat at the last message. Cutting here would flatter
            # or punish the run arbitrarily depending on where the corpus ends.
            if virtual_ms is not None:
                tail_ms = (self.cfg.strategy.max_hold_s + 60) * 1000.0
                await self._advance_to(virtual_ms, virtual_ms + tail_ms)
                await self.engine.close_all(virtual_ms + tail_ms)
        finally:
            if self._log is not None:
                await self._log.stop()

        return self.engine.result()

    async def _advance_to(self, start_ms: float, end_ms: float) -> None:
        t = start_ms
        while t < end_ms:
            t = min(t + _SIM_TICK_MS, end_ms)
            await self.engine.tick(t)
            if not self.engine.open_positions:
                # Nothing to manage: jump straight to the next message rather
                # than grinding through empty ticks.
                return


class LiveRunner:
    """Real-time loop over Telegram.

    Used for ``record`` (ingest only, no orders) and ``live`` (real orders).
    """

    def __init__(
        self,
        cfg: Config,
        *,
        executor: Executor,
        feed,                                   # noqa: ANN001
        logbook: Optional[Logbook] = None,
        tick_interval_s: float = 0.25,
        record_only: bool = False,
    ) -> None:
        self.cfg = cfg
        self._log = logbook
        self._tick_s = tick_interval_s
        self._record_only = record_only
        self.engine = Engine(cfg, executor=executor, feed=feed, logbook=logbook)
        self._ingest = None
        self._stop = asyncio.Event()

    async def run(self) -> RunResult:
        from .ingest.telegram import TelegramIngest

        if self._log is not None:
            await self._log.start()

        self._ingest = TelegramIngest(self.cfg.telegram, self._on_message)
        await self._ingest.start()
        await self.executor_start()

        ticker = asyncio.create_task(self._tick_loop(), name="exit-ticker")
        warmer = asyncio.create_task(self._warm_loop(), name="connection-warmer")
        try:
            await self._stop.wait()
        finally:
            for task in (ticker, warmer):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self.engine.close_all(wall_ms())
            await self._ingest.stop()
            await self.engine.executor.close()
            if self._log is not None:
                await self._log.stop()

        result = self.engine.result()
        result.messages_seen = max(result.messages_seen, self._ingest.messages_seen)
        return result

    async def executor_start(self) -> None:
        await self.engine.executor.start()

    def stop(self) -> None:
        self._stop.set()

    async def _on_message(self, raw: RawMessage) -> None:
        if self._log is not None:
            # In record mode this file is the corpus every later backtest reads.
            self._log.record(
                "message",
                chat_id=raw.chat_id,
                message_id=raw.message_id,
                text=raw.text,
                channel_name=raw.channel_name,
                received_wall_ms=raw.received_wall_ms,
                posted_wall_ms=raw.posted_wall_ms,
                is_edit=raw.is_edit,
                source_session=raw.source_session,
            )
        if self._record_only:
            # Still parse, so the corpus carries parser output and the channel
            # scorer accumulates — just never send an order.
            self.engine.messages_seen += 1
            signal = self.engine.extractor.extract(raw)
            if signal is not None:
                self.engine.signals_parsed += 1
            return

        await self.engine.handle_message(raw)

    async def _tick_loop(self) -> None:
        while not self._stop.is_set():
            await self.engine.tick(wall_ms())
            if self.engine.risk.state.halted:
                self.stop()
                return
            await asyncio.sleep(self._tick_s)

    async def _warm_loop(self) -> None:
        warm = getattr(self.engine.executor, "warm", None)
        if warm is None:
            return
        while not self._stop.is_set():
            await warm()
            await asyncio.sleep(30.0)


# ---------------------------------------------------------------------------
# convenience builders
# ---------------------------------------------------------------------------
DEFAULT_SYNTHETIC_CHANNELS: Sequence[Tuple[int, str]] = (
    (-1001000000001, "alpha_originator"),
    (-1001000000002, "fast_calls"),
    (-1001000000003, "midtier_signals"),
    (-1001000000004, "relay_reposts"),
    (-1001000000005, "exit_liquidity_vip"),
)


def synthetic_messages(
    *, count: int = 400, seed: int = 0, call_rate: float = 0.08
) -> Tuple[List[RawMessage], Dict[str, float]]:
    """Build a synthetic corpus plus its ground truth.

    The ground truth maps each base symbol to the quality of the channel that
    first called it. It is fed to the price feed so the scenario has a real
    answer, and to nothing else — the scorer must find it on its own.
    """
    source = SyntheticSource(
        DEFAULT_SYNTHETIC_CHANNELS, count=count, call_rate=call_rate, seed=seed
    )
    messages = list(source)
    return messages, dict(source.ground_truth)


def recorded_messages(path: str) -> List[RawMessage]:
    return list(RecordedSource(path))
