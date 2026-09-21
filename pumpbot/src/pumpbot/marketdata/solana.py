"""Solana token resolution and market data.

**The central problem is identity, not price.** Anyone can mint a token with
any name. Searching DexScreener for "HEDGE" returns fourteen different mints
calling themselves HEDGE; searching for "BONK" returns a mint with $249M of
claimed liquidity, $3.99 of daily volume and two transactions in a day — which
is not BONK, and is not liquid, and is designed to look like both.

So resolution here is built the other way round from a CEX. On a CEX the ticker
*is* the instrument. Here the ticker is a claim, and the mint address is the
instrument:

1. If the message carries a contract address, that is the token. No search.
2. Otherwise a ticker is resolved against real trading activity — volume and
   transaction count, never claimed liquidity alone — and the result is
   reported as ambiguous when more than one candidate is plausible.
3. Either way, routability through the aggregator is the ground truth for
   "can I trade this", and it is checked before size is committed.

Claimed liquidity is the number every scam optimises, because it is the number
every dashboard shows. Volume and transaction count are harder to fake and
cost the faker real money, so those are what this trusts.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

DEXSCREENER = "https://api.dexscreener.com"
GECKOTERMINAL = "https://api.geckoterminal.com/api/v2"

# Solana mint addresses are base58 and 32-44 characters. WSOL and USDC are the
# quote assets worth routing through.
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6


@dataclass(slots=True)
class TokenPair:
    """One trading pair as the aggregator sees it."""

    mint: str
    symbol: str
    pair_address: str
    dex: str
    price_usd: float
    liquidity_usd: float
    volume_h24: float
    volume_h1: float
    txns_h24: int
    txns_h1: int
    created_at_ms: Optional[int]
    price_change_h1: float

    @property
    def age_hours(self) -> Optional[float]:
        if self.created_at_ms is None:
            return None
        return (time.time() * 1000.0 - self.created_at_ms) / 3_600_000.0

    @property
    def looks_inactive(self) -> bool:
        """Claimed liquidity with nobody trading against it.

        The signature of manufactured depth: a large pool that no one touches.
        Real interest leaves transactions behind.
        """
        return self.txns_h24 < 10 or self.volume_h24 < 500.0


@dataclass(slots=True)
class Resolution:
    """The outcome of turning a message's token reference into a mint."""

    mint: Optional[str]
    symbol: str
    source: str                      # "contract" | "ticker" | "none"
    best: Optional[TokenPair] = None
    candidates: List[TokenPair] = field(default_factory=list)
    ambiguous: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.mint is not None and not self.ambiguous


def _pair_from(payload: Dict[str, Any]) -> Optional[TokenPair]:
    base = payload.get("baseToken") or {}
    mint = base.get("address")
    if not mint:
        return None
    liq = payload.get("liquidity") or {}
    vol = payload.get("volume") or {}
    txns = payload.get("txns") or {}

    def _tx(window: str) -> int:
        w = txns.get(window) or {}
        return int(w.get("buys") or 0) + int(w.get("sells") or 0)

    try:
        price = float(payload.get("priceUsd") or 0.0)
    except (TypeError, ValueError):
        price = 0.0

    return TokenPair(
        mint=mint,
        symbol=(base.get("symbol") or "").upper(),
        pair_address=payload.get("pairAddress") or "",
        dex=payload.get("dexId") or "",
        price_usd=price,
        liquidity_usd=float(liq.get("usd") or 0.0),
        volume_h24=float(vol.get("h24") or 0.0),
        volume_h1=float(vol.get("h1") or 0.0),
        txns_h24=_tx("h24"),
        txns_h1=_tx("h1"),
        created_at_ms=payload.get("pairCreatedAt"),
        price_change_h1=float((payload.get("priceChange") or {}).get("h1") or 0.0),
    )


class SolanaTokens:
    """Resolves tokens and reads their market data."""

    def __init__(self, session, dexscreener: str = DEXSCREENER) -> None:  # noqa: ANN001
        self._session = session
        self._base = dexscreener.rstrip("/")
        self._cache: Dict[str, List[TokenPair]] = {}

    # -- lookups --------------------------------------------------------
    async def pairs_for_mint(self, mint: str) -> List[TokenPair]:
        cached = self._cache.get(mint)
        if cached is not None:
            return cached
        try:
            async with self._session.get(
                f"{self._base}/latest/dex/tokens/{mint}"
            ) as resp:
                payload = await resp.json()
        except Exception:                        # noqa: BLE001 - treat as unknown
            return []
        pairs = [
            p for p in (_pair_from(x) for x in (payload.get("pairs") or []))
            if p is not None
        ]
        self._cache[mint] = pairs
        return pairs

    async def search_ticker(self, ticker: str) -> List[TokenPair]:
        try:
            async with self._session.get(
                f"{self._base}/latest/dex/search", params={"q": ticker}
            ) as resp:
                payload = await resp.json()
        except Exception:                        # noqa: BLE001
            return []
        out: List[TokenPair] = []
        for raw in payload.get("pairs") or []:
            if raw.get("chainId") != "solana":
                continue
            pair = _pair_from(raw)
            if pair is not None and pair.symbol == ticker.upper():
                out.append(pair)
        return out

    # -- resolution -----------------------------------------------------
    async def resolve(
        self,
        *,
        contract: Optional[str] = None,
        ticker: Optional[str] = None,
        min_volume_h24: float = 5_000.0,
        min_txns_h24: int = 50,
    ) -> Resolution:
        """Turn a message's token reference into a mint that can be traded.

        A contract address is taken as given — it names the instrument
        unambiguously, which is the whole reason these channels post them.

        A bare ticker is resolved only when exactly one candidate shows real
        trading activity. Two plausible candidates means the message did not
        say which token it meant, and guessing between them is how you buy an
        impersonator.
        """
        if contract:
            pairs = await self.pairs_for_mint(contract)
            best = max(pairs, key=lambda p: p.volume_h24, default=None)
            return Resolution(
                mint=contract,
                symbol=best.symbol if best else (ticker or "").upper(),
                source="contract",
                best=best,
                candidates=pairs,
                reason="" if pairs else "no DEX pair found for this mint",
            )

        if not ticker:
            return Resolution(mint=None, symbol="", source="none",
                              reason="neither a contract address nor a ticker")

        candidates = await self.search_ticker(ticker)
        if not candidates:
            return Resolution(mint=None, symbol=ticker.upper(), source="ticker",
                              reason=f"no Solana pair named {ticker}")

        # Rank on activity, not on claimed liquidity: depth is the number a
        # scam inflates, and transactions are the number it cannot.
        active = [
            p for p in candidates
            if p.volume_h24 >= min_volume_h24 and p.txns_h24 >= min_txns_h24
        ]
        distinct_mints = {p.mint for p in active}

        if not active:
            total = len({p.mint for p in candidates})
            return Resolution(
                mint=None, symbol=ticker.upper(), source="ticker",
                candidates=candidates,
                reason=(
                    f"{total} mint(s) use this ticker and none trades enough to "
                    f"identify itself (need ${min_volume_h24:,.0f} 24h volume and "
                    f"{min_txns_h24} transactions)"
                ),
            )

        best = max(active, key=lambda p: p.volume_h24)
        if len(distinct_mints) > 1:
            return Resolution(
                mint=None, symbol=ticker.upper(), source="ticker",
                best=best, candidates=active, ambiguous=True,
                reason=(
                    f"{len(distinct_mints)} different mints trade actively under "
                    f"this ticker; the message did not say which one"
                ),
            )

        return Resolution(mint=best.mint, symbol=ticker.upper(), source="ticker",
                          best=best, candidates=active)

    # -- history --------------------------------------------------------
    async def ohlcv(
        self,
        pair_address: str,
        *,
        minutes: int = 1,
        limit: int = 300,
        before_ts: Optional[int] = None,
        gecko: str = GECKOTERMINAL,
    ) -> List[Tuple[float, float, float, float, float]]:
        """One-minute candles for a pool, as ``(t_ms, open, high, low, close)``.

        GeckoTerminal returns newest first as
        ``[unix_seconds, open, high, low, close, volume]``.
        """
        params: Dict[str, Any] = {"aggregate": minutes, "limit": limit}
        if before_ts is not None:
            params["before_timestamp"] = before_ts
        try:
            async with self._session.get(
                f"{gecko.rstrip('/')}/networks/solana/pools/{pair_address}/ohlcv/minute",
                params=params,
            ) as resp:
                payload = await resp.json()
        except Exception:                        # noqa: BLE001
            return []

        rows = (((payload.get("data") or {}).get("attributes") or {})
                .get("ohlcv_list") or [])
        out = []
        for r in rows:
            try:
                out.append((float(r[0]) * 1000.0, float(r[1]), float(r[2]),
                            float(r[3]), float(r[4])))
            except (TypeError, ValueError, IndexError):
                continue
        out.sort()
        return out
