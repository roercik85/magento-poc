"""Live KuCoin spot executor.

Measured on live books (833 tradable USDT pairs, September 2026), a 5-10 USDT
market order on a low-cap pair costs around 11-12 bps one-way in calm
conditions, against a 0.1% taker fee — so roughly **0.4% round trip**. That is
cheap enough that small position sizes are not penalised, which is what makes a
10 USDT live test meaningful at all.

It is also *calm-book* data. During the pump you are trying to trade, everyone
sweeps at once: the spread widens, the near depth evaporates, and realised
slippage is multiples of this. Do not tune the simulator to the calm number.

**Two venue quirks that cost real money if missed:**

1. ``minFunds`` is 0.1 USDT across every USDT pair, so tiny orders are allowed —
   but ``baseIncrement`` rounding on the *sell* side is not forgiving. Round up
   and the order is rejected for insufficient balance, at the exact moment you
   are trying to get out.
2. The order endpoint returns an ``orderId`` and nothing else. The fill price
   has to be fetched separately, so an entry costs two round trips. The order
   is *live* after the first one — the second is bookkeeping, and must never
   gate the exit logic.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from typing import Any, Dict

from ..clock import now_ns, wall_ms
from ..config import LiveConfig
from ..models import Fill, Side
from .base import Executor, OrderRejected, OrderRequest

REST_BASE = "https://api.kucoin.com"


class KucoinLiveExecutor(Executor):
    simulated = False

    def __init__(
        self,
        cfg: LiveConfig,
        rest_base: str = REST_BASE,
        *,
        hard_cap_quote: float,
        symbols=None,                            # noqa: ANN001 - KucoinSymbols
        dry_run: bool = False,
        passphrase_env: str = "PUMPBOT_API_PASSPHRASE",
    ) -> None:
        self._cfg = cfg
        self._base = rest_base.rstrip("/")
        self._hard_cap = hard_cap_quote
        self._dry_run = dry_run
        self._passphrase_env = passphrase_env
        self._session: Any = None
        self._key = ""
        self._secret = b""
        self._passphrase = ""
        self.symbols = symbols

    # -- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        import aiohttp

        from ..marketdata.kucoin import KucoinSymbols

        self._key = os.environ.get(self._cfg.api_key_env, "")
        secret = os.environ.get(self._cfg.api_secret_env, "")
        raw_passphrase = os.environ.get(self._passphrase_env, "")
        missing = [
            name for name, value in (
                (self._cfg.api_key_env, self._key),
                (self._cfg.api_secret_env, secret),
                (self._passphrase_env, raw_passphrase),
            ) if not value
        ]
        if missing:
            raise OrderRejected(f"missing credentials: {', '.join(missing)} not set")

        self._secret = secret.encode()
        # v2 keys send the passphrase HMAC'd with the secret, not in the clear.
        self._passphrase = base64.b64encode(
            hmac.new(self._secret, raw_passphrase.encode(), hashlib.sha256).digest()
        ).decode()

        connector = aiohttp.TCPConnector(
            limit=16, ttl_dns_cache=300, keepalive_timeout=300,
            force_close=False, enable_cleanup_closed=True,
        )
        self._session = aiohttp.ClientSession(connector=connector)

        if self.symbols is None:
            self.symbols = await KucoinSymbols.load(self._session, self._base)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def warm(self) -> None:
        """Keep the pool hot. KuCoin has no dedicated ping; a cheap GET does."""
        try:
            async with self._session.get(f"{self._base}/api/v1/timestamp") as resp:
                await resp.read()
        except Exception:                        # noqa: BLE001 - best effort
            pass

    # -- auth -----------------------------------------------------------
    def _headers(self, method: str, endpoint: str, body: str) -> Dict[str, str]:
        timestamp = str(int(wall_ms()))
        payload = f"{timestamp}{method}{endpoint}{body}"
        signature = base64.b64encode(
            hmac.new(self._secret, payload.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "KC-API-KEY": self._key,
            "KC-API-SIGN": signature,
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-PASSPHRASE": self._passphrase,
            "KC-API-KEY-VERSION": "2",
            "Content-Type": "application/json",
        }

    # -- hot path -------------------------------------------------------
    async def submit(self, req: OrderRequest) -> Fill:
        t_start = now_ns()

        venue_symbol = self.symbols.resolve(req.symbol) if self.symbols else None
        if venue_symbol is None:
            raise OrderRejected(f"{req.symbol} is not listed on this venue")

        body: Dict[str, Any] = {
            "clientOid": uuid.uuid4().hex,
            "symbol": venue_symbol,
            "type": "market",
            "side": req.side.value.lower(),
        }

        if req.side is Side.BUY:
            notional = req.quote_notional or 0.0
            if notional <= 0:
                raise OrderRejected("buy order without quote notional")
            if notional > self._hard_cap:
                raise OrderRejected(
                    f"notional {notional} exceeds hard cap {self._hard_cap}"
                )
            min_funds = self.symbols.min_funds(venue_symbol)
            if min_funds and notional < min_funds:
                raise OrderRejected(
                    f"notional {notional} below {venue_symbol} minimum {min_funds}"
                )
            body["funds"] = f"{notional:.8f}".rstrip("0").rstrip(".")
        else:
            qty = self.symbols.round_size(venue_symbol, req.qty or 0.0)
            if qty <= 0:
                raise OrderRejected("sell quantity rounds to zero at this increment")
            body["size"] = f"{qty:.12f}".rstrip("0").rstrip(".")

        encoded = json.dumps(body, separators=(",", ":"))
        endpoint = "/api/v1/orders"

        if self._dry_run:
            raise OrderRejected(f"dry_run: would send {encoded}")

        async with self._session.post(
            f"{self._base}{endpoint}",
            data=encoded,
            headers=self._headers("POST", endpoint, encoded),
        ) as resp:
            raw = await resp.read()
            if resp.status != 200:
                raise OrderRejected(f"HTTP {resp.status}: {raw[:200]!r}")
            payload = json.loads(raw)

        if payload.get("code") != "200000":
            raise OrderRejected(f"venue error {payload.get('code')}: {payload.get('msg')}")

        order_id = (payload.get("data") or {}).get("orderId")
        if not order_id:
            raise OrderRejected(f"no orderId in response: {payload}")

        submit_latency_ms = (now_ns() - t_start) / 1e6
        return await self._resolve_fill(order_id, req.side, submit_latency_ms)

    async def _resolve_fill(self, order_id: str, side: Side, submit_ms: float) -> Fill:
        """Fetch the executed price. The order is already live at this point.

        A market order on a thin pair can take a moment to report as fully
        dealt, so this retries briefly. It never blocks entry: by the time we
        are here the position exists, and this call only decides what we write
        in the log.
        """
        import asyncio

        endpoint = f"/api/v1/orders/{order_id}"
        data: Dict[str, Any] = {}
        for attempt in range(5):
            async with self._session.get(
                f"{self._base}{endpoint}", headers=self._headers("GET", endpoint, "")
            ) as resp:
                payload = json.loads(await resp.read())
            data = payload.get("data") or {}
            if float(data.get("dealSize") or 0) > 0 and not data.get("isActive"):
                break
            await asyncio.sleep(0.05 * (attempt + 1))

        deal_size = float(data.get("dealSize") or 0.0)
        deal_funds = float(data.get("dealFunds") or 0.0)
        fee = float(data.get("fee") or 0.0)

        if deal_size <= 0:
            raise OrderRejected(
                f"order {order_id} filled nothing "
                f"(cancelled: {data.get('cancelExist')}, active: {data.get('isActive')})"
            )

        return Fill(
            side=side,
            qty=deal_size,
            price=deal_funds / deal_size,
            fee_quote=fee,
            wall_ms=wall_ms(),
            latency_ms=submit_ms,
            simulated=False,
        )

    # -- account --------------------------------------------------------
    async def balance(self, currency: str = "USDT") -> float:
        endpoint = f"/api/v1/accounts?currency={currency}&type=trade"
        async with self._session.get(
            f"{self._base}{endpoint}", headers=self._headers("GET", endpoint, "")
        ) as resp:
            payload = json.loads(await resp.read())
        return sum(float(a.get("available") or 0.0) for a in payload.get("data", []))
