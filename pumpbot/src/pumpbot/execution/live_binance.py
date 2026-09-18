"""Live Binance spot executor, optimised for time-to-first-byte.

Everything that can be computed before the signal arrives, is. On the hot path
we do exactly three things: format a query string, HMAC it, and write it to an
already-open TLS connection.

Latency work that actually pays, roughly in order of effect:

* **Keep the connection hot.** A cold TCP+TLS handshake to Binance is 2 RTTs of
  pure waste. We hold a pool open and ping it so the OS never reaps it.
* **Pre-seed the HMAC.** ``hmac.new(secret, ...)`` re-expands the key every
  call. Constructing one primed object and ``.copy()``-ing it per order removes
  that.
* **Pre-load symbol filters.** ``exchangeInfo`` is a large response and calling
  it on the hot path is indefensible. It is fetched at startup.
* **``quoteOrderQty`` for entries.** Lets the venue compute the base quantity,
  so we never round against ``stepSize`` under time pressure and never get a
  ``LOT_SIZE`` reject at the worst possible moment.
* **Synced clock.** A drifting local clock produces ``recvWindow`` rejects that
  look exactly like latency problems. See :class:`pumpbot.clock.ServerClock`.

Beyond this process, the remaining wins are physical: run in the same region as
the matching engine, and use an account whose API weight is not already spent.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import urllib.parse
from typing import Any, Dict, Optional

from ..clock import ServerClock, now_ns, wall_ms
from ..config import LiveConfig
from ..models import Fill, Side
from .base import Executor, OrderRejected, OrderRequest

try:                                            # pragma: no cover - optional dep
    import orjson as _json

    def _loads(b: bytes) -> Any:
        return _json.loads(b)
except ImportError:                             # pragma: no cover
    import json as _json_std

    def _loads(b: bytes) -> Any:
        return _json_std.loads(b)


class BinanceLiveExecutor(Executor):
    simulated = False

    def __init__(
        self,
        cfg: LiveConfig,
        rest_base: str,
        *,
        hard_cap_quote: float,
        dry_run: bool = False,
    ) -> None:
        self._cfg = cfg
        self._base = rest_base.rstrip("/")
        self._hard_cap = hard_cap_quote
        self._dry_run = dry_run
        self._session: Any = None
        self._clock = ServerClock()
        self._hmac_template: Optional[hmac.HMAC] = None
        self._api_key: str = ""
        self._filters: Dict[str, Dict[str, float]] = {}

    # -- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        import aiohttp

        self._api_key = os.environ.get(self._cfg.api_key_env, "")
        secret = os.environ.get(self._cfg.api_secret_env, "")
        if not self._api_key or not secret:
            raise OrderRejected(
                f"missing credentials: set {self._cfg.api_key_env} and {self._cfg.api_secret_env}"
            )
        # Primed once; .copy() per order avoids re-expanding the key.
        self._hmac_template = hmac.new(secret.encode(), b"", hashlib.sha256)

        connector = aiohttp.TCPConnector(
            limit=16,
            ttl_dns_cache=300,
            keepalive_timeout=300,      # outlive idle gaps between calls
            force_close=False,
            enable_cleanup_closed=True,
        )
        # Orders go out form-encoded (that is what the venue signs), so the
        # session needs no JSON serialiser.
        self._session = aiohttp.ClientSession(
            connector=connector,
            headers={"X-MBX-APIKEY": self._api_key},
        )

        await self._sync_clock()
        await self._load_filters()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def warm(self) -> None:
        """Keep the TLS session alive and the clock fresh. Call periodically."""
        try:
            async with self._session.get(f"{self._base}/api/v3/ping") as resp:
                await resp.read()
        except Exception:                        # noqa: BLE001 - warming is best effort
            pass
        if self._clock.stale():
            await self._sync_clock()

    # -- setup ----------------------------------------------------------
    async def _sync_clock(self) -> None:
        before = wall_ms()
        async with self._session.get(f"{self._base}/api/v3/time") as resp:
            payload = _loads(await resp.read())
        after = wall_ms()
        self._clock.observe(before, float(payload["serverTime"]), after)

    async def _load_filters(self) -> None:
        """Cache LOT_SIZE / MIN_NOTIONAL so the hot path never queries them."""
        async with self._session.get(f"{self._base}/api/v3/exchangeInfo") as resp:
            payload = _loads(await resp.read())
        for sym in payload.get("symbols", []):
            entry: Dict[str, float] = {}
            for f in sym.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    entry["step_size"] = float(f["stepSize"])
                    entry["min_qty"] = float(f["minQty"])
                elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                    entry["min_notional"] = float(f.get("minNotional", 0.0))
            if entry:
                self._filters[sym["symbol"]] = entry

    def knows_symbol(self, symbol: str) -> bool:
        return symbol in self._filters

    # -- hot path -------------------------------------------------------
    def _sign(self, query: str) -> str:
        assert self._hmac_template is not None
        mac = self._hmac_template.copy()
        mac.update(query.encode())
        return mac.hexdigest()

    async def submit(self, req: OrderRequest) -> Fill:
        t_start = now_ns()

        notional = req.quote_notional or 0.0
        if req.side is Side.BUY:
            if notional > self._hard_cap:
                raise OrderRejected(
                    f"notional {notional} exceeds hard cap {self._hard_cap}"
                )
            if notional <= 0:
                raise OrderRejected("buy order without quote notional")

        params = {
            "symbol": req.symbol,
            "side": req.side.value,
            "type": "MARKET",
            "newOrderRespType": "FULL",      # we need the fills to compute VWAP
            "timestamp": str(self._clock.timestamp_ms()),
            "recvWindow": str(self._cfg.recv_window_ms),
        }
        if req.side is Side.BUY:
            # Venue-side sizing: no stepSize rounding on our critical path.
            params["quoteOrderQty"] = f"{notional:.8f}".rstrip("0").rstrip(".")
        else:
            qty = self._round_step(req.symbol, req.qty or 0.0)
            if qty <= 0:
                raise OrderRejected("sell quantity rounds to zero at this stepSize")
            params["quantity"] = f"{qty:.8f}".rstrip("0").rstrip(".")

        query = urllib.parse.urlencode(params)
        body = f"{query}&signature={self._sign(query)}"

        if self._dry_run:
            raise OrderRejected("dry_run: order constructed but not sent")

        async with self._session.post(
            f"{self._base}/api/v3/order",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            raw = await resp.read()
            if resp.status != 200:
                raise OrderRejected(f"HTTP {resp.status}: {raw[:200]!r}")
            payload = _loads(raw)

        latency_ms = (now_ns() - t_start) / 1e6
        return self._to_fill(payload, req.side, latency_ms)

    def _round_step(self, symbol: str, qty: float) -> float:
        step = self._filters.get(symbol, {}).get("step_size", 0.0)
        if step <= 0:
            return qty
        return math.floor(qty / step) * step

    @staticmethod
    def _to_fill(payload: Dict[str, Any], side: Side, latency_ms: float) -> Fill:
        fills = payload.get("fills") or []
        if fills:
            qty = sum(float(f["qty"]) for f in fills)
            gross = sum(float(f["qty"]) * float(f["price"]) for f in fills)
            price = gross / qty if qty else 0.0
            # Commission may be charged in the base asset; convert at fill price.
            fee = 0.0
            for f in fills:
                amount = float(f.get("commission", 0.0))
                asset = f.get("commissionAsset", "")
                fee += amount * price if asset and asset not in ("USDT", "USDC", "BUSD") else amount
        else:
            qty = float(payload.get("executedQty", 0.0))
            gross = float(payload.get("cummulativeQuoteQty", 0.0))
            price = gross / qty if qty else 0.0
            fee = 0.0

        if qty <= 0:
            raise OrderRejected(f"order filled zero quantity: {payload.get('status')}")

        return Fill(
            side=side,
            qty=qty,
            price=price,
            fee_quote=fee,
            wall_ms=wall_ms(),
            latency_ms=latency_ms,
            simulated=False,
        )
