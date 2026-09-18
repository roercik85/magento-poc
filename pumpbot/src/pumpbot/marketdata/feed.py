"""Price feeds.

Two implementations:

* :class:`SyntheticPumpFeed` — a parameterised model of what a pumped low-cap
  actually does. Used for simulation and for regression-testing the strategy
  without a network. It is deliberately unkind.
* :class:`BinanceFeed` — live book ticker over REST/WebSocket.

The synthetic model is the important one, because it encodes the central
uncomfortable fact about pump channels: **the move usually starts before the
message is posted.** Insiders and the organisers are positioned first; the
channel post is the distribution event, not the accumulation event. A simulator
that starts the price at 1.0 the instant the message arrives will tell you this
strategy prints money. It does not.
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from typing import Dict, Optional, Protocol


class PriceFeed(Protocol):
    def price(self, symbol: str, t_ms: float) -> Optional[float]:
        """Mid price for ``symbol`` at wall-clock ``t_ms``, or None if unknown."""
        ...

    def depth_quote(self, symbol: str) -> float:
        """Approximate resting size near touch, in quote currency."""
        ...


# ---------------------------------------------------------------------------
# Synthetic
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class PumpProfile:
    """Shape of a single pump event, in price-multiple space."""

    t0_ms: float                 # when accumulation began
    post_ms: float               # when the channel posted
    peak_ms: float               # when price topped out
    peak_multiple: float         # peak / baseline
    pre_post_multiple: float     # price at post time / baseline
    decay_halflife_s: float      # how fast it bleeds back after the peak
    floor_multiple: float        # where it settles
    noise_bps: float             # tick-to-tick jitter
    baseline: float              # absolute price at t0
    jitter_seed: int             # stable across processes; see price()


class SyntheticPumpFeed:
    """Deterministic-per-symbol synthetic pump paths.

    Seeded from the symbol name plus a run seed, so a given run is exactly
    reproducible while different runs explore different draws.
    """

    def __init__(
        self,
        seed: int = 0,
        *,
        pre_post_run_pct_mean: float = 22.0,
        pre_post_run_pct_sd: float = 14.0,
        peak_over_post_pct_mean: float = 9.0,
        peak_over_post_pct_sd: float = 12.0,
        seconds_post_to_peak_mean: float = 38.0,
        depth_quote: float = 25_000.0,
    ) -> None:
        self._seed = seed
        self._profiles: Dict[str, PumpProfile] = {}
        self._quality: Dict[str, float] = {}
        self._depth = depth_quote
        self._pre_mean = pre_post_run_pct_mean
        self._pre_sd = pre_post_run_pct_sd
        self._peak_mean = peak_over_post_pct_mean
        self._peak_sd = peak_over_post_pct_sd
        self._to_peak_mean = seconds_post_to_peak_mean

    # -- construction ---------------------------------------------------
    def _rng(self, symbol: str) -> random.Random:
        h = hashlib.blake2b(f"{self._seed}:{symbol}".encode(), digest_size=8).digest()
        return random.Random(int.from_bytes(h, "big"))

    def set_symbol_quality(self, symbol: str, quality: float) -> None:
        """Plant ground truth for a scenario: 0.0 = pure exit liquidity, 1.0 = real call.

        Only the scenario builder calls this. The engine and the channel scorer
        never see it — that is the point. It exists so the scorer can be
        validated against a known answer instead of against its own output.
        """
        self._quality[symbol] = max(0.0, min(1.0, quality))

    def register(self, symbol: str, post_ms: float) -> PumpProfile:
        """Create (once) the pump path for ``symbol`` announced at ``post_ms``.

        First registration wins. A channel that reposts the same symbol later
        therefore inherits the original path and sees the price already run —
        which is exactly what being a relay costs you, and is measured rather
        than assumed.
        """
        existing = self._profiles.get(symbol)
        if existing is not None:
            return existing

        r = self._rng(symbol)
        q = self._quality.get(symbol, 0.5)

        # How far the price has *already* run by the time the message lands.
        # Low-quality calls have run further: the organisers were in first.
        pre_pct = max(0.0, r.gauss(self._pre_mean * (1.5 - q), self._pre_sd))
        pre_post_multiple = 1.0 + pre_pct / 100.0

        # How much further it goes after the post. This is the only part a
        # follower can capture, and for a low-quality call it is negative.
        extra_pct = r.gauss(self._peak_mean * (2.0 * q - 0.6), self._peak_sd)
        peak_multiple = pre_post_multiple * (1.0 + max(-0.02, extra_pct / 100.0))

        seconds_to_peak = max(3.0, r.gauss(self._to_peak_mean, self._to_peak_mean * 0.6))
        halflife = max(8.0, r.gauss(45.0, 25.0))

        # Where it settles: pumps round-trip most of the move, and a meaningful
        # minority end below where they started.
        floor_multiple = 1.0 + (pre_pct / 100.0) * r.uniform(-0.15, 0.35)

        return self._profiles.setdefault(
            symbol,
            PumpProfile(
                t0_ms=post_ms - r.uniform(60_000, 900_000),
                post_ms=post_ms,
                peak_ms=post_ms + seconds_to_peak * 1000.0,
                peak_multiple=peak_multiple,
                pre_post_multiple=pre_post_multiple,
                decay_halflife_s=halflife,
                floor_multiple=max(0.25, floor_multiple),
                noise_bps=r.uniform(15.0, 60.0),
                baseline=10 ** r.uniform(-6, -1),
                jitter_seed=r.getrandbits(48),
            ),
        )

    # -- PriceFeed ------------------------------------------------------
    def price(self, symbol: str, t_ms: float) -> Optional[float]:
        p = self._profiles.get(symbol)
        if p is None:
            return None

        if t_ms <= p.t0_ms:
            mult = 1.0
        elif t_ms <= p.post_ms:
            # Accumulation ramp: smooth, mostly hidden from the channel.
            frac = (t_ms - p.t0_ms) / max(1.0, p.post_ms - p.t0_ms)
            mult = 1.0 + (p.pre_post_multiple - 1.0) * (frac ** 1.8)
        elif t_ms <= p.peak_ms:
            # The public leg. Fast, and this is the whole opportunity.
            frac = (t_ms - p.post_ms) / max(1.0, p.peak_ms - p.post_ms)
            mult = p.pre_post_multiple + (p.peak_multiple - p.pre_post_multiple) * math.sin(
                frac * math.pi / 2
            )
        else:
            # Exponential bleed toward the floor.
            dt_s = (t_ms - p.peak_ms) / 1000.0
            decay = 0.5 ** (dt_s / p.decay_halflife_s)
            mult = p.floor_multiple + (p.peak_multiple - p.floor_multiple) * decay

        # Deterministic jitter, so repeated reads at the same timestamp agree.
        # Seeded from the profile rather than hash(): str.__hash__ is salted per
        # process, which would make results differ between runs of the same
        # backtest and quietly destroy reproducibility.
        bucket = int(t_ms // 250)
        jitter_r = random.Random(p.jitter_seed ^ (bucket * 0x9E3779B1))
        jitter = 1.0 + jitter_r.gauss(0.0, p.noise_bps / 10_000.0)
        return max(1e-12, p.baseline * mult * jitter)

    def depth_quote(self, symbol: str) -> float:
        return self._depth

    def profile(self, symbol: str) -> Optional[PumpProfile]:
        return self._profiles.get(symbol)


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------
class BinanceFeed:
    """Live book-ticker feed.

    Kept minimal on purpose: the engine only needs a mark price and a depth
    estimate. Anything richer belongs in a real market-data service, not in the
    same process as the order router.
    """

    def __init__(self, rest_base: str, session=None) -> None:  # noqa: ANN001
        self._rest_base = rest_base.rstrip("/")
        self._session = session
        self._last: Dict[str, tuple[float, float]] = {}   # symbol -> (price, t_ms)
        self._depth: Dict[str, float] = {}

    def on_book_ticker(self, symbol: str, bid: float, ask: float, qty_quote: float, t_ms: float) -> None:
        """Feed a websocket bookTicker update in. Called from the WS reader."""
        self._last[symbol] = ((bid + ask) / 2.0, t_ms)
        self._depth[symbol] = qty_quote

    def price(self, symbol: str, t_ms: float) -> Optional[float]:
        entry = self._last.get(symbol)
        if entry is None:
            return None
        price, stamped = entry
        # Refuse to mark off a stale quote; a stale price in a pump is worse
        # than no price, because it is confidently wrong.
        if t_ms - stamped > 5_000:
            return None
        return price

    def depth_quote(self, symbol: str) -> float:
        return self._depth.get(symbol, 0.0)
