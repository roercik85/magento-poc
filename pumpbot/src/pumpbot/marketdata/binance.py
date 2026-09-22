"""Binance market data.

Two endpoints, deliberately separated:

* ``data-api.binance.vision`` — public market data, no key, no geo-block. This
  is what scoring and triage use, and it works from anywhere.
* ``api.binance.com`` — trading. Needs credentials, and is refused with HTTP
  451 in a number of jurisdictions.

Splitting them means the analysis runs where the trading cannot, which is the
situation that produced the previous round's mistake: the channels named
Binance, Binance was unreachable, so everything was measured on KuCoin and
twelve of thirty-six symbols were dropped as "not listed" — including tokens
with half a million dollars of daily volume.

**Binance serves one-second klines, at least thirty days back.** KuCoin's floor
is one minute, which cannot see a thirty-second spike at all; the whole reason
for recording ticks live was to get resolution that this endpoint simply hands
over for the past. A pump that peaks forty seconds after a call is measurable
here, retrospectively, for free.

Two windows are therefore fetched per call, because they answer different
questions and have wildly different sizes:

* **seconds around the call** — what a follower could actually have captured.
* **days before the call** — whether somebody was accumulating before anyone
  was told. That is the question a five-minute lookback cannot ask, and it
  needs minute candles, because three days of seconds is 260,000 bars.
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Market data. Not api.binance.com: that is geo-blocked in places where this
# endpoint is not, and no analysis needs the trading host.
DATA_BASE = "https://data-api.binance.vision"
TRADE_BASE = "https://api.binance.com"

# USD-M perpetual futures. A separate venue with a separate symbol universe:
# GRASS, MOODENG and CHILLGUY are perpetuals and are not spot pairs at all, so
# a spot-only search drops those calls as "not listed" when the market the
# channel meant is right there. Channels named after Binance frequently mean
# futures — that is where the leverage is, and where a pump moves furthest.
FUTURES_BASE = "https://fapi.binance.com"

SPOT, FUTURES = "spot", "futures"

# Binance klines are [openTime, open, high, low, close, volume, closeTime,
# quoteAssetVolume, ...] — ordinary OHLC order, unlike KuCoin, which puts
# close before high and low.
_K_TIME, _K_OPEN, _K_HIGH, _K_LOW, _K_CLOSE = 0, 1, 2, 3, 4
_K_QUOTE_VOLUME = 7

_PAGE_LIMIT = 1000
_PACE_S = 0.12
_MAX_PAGES = 40


async def pick_data_base(
    session,                                     # noqa: ANN001
    *,
    preferred: str = TRADE_BASE,
    fallback: str = DATA_BASE,
) -> Tuple[str, str]:
    """Choose the market-data host, preferring the full one.

    ``api.binance.com`` answers HTTP 451 from a number of jurisdictions.
    ``data-api.binance.vision`` answers from anywhere and its prices are
    current. Both were observed serving the same 1370 trading symbols, so the
    preference here is for the authoritative host rather than a measured
    difference — an earlier version of this docstring asserted the mirror's
    symbol list was smaller, which the data did not support.

    Returns ``(base, note)``; the note is empty when the full host was used.
    """
    try:
        async with session.get(f"{preferred.rstrip('/')}/api/v3/ping") as resp:
            if resp.status == 200:
                return preferred, ""
            status = resp.status
    except Exception as exc:                     # noqa: BLE001
        status = f"{type(exc).__name__}"

    return fallback, (
        f"{preferred} is unreachable here ({status}); using {fallback}, which "
        f"serves current prices for the same trading symbols"
    )


class BinanceSymbols:
    """Tradable symbols and their filters, across spot and futures."""

    def __init__(self) -> None:
        self._rules: Dict[str, Dict[str, Any]] = {}

    @classmethod
    async def load(
        cls,
        session,                                 # noqa: ANN001
        base: str = DATA_BASE,
        *,
        futures_base: Optional[str] = None,
    ) -> "BinanceSymbols":
        """Load spot, and optionally merge the perpetual futures universe.

        Spot wins a name collision: a symbol tradable on both is quoted and
        traded on spot, where there is no funding rate and no liquidation.
        """
        async with session.get(f"{base.rstrip('/')}/api/v3/exchangeInfo") as resp:
            payload = await resp.json()
        self = cls.from_payload(payload)

        if futures_base:
            try:
                async with session.get(
                    f"{futures_base.rstrip('/')}/fapi/v1/exchangeInfo"
                ) as resp:
                    if resp.status == 200:
                        self.merge_futures(await resp.json())
            except Exception:                    # noqa: BLE001 - geo-blocked, offline
                pass
        return self

    def merge_futures(self, payload: Dict[str, Any]) -> int:
        """Add perpetuals that spot does not already carry. Returns how many."""
        added = 0
        for sym in payload.get("symbols", []):
            if sym.get("status") != "TRADING":
                continue
            if sym.get("contractType") not in (None, "PERPETUAL"):
                continue
            name = sym.get("symbol")
            if not name or name in self._rules:
                continue
            self._rules[name] = {
                "base": sym.get("baseAsset", ""),
                "quote": sym.get("quoteAsset", ""),
                "market": FUTURES,
            }
            added += 1
        return added

    def market(self, symbol: str) -> Optional[str]:
        rule = self.rule(symbol)
        return rule.get("market") if rule else None

    @property
    def futures_symbols(self) -> List[str]:
        return [k for k, v in self._rules.items() if v.get("market") == FUTURES]

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "BinanceSymbols":
        self = cls()
        for sym in payload.get("symbols", []):
            if sym.get("status") != "TRADING":
                continue
            entry: Dict[str, Any] = {
                "base": sym.get("baseAsset", ""),
                "quote": sym.get("quoteAsset", ""),
                "market": SPOT,
            }
            for f in sym.get("filters", []):
                kind = f.get("filterType")
                if kind == "LOT_SIZE":
                    entry["step_size"] = float(f.get("stepSize") or 0.0)
                    entry["min_qty"] = float(f.get("minQty") or 0.0)
                elif kind in ("MIN_NOTIONAL", "NOTIONAL"):
                    entry["min_notional"] = float(f.get("minNotional") or 0.0)
            self._rules[sym["symbol"]] = entry
        return self

    def resolve(self, symbol: str) -> Optional[str]:
        """Binance uses the compact form already; normalise separators anyway."""
        compact = symbol.replace("-", "").replace("/", "").upper()
        return compact if compact in self._rules else None

    def rule(self, symbol: str) -> Optional[Dict[str, Any]]:
        venue = self.resolve(symbol)
        return self._rules.get(venue) if venue else None

    def min_notional(self, symbol: str) -> float:
        rule = self.rule(symbol)
        return float(rule.get("min_notional", 0.0)) if rule else 0.0

    def __len__(self) -> int:
        return len(self._rules)

    @property
    def venue_symbols(self) -> List[str]:
        return list(self._rules)


async def _fetch_range(
    session,                                     # noqa: ANN001
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    base: str = DATA_BASE,
    market: str = SPOT,
) -> List[List[Any]]:
    """All klines in a range, paging forward.

    Binance caps a response at 1000 and returns oldest first. Without paging, a
    three-day minute window returns its first sixteen hours and the rest looks
    like a symbol that did not exist yet.
    """
    out: List[List[Any]] = []
    cursor = start_ms
    path = "/fapi/v1/klines" if market == FUTURES else "/api/v3/klines"
    for _ in range(_MAX_PAGES):
        url = (
            f"{base.rstrip('/')}{path}?symbol={symbol}&interval={interval}"
            f"&startTime={cursor}&endTime={end_ms}&limit={_PAGE_LIMIT}"
        )
        try:
            async with session.get(url) as resp:
                if resp.status == 429:
                    await asyncio.sleep(float(resp.headers.get("Retry-After", 5)))
                    continue
                if resp.status != 200:
                    break
                page = await resp.json()
        except Exception:                        # noqa: BLE001 - delisted, network
            break

        if not isinstance(page, list) or not page:
            break
        out.extend(page)
        if len(page) < _PAGE_LIMIT:
            break
        # Step past the last open time; reusing it would return it forever.
        last_open = int(page[-1][_K_TIME])
        if last_open <= cursor:
            break
        cursor = last_open + 1
        await asyncio.sleep(_PACE_S)

    return out


def _row(symbol: str, kline: Sequence[Any]) -> str:
    return json.dumps({
        "symbol": symbol,
        "t_ms": int(kline[_K_TIME]),
        "close": float(kline[_K_CLOSE]),
        "high": float(kline[_K_HIGH]),
        "low": float(kline[_K_LOW]),
        "quote_volume": float(kline[_K_QUOTE_VOLUME]),
    })


async def fetch_klines(
    session,                                     # noqa: ANN001
    symbols_and_times: Sequence[Tuple[str, float]],
    out_path: str | Path,
    *,
    base: str = DATA_BASE,
    futures_base: str = FUTURES_BASE,
    spike_before_s: int = 600,
    spike_after_s: int = 1_800,
    lookback_days: int = 3,
    symbols: Optional[BinanceSymbols] = None,
    progress=None,                               # noqa: ANN001
) -> Dict[str, int]:
    """Fetch, per call, seconds around it and minutes before it.

    The dense window is at one-second resolution, which is what makes a
    thirty-second spike measurable at all. The sparse window is minutes,
    because three days of seconds would be a quarter of a million bars per
    symbol to answer a question that minutes answer perfectly well.

    Both land in the same file, and :class:`~pumpbot.marketdata.historical
    .HistoricalFeed` reads whichever is nearer when asked for a price.
    """
    wanted: Dict[str, List[float]] = defaultdict(list)
    for symbol, post_ms in symbols_and_times:
        venue = symbols.resolve(symbol) if symbols else symbol.upper()
        if venue:
            wanted[venue].append(post_ms)

    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    written: Dict[str, int] = {}

    for venue, stamps in wanted.items():
        rows: List[str] = []
        stamps.sort()
        market = symbols.market(venue) if symbols else SPOT
        host = futures_base if market == FUTURES else base

        # Merge the dense windows so a symbol called five times in an hour is
        # not fetched five times over.
        spans: List[Tuple[int, int]] = []
        for post_ms in stamps:
            start = int(post_ms - spike_before_s * 1000)
            end = int(post_ms + spike_after_s * 1000)
            if spans and start <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], end))
            else:
                spans.append((start, end))

        for start, end in spans:
            # Spot serves one-second klines; the futures endpoint does not
            # advertise them. Ask, and drop to minutes when nothing comes back,
            # rather than assuming either way and silently fetching nothing.
            dense = await _fetch_range(
                session, venue, "1s", start, end, base=host, market=market
            )
            if not dense:
                dense = await _fetch_range(
                    session, venue, "1m", start, end, base=host, market=market
                )
            for k in dense:
                rows.append(_row(venue, k))
            await asyncio.sleep(_PACE_S)

        if lookback_days > 0:
            # Fetch a little past the requested lookback. Asking for exactly
            # three days leaves the outermost measurement sitting on the first
            # candle, where the feed correctly refuses to extrapolate and the
            # column reads "no data" for a window that was in fact fetched.
            back_start = int(stamps[0] - (lookback_days * 86_400_000 + 3_600_000))
            back_end = int(stamps[-1])
            for k in await _fetch_range(
                session, venue, "1m", back_start, back_end,
                base=host, market=market,
            ):
                rows.append(_row(venue, k))

        if rows:
            with path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(rows) + "\n")
        written[venue] = len(rows)
        if progress is not None:
            progress(venue, len(rows))

    return written
