"""Historical price feed for scoring and backtesting real recorded corpora.

Without this, replaying a recorded corpus falls back to the synthetic pump feed
— which produces confident numbers that have nothing to do with what those
symbols actually did. That is worse than no backtest, because it looks like one.

The feed reads a price file produced by :func:`fetch_prices`, which pulls 1-second
klines from the venue around each signal in the corpus. One second is the finest
granularity Binance serves over REST; anything shorter needs the trade stream,
which is not available historically at this scale.

**Resolution honesty.** A 1-second bar cannot tell you what you would have been
filled at 300 ms after the post. It bounds it: the scorer therefore measures
returns from the close of the bar containing the post, which is the first price
a follower could plausibly have traded. Where the bar range is wide, the report
flags the symbol as unreliable rather than pretending to a precision the data
does not have.
"""
from __future__ import annotations

import bisect
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


class HistoricalFeed:
    """Sorted-by-time price series per symbol, with as-of lookup.

    ``price(symbol, t_ms)`` returns the last observation at or before ``t_ms``.
    It returns ``None`` — rather than the nearest thing it can find — when the
    gap exceeds ``max_staleness_ms``, because a stale price in a pump is
    confidently wrong, and confidently wrong is how a backtest lies.
    """

    def __init__(self, max_staleness_ms: float = 120_000.0) -> None:
        self._times: Dict[str, List[float]] = {}
        self._prices: Dict[str, List[float]] = {}
        self._ranges: Dict[str, List[float]] = {}     # per-bar high/low spread, bps
        self._depth: Dict[str, float] = {}
        self._max_stale = max_staleness_ms
        self.misses: Set[str] = set()

    # -- loading --------------------------------------------------------
    @classmethod
    def from_file(cls, path: str | Path, **kw) -> "HistoricalFeed":
        """Load a price file written by :func:`fetch_prices`.

        Format is JSONL, one object per line::

            {"symbol": "PEPEUSDT", "t_ms": 1700000000000, "close": 1.23e-6,
             "high": 1.24e-6, "low": 1.22e-6, "quote_volume": 4123.5}
        """
        feed = cls(**kw)
        rows: Dict[str, List[Tuple[float, float, float]]] = defaultdict(list)
        volumes: Dict[str, List[float]] = defaultdict(list)

        with Path(path).open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                symbol = r["symbol"]
                close = float(r["close"])
                high = float(r.get("high", close))
                low = float(r.get("low", close))
                spread_bps = 10_000.0 * (high - low) / close if close > 0 else 0.0
                rows[symbol].append((float(r["t_ms"]), close, spread_bps))
                if "quote_volume" in r:
                    volumes[symbol].append(float(r["quote_volume"]))

        for symbol, series in rows.items():
            series.sort()
            feed._times[symbol] = [t for t, _, _ in series]
            feed._prices[symbol] = [p for _, p, _ in series]
            feed._ranges[symbol] = [s for _, _, s in series]
            vols = volumes.get(symbol)
            if vols:
                # A crude but defensible depth proxy: median per-second quote
                # volume. Real book depth is not available historically, and
                # assuming a fixed depth for every low-cap is worse.
                vols_sorted = sorted(vols)
                feed._depth[symbol] = vols_sorted[len(vols_sorted) // 2]

        return feed

    @property
    def symbols(self) -> List[str]:
        return list(self._times)

    # -- PriceFeed ------------------------------------------------------
    def price(self, symbol: str, t_ms: float) -> Optional[float]:
        times = self._times.get(symbol)
        if not times:
            self.misses.add(symbol)
            return None

        idx = bisect.bisect_right(times, t_ms) - 1
        if idx < 0:
            return None
        if t_ms - times[idx] > self._max_stale:
            return None
        return self._prices[symbol][idx]

    def depth_quote(self, symbol: str) -> float:
        return self._depth.get(symbol, 0.0)

    def bar_spread_bps(self, symbol: str, t_ms: float) -> Optional[float]:
        """How wide the bar containing ``t_ms`` was.

        A wide bar means the 1-second resolution cannot pin down the fill, and
        the observation should be treated as noisy rather than precise.
        """
        times = self._times.get(symbol)
        if not times:
            return None
        idx = bisect.bisect_right(times, t_ms) - 1
        if idx < 0:
            return None
        return self._ranges[symbol][idx]

    def coverage(self, symbols: Iterable[str]) -> Tuple[int, int, List[str]]:
        """``(covered, total, missing)`` — how much of a corpus is priceable."""
        wanted = list(dict.fromkeys(symbols))
        missing = [s for s in wanted if s not in self._times]
        return len(wanted) - len(missing), len(wanted), missing


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------
async def fetch_prices(
    symbols_and_times: Sequence[Tuple[str, float]],
    out_path: str | Path,
    *,
    rest_base: str = "https://api.binance.com",
    window_before_s: int = 600,
    window_after_s: int = 3_600,
    concurrency: int = 4,
    progress=None,                               # noqa: ANN001
) -> Dict[str, int]:
    """Pull 1-second klines around each (symbol, signal time) and write JSONL.

    Binance serves at most 1000 klines per request, so a 1-second interval
    covers ~16 minutes per call and a long window needs several. Requests are
    throttled: a corpus of a few hundred signals will otherwise trip the weight
    limit and start returning 429s, and a partially-fetched price file produces
    a silently truncated backtest.

    Returns ``{symbol: rows_written}``. Symbols the venue does not list — which
    is common, since many pump targets are delisted or never listed — come back
    with zero and are reported rather than skipped silently.
    """
    import asyncio

    import aiohttp

    windows: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
    for symbol, post_ms in symbols_and_times:
        start = int(post_ms - window_before_s * 1000)
        end = int(post_ms + window_after_s * 1000)
        windows[symbol].append((start, end))

    # Merge overlapping windows per symbol so a busy symbol is fetched once.
    merged: Dict[str, List[Tuple[int, int]]] = {}
    for symbol, spans in windows.items():
        spans.sort()
        out: List[Tuple[int, int]] = []
        for start, end in spans:
            if out and start <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], end))
            else:
                out.append((start, end))
        merged[symbol] = out

    written: Dict[str, int] = {s: 0 for s in merged}
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    async def fetch_span(session, symbol: str, start: int, end: int) -> List[dict]:
        rows: List[dict] = []
        cursor = start
        while cursor < end:
            params = {
                "symbol": symbol, "interval": "1s",
                "startTime": cursor, "endTime": end, "limit": 1000,
            }
            async with sem:
                async with session.get(f"{rest_base}/api/v3/klines", params=params) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(float(resp.headers.get("Retry-After", 5)))
                        continue
                    if resp.status != 200:
                        return rows          # unlisted or delisted: caller reports it
                    klines = await resp.json()
            if not klines:
                break
            for k in klines:
                rows.append({
                    "symbol": symbol, "t_ms": int(k[0]),
                    "close": float(k[4]), "high": float(k[2]), "low": float(k[3]),
                    "quote_volume": float(k[7]),
                })
            cursor = int(klines[-1][0]) + 1000
            # Stay well inside the weight budget; a 429 mid-corpus silently
            # truncates the price file and therefore the backtest.
            await asyncio.sleep(0.12)
        return rows

    async def handle(session, symbol: str, spans: List[Tuple[int, int]]) -> None:
        collected: List[dict] = []
        for start, end in spans:
            collected.extend(await fetch_span(session, symbol, start, end))
        async with lock:
            with path.open("a", encoding="utf-8") as fh:
                for row in collected:
                    fh.write(json.dumps(row) + "\n")
            written[symbol] = len(collected)
        if progress is not None:
            progress(symbol, len(collected))

    path.write_text("", encoding="utf-8")        # start clean
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*(handle(session, s, spans) for s, spans in merged.items()))

    return written
