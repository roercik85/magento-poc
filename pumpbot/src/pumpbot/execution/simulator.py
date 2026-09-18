"""Paper executor with an explicitly pessimistic microstructure model.

The model has four parts, and each one exists because leaving it out is a
well-known way to produce a backtest that cannot be reproduced with money:

1. **Telegram fanout delay.** The message is timestamped when the channel
   posts, not when it reaches you. Telegram's fanout to a large channel is not
   instant and is not uniform across subscribers.
2. **Our own latency.** Parse, decide, sign, send, match. Lognormal, because
   latency distributions have a hard floor and a long right tail.
3. **Slippage.** Market orders into a thin, moving book. Scales with the size
   you are pushing relative to resting depth.
4. **Rejects.** Symbol halted, insufficient liquidity, rate limit. These
   cluster at exactly the moment everyone else is also trying to trade.

The price used for the fill is the feed price **at fill time**, not at signal
time. That single detail is where most of the difference between a plausible
backtest and an honest one lives.
"""
from __future__ import annotations

import math
import random

from ..clock import wall_ms
from ..config import SimulationConfig
from ..models import Fill, Side
from .base import Executor, OrderRejected, OrderRequest


def _lognormal_ms(rng: random.Random, median_ms: float, sigma: float) -> float:
    """Draw from a lognormal whose *median* is ``median_ms``.

    Parameterising by median rather than mean is deliberate: median latency is
    what you can measure with a stopwatch, and the mean of a lognormal is
    dominated by the tail you cannot.
    """
    if median_ms <= 0:
        return 0.0
    return math.exp(math.log(median_ms) + rng.gauss(0.0, sigma))


class SimulatedExecutor(Executor):
    simulated = True

    def __init__(self, cfg: SimulationConfig, feed, seed: int = 0) -> None:  # noqa: ANN001
        self._cfg = cfg
        self._feed = feed
        self._rng = random.Random(seed ^ 0x5EED)
        self.total_latency_ms: float = 0.0
        self.orders: int = 0
        self.rejects: int = 0

    def sample_fanout_ms(self) -> float:
        return _lognormal_ms(
            self._rng,
            self._cfg.telegram_fanout_ms_median,
            self._cfg.telegram_fanout_ms_sigma,
        )

    def sample_latency_ms(self) -> float:
        return _lognormal_ms(
            self._rng, self._cfg.latency_ms_median, self._cfg.latency_ms_sigma
        )

    async def submit(self, req: OrderRequest) -> Fill:
        self.orders += 1
        cfg = self._cfg

        if self._rng.random() < cfg.reject_probability:
            self.rejects += 1
            raise OrderRejected("simulated venue reject (halt / no liquidity / rate limit)")

        latency_ms = self.sample_latency_ms()
        fill_wall_ms = (req.signal_wall_ms or wall_ms()) + latency_ms

        price = self._feed.price(req.symbol, fill_wall_ms)
        if price is None:
            self.rejects += 1
            raise OrderRejected(f"no market data for {req.symbol}")

        # ---- size -----------------------------------------------------
        if req.side is Side.BUY:
            if not req.quote_notional or req.quote_notional <= 0:
                raise OrderRejected("buy order without quote notional")
            notional = req.quote_notional
        else:
            if not req.qty or req.qty <= 0:
                raise OrderRejected("sell order without quantity")
            notional = req.qty * price

        # ---- slippage -------------------------------------------------
        depth = self._feed.depth_quote(req.symbol) or cfg.assumed_depth_quote
        impact_bps = cfg.slippage_impact_coeff * (notional / max(depth, 1.0))
        slip_bps = cfg.slippage_bps_base + impact_bps
        # Slippage is always adverse: you pay up to buy, you give up to sell.
        direction = 1.0 if req.side is Side.BUY else -1.0
        fill_price = price * (1.0 + direction * slip_bps / 10_000.0)
        fill_price = max(fill_price, 1e-12)

        qty = (notional / fill_price) if req.side is Side.BUY else req.qty
        assert qty is not None
        fee_quote = qty * fill_price * (cfg.taker_fee_bps / 10_000.0)

        self.total_latency_ms += latency_ms
        return Fill(
            side=req.side,
            qty=qty,
            price=fill_price,
            fee_quote=fee_quote,
            wall_ms=fill_wall_ms,
            latency_ms=latency_ms,
            simulated=True,
        )
