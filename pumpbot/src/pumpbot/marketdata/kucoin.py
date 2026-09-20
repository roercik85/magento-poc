"""KuCoin market data: symbol rules, live tick feed, and a reactive recorder.

**Why a recorder exists at all.** The scoring pipeline needs to know what a
price did in the 30 seconds after a call. KuCoin's public REST serves klines no
finer than one minute, and ``/market/histories`` returns only the last ~100
trades — about half a minute on a liquid pair, less on a dead one. There is no
historical sub-minute endpoint. So unlike the Binance path, **you cannot
backfill prices after the fact**: if you did not record the market data while
the call was live, the call is unscoreable forever.

:class:`KucoinTickRecorder` closes that. It subscribes reactively — when a
signal names a symbol, it subscribes to that symbol's trade stream within
milliseconds and records for a fixed window, then drops it. You end up with
tick data for exactly the symbols that were called and nothing else, in the
format :class:`~pumpbot.marketdata.historical.HistoricalFeed` already reads.

Run this alongside ``record`` from day one. Weeks of Telegram messages with no
matching market data cannot be scored, and you will not find out until you try.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

REST_BASE = "https://api.kucoin.com"


# ---------------------------------------------------------------------------
# symbols
# ---------------------------------------------------------------------------
class KucoinSymbols:
    """Symbol rules, and the translation the rest of the system needs.

    The parser emits venue-neutral tickers like ``PEPEUSDT``; KuCoin wants
    ``PEPE-USDT``. Getting this wrong does not raise — it produces "symbol not
    found" on every single order, which looks exactly like a connectivity
    problem and wastes an evening.
    """

    def __init__(self) -> None:
        self._rules: Dict[str, Dict[str, Any]] = {}      # "PEPE-USDT" -> rule
        self._compact: Dict[str, str] = {}               # "PEPEUSDT"  -> "PEPE-USDT"

    @classmethod
    async def load(cls, session, rest_base: str = REST_BASE) -> "KucoinSymbols":  # noqa: ANN001
        async with session.get(f"{rest_base}/api/v1/symbols") as resp:
            payload = await resp.json()
        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "KucoinSymbols":
        self = cls()
        for r in payload.get("data", []):
            if not r.get("enableTrading"):
                continue
            symbol = r["symbol"]
            self._rules[symbol] = {
                "base": r["baseCurrency"],
                "quote": r["quoteCurrency"],
                "min_funds": float(r.get("minFunds") or 0.0),
                "base_min_size": float(r.get("baseMinSize") or 0.0),
                "base_increment": float(r.get("baseIncrement") or 0.0),
                "price_increment": float(r.get("priceIncrement") or 0.0),
            }
            self._compact[symbol.replace("-", "")] = symbol
        return self

    def resolve(self, symbol: str) -> Optional[str]:
        """Accept ``PEPEUSDT``, ``PEPE-USDT`` or ``PEPE/USDT``; return venue form."""
        if symbol in self._rules:
            return symbol
        compact = symbol.replace("-", "").replace("/", "").upper()
        return self._compact.get(compact)

    def rule(self, symbol: str) -> Optional[Dict[str, Any]]:
        venue = self.resolve(symbol)
        return self._rules.get(venue) if venue else None

    def min_funds(self, symbol: str) -> float:
        rule = self.rule(symbol)
        return rule["min_funds"] if rule else 0.0

    def round_size(self, symbol: str, qty: float) -> float:
        """Round down to the venue's base increment.

        Rounding up gets the order rejected for insufficient balance on a sell,
        which is the worst moment to discover a rounding convention.
        """
        rule = self.rule(symbol)
        if not rule:
            return qty
        inc = rule["base_increment"]
        if inc <= 0:
            return qty
        import math

        return math.floor(qty / inc) * inc

    def __len__(self) -> int:
        return len(self._rules)

    @property
    def venue_symbols(self) -> List[str]:
        return list(self._rules)


# ---------------------------------------------------------------------------
# live feed
# ---------------------------------------------------------------------------
class KucoinTickFeed:
    """In-memory last-trade feed, fed by the websocket reader.

    Implements the ``PriceFeed`` protocol the engine expects. Prices go stale
    fast on a thin pair, and a stale mark during a pump is confidently wrong,
    so lookups past ``max_staleness_ms`` return ``None`` rather than the last
    thing seen.
    """

    def __init__(self, max_staleness_ms: float = 10_000.0) -> None:
        self._last: Dict[str, Tuple[float, float]] = {}      # compact -> (price, t_ms)
        self._depth: Dict[str, float] = {}
        self._max_stale = max_staleness_ms

    @staticmethod
    def _key(symbol: str) -> str:
        return symbol.replace("-", "").replace("/", "").upper()

    def on_trade(self, venue_symbol: str, price: float, size: float, t_ms: float) -> None:
        key = self._key(venue_symbol)
        self._last[key] = (price, t_ms)
        # Rolling notional seen, as a crude liveness/depth proxy. Real book
        # depth would need the level2 feed and its own sequencing logic.
        self._depth[key] = self._depth.get(key, 0.0) * 0.99 + price * size

    def price(self, symbol: str, t_ms: float) -> Optional[float]:
        entry = self._last.get(self._key(symbol))
        if entry is None:
            return None
        price, stamped = entry
        if t_ms - stamped > self._max_stale:
            return None
        return price

    def depth_quote(self, symbol: str) -> float:
        return self._depth.get(self._key(symbol), 0.0)

    def seen(self, symbol: str) -> bool:
        return self._key(symbol) in self._last


# ---------------------------------------------------------------------------
# websocket
# ---------------------------------------------------------------------------
class KucoinWebsocket:
    """Minimal KuCoin public websocket client.

    KuCoin hands out a short-lived token over REST, then expects a ping on the
    interval it names. Miss the ping window and the server drops you silently —
    the socket stays open and simply stops delivering, which during a pump
    looks like "the market went quiet".
    """

    def __init__(self, rest_base: str = REST_BASE) -> None:
        self._rest_base = rest_base.rstrip("/")
        self._ws: Any = None
        self._session: Any = None
        self._ping_interval_s = 18.0
        self._sub_id = 0
        self.subscribed: Set[str] = set()
        self.connected = False

    async def connect(self, session) -> None:  # noqa: ANN001
        self._session = session
        async with session.post(f"{self._rest_base}/api/v1/bullet-public") as resp:
            payload = await resp.json()
        data = payload["data"]
        server = data["instanceServers"][0]
        self._ping_interval_s = float(server["pingInterval"]) / 1000.0 * 0.8
        url = f"{server['endpoint']}?token={data['token']}&connectId={uuid.uuid4().hex}"
        self._ws = await session.ws_connect(url, heartbeat=None)
        self.connected = True

    async def subscribe_trades(self, venue_symbols: List[str]) -> None:
        """Subscribe to ``/market/match`` for the given symbols.

        KuCoin accepts a comma-separated list, capped well below the 100-topic
        limit per connection — batching matters because each subscribe is a
        round trip and a pump does not wait.
        """
        new = [s for s in venue_symbols if s not in self.subscribed]
        if not new or self._ws is None:
            return
        for i in range(0, len(new), 50):
            chunk = new[i:i + 50]
            self._sub_id += 1
            await self._ws.send_json({
                "id": str(self._sub_id),
                "type": "subscribe",
                "topic": "/market/match:" + ",".join(chunk),
                "privateChannel": False,
                "response": False,
            })
            self.subscribed.update(chunk)

    async def unsubscribe_trades(self, venue_symbols: List[str]) -> None:
        live = [s for s in venue_symbols if s in self.subscribed]
        if not live or self._ws is None:
            return
        self._sub_id += 1
        await self._ws.send_json({
            "id": str(self._sub_id),
            "type": "unsubscribe",
            "topic": "/market/match:" + ",".join(live),
            "privateChannel": False,
            "response": False,
        })
        self.subscribed.difference_update(live)

    async def ping_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set() and self._ws is not None:
            self._sub_id += 1
            try:
                await self._ws.send_json({"id": str(self._sub_id), "type": "ping"})
            except Exception:                    # noqa: BLE001 - reconnect handles it
                self.connected = False
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._ping_interval_s)
            except asyncio.TimeoutError:
                pass

    async def read(self, on_trade: Callable[[str, float, float, float], None],
                   stop: asyncio.Event) -> None:
        """Dispatch trade messages until the socket closes or ``stop`` is set."""
        import aiohttp

        if self._ws is None:
            return
        async for msg in self._ws:
            if stop.is_set():
                break
            if msg.type is not aiohttp.WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(msg.data)
            except (ValueError, TypeError):
                continue
            if payload.get("type") != "message":
                continue
            d = payload.get("data") or {}
            symbol = d.get("symbol")
            price = d.get("price")
            if not symbol or price is None:
                continue
            # KuCoin stamps trades in nanoseconds.
            t_ns = int(d.get("time") or 0)
            t_ms = t_ns / 1e6 if t_ns else time.time() * 1000.0
            on_trade(symbol, float(price), float(d.get("size") or 0.0), t_ms)
        self.connected = False

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        self.connected = False


# ---------------------------------------------------------------------------
# recorder
# ---------------------------------------------------------------------------
class KucoinTickRecorder:
    """Records ticks for symbols as they get called, into a price file.

    Ticks are aggregated into 1-second bars on the way out, matching what
    :class:`~pumpbot.marketdata.historical.HistoricalFeed` reads, so a recorded
    session replays through exactly the same code path as a Binance backfill.
    Raw ticks are kept too when ``keep_raw`` is set — they are the only way to
    ever revisit a sub-second question later, and they cannot be re-obtained.
    """

    def __init__(
        self,
        out_path: str | Path,
        *,
        window_s: int = 900,
        feed: Optional[KucoinTickFeed] = None,
        keep_raw: bool = True,
    ) -> None:
        self.path = Path(out_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_path = self.path.with_suffix(".ticks.jsonl") if keep_raw else None
        self.feed = feed or KucoinTickFeed()
        self._window_s = window_s
        self._until_ms: Dict[str, float] = {}         # venue symbol -> record until
        self._bars: Dict[Tuple[str, int], Dict[str, float]] = {}
        self._raw: List[str] = []
        self.ticks_seen = 0
        self.symbols_recorded: Set[str] = set()

    # -- subscription lifecycle -----------------------------------------
    def want(self, venue_symbol: str, now_ms: float) -> None:
        """Mark a symbol as interesting; extends the window if already live."""
        self._until_ms[venue_symbol] = now_ms + self._window_s * 1000.0
        self.symbols_recorded.add(venue_symbol)

    def expired(self, now_ms: float) -> List[str]:
        return [s for s, until in self._until_ms.items() if now_ms > until]

    def drop(self, venue_symbols: List[str]) -> None:
        for s in venue_symbols:
            self._until_ms.pop(s, None)

    @property
    def active(self) -> List[str]:
        return list(self._until_ms)

    # -- ingestion -------------------------------------------------------
    def on_trade(self, venue_symbol: str, price: float, size: float, t_ms: float) -> None:
        self.ticks_seen += 1
        self.feed.on_trade(venue_symbol, price, size, t_ms)

        if self.raw_path is not None:
            self._raw.append(json.dumps({
                "symbol": venue_symbol, "t_ms": t_ms, "price": price, "size": size,
            }))

        second = int(t_ms // 1000)
        key = (venue_symbol, second)
        bar = self._bars.get(key)
        if bar is None:
            self._bars[key] = {
                "open": price, "high": price, "low": price, "close": price,
                "quote_volume": price * size,
            }
        else:
            bar["close"] = price
            if price > bar["high"]:
                bar["high"] = price
            if price < bar["low"]:
                bar["low"] = price
            bar["quote_volume"] += price * size

    # -- output ----------------------------------------------------------
    def flush(self) -> int:
        """Write completed bars and raw ticks. Returns bars written."""
        if not self._bars and not self._raw:
            return 0

        written = 0
        if self._bars:
            with self.path.open("a", encoding="utf-8") as fh:
                for (venue_symbol, second), bar in sorted(self._bars.items(), key=lambda kv: kv[0][1]):
                    fh.write(json.dumps({
                        # Stored in the compact form the engine trades in, so
                        # the price file keys match the parser's symbols.
                        "symbol": venue_symbol.replace("-", ""),
                        "t_ms": second * 1000,
                        "close": bar["close"], "high": bar["high"], "low": bar["low"],
                        "quote_volume": bar["quote_volume"],
                    }) + "\n")
                    written += 1
            self._bars.clear()

        if self.raw_path is not None and self._raw:
            with self.raw_path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(self._raw) + "\n")
            self._raw.clear()

        return written


# ---------------------------------------------------------------------------
# recording session
# ---------------------------------------------------------------------------
class KucoinRecordingSession:
    """Owns the websocket, the recorder and their background tasks.

    Plugs into the live runner as the market-data side of ``record`` mode: the
    runner calls :meth:`note_symbol` the instant a signal parses, and this
    subscribes to that symbol's trades within one round trip. Symbols fall off
    the subscription once their window expires, so a long recording session
    does not accumulate hundreds of dead topics on one connection.

    Reconnects on drop. KuCoin's token is short-lived and the socket dies
    routinely over a multi-week recording run; treating that as fatal would
    lose the recording, and losing the recording loses the calls, which cannot
    be re-obtained at this resolution.
    """

    def __init__(
        self,
        out_path: str | Path,
        symbols: Optional[KucoinSymbols] = None,
        *,
        window_s: int = 900,
        flush_interval_s: float = 5.0,
        rest_base: str = REST_BASE,
        keep_raw: bool = True,
    ) -> None:
        self.symbols = symbols
        self.recorder = KucoinTickRecorder(
            out_path, window_s=window_s, keep_raw=keep_raw
        )
        self.feed = self.recorder.feed
        self._rest_base = rest_base
        self._flush_s = flush_interval_s
        self._ws: Optional[KucoinWebsocket] = None
        self._session: Any = None
        self._stop = asyncio.Event()
        self._tasks: List[asyncio.Task] = []
        self.bars_written = 0
        self.reconnects = 0

    async def start(self, session) -> None:  # noqa: ANN001
        self._session = session
        if self.symbols is None:
            self.symbols = await KucoinSymbols.load(session, self._rest_base)
        self._stop.clear()
        self._tasks.append(asyncio.create_task(self._run(), name="kucoin-ws"))
        self._tasks.append(asyncio.create_task(self._housekeeping(), name="kucoin-flush"))

    def note_symbol(self, symbol: str, now_ms: float) -> Optional[str]:
        """Called when a signal names a symbol. Returns the venue symbol, if listed."""
        if self.symbols is None:
            return None                          # not started yet
        venue = self.symbols.resolve(symbol)
        if venue is None:
            return None
        self.recorder.want(venue, now_ms)
        if self._ws is not None and self._ws.connected:
            asyncio.create_task(self._ws.subscribe_trades([venue]))
        return venue

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            ws = KucoinWebsocket(self._rest_base)
            try:
                await ws.connect(self._session)
                self._ws = ws
                backoff = 1.0
                # Re-subscribe whatever is still inside its window.
                active = self.recorder.active
                if active:
                    await ws.subscribe_trades(active)
                ping = asyncio.create_task(ws.ping_loop(self._stop))
                await ws.read(self.recorder.on_trade, self._stop)
                ping.cancel()
            except Exception:                    # noqa: BLE001 - reconnect and carry on
                pass
            finally:
                await ws.close()
                self._ws = None

            if self._stop.is_set():
                return
            self.reconnects += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                return
            except asyncio.TimeoutError:
                backoff = min(backoff * 2, 30.0)

    async def _housekeeping(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._flush_s)
                break
            except asyncio.TimeoutError:
                pass
            self.bars_written += self.recorder.flush()
            now = time.time() * 1000.0
            stale = self.recorder.expired(now)
            if stale:
                self.recorder.drop(stale)
                if self._ws is not None and self._ws.connected:
                    await self._ws.unsubscribe_trades(stale)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):   # noqa: BLE001
                pass
        self._tasks.clear()
        self.bars_written += self.recorder.flush()
