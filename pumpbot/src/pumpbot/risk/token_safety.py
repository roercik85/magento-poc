"""Pre-trade safety for on-chain tokens, on whichever chain they live.

On a centralised venue the exchange has already decided the instrument is real
and that you will be able to sell it. On a DEX nobody has decided anything: the
token may be an impersonator, the pool may be manufactured, the contract may
allow buying and refuse selling, and the tax may be 90%.

The check that catches most of this costs nothing and takes one round trip:
**quote the buy, then quote selling back exactly what the buy would give you.**

* No buy route — not tradable, whatever a dashboard claims.
* Buy routes but sell does not — a honeypot. This is the one that takes the
  whole position, and it is invisible in liquidity, volume and holder counts.
* Both route, but the round trip loses 40% — transfer tax, or depth so thin
  that your own order is the market.

That one measurement subsumes tax detection, depth estimation and honeypot
detection, and it is measured at *your* size rather than in the abstract.

Two aggregators are needed because none spans both worlds: Jupiter for Solana,
LI.FI for EVM chains. Both are keyless.

**A chain with no public aggregator is reported as unquotable, never as
untradable.** HEDGE's largest market is on Robinhood Chain with $260k of daily
volume and nothing here can price it. Conflating "I cannot reach this router"
with "this has no market" is how the first version of this file reported
tradable tokens as dead.

None of it is a guarantee: a contract can be upgraded, selling can be enabled
only for some addresses, and a quote is not a fill. It is the cheapest large
filter available.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..marketdata.chains import Chain, describe_unquotable, get_chain

JUPITER = "https://lite-api.jup.ag/swap/v1"
LIFI = "https://li.quest/v1"

# LI.FI wants a from-address to build a route. Nothing is signed or sent, so
# any well-formed address works; this one is the canonical burn address.
LIFI_PROBE_ADDRESS = "0x0000000000000000000000000000000000000001"


@dataclass(slots=True)
class RoundTrip:
    """What buying and immediately selling back would cost."""

    bought: bool = False
    sold: bool = False
    tokens_out: int = 0
    usd_back: Optional[float] = None
    round_trip_pct: Optional[float] = None
    buy_impact_pct: Optional[float] = None
    sell_impact_pct: Optional[float] = None
    error: str = ""


class Quoter(abc.ABC):
    """Prices a round trip on one family of chains."""

    @abc.abstractmethod
    async def round_trip(self, chain: Chain, mint: str, usd: float,
                         slippage_bps: int) -> RoundTrip:
        ...


class JupiterQuoter(Quoter):
    def __init__(self, session, base: str = JUPITER) -> None:  # noqa: ANN001
        self._session = session
        self._base = base.rstrip("/")

    async def quote(self, input_mint: str, output_mint: str, amount: int,
                    slippage_bps: int) -> Dict[str, Any]:
        params = {
            "inputMint": input_mint, "outputMint": output_mint,
            "amount": str(amount), "slippageBps": str(slippage_bps),
        }
        try:
            async with self._session.get(f"{self._base}/quote", params=params) as resp:
                payload = await resp.json()
        except Exception as exc:                 # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        if "outAmount" not in payload:
            return {"error": payload.get("error") or payload.get("errorCode")
                    or "no route"}
        return payload

    async def round_trip(self, chain: Chain, mint: str, usd: float,
                         slippage_bps: int) -> RoundTrip:
        rt = RoundTrip()
        amount_in = int(round(usd * 10 ** chain.usdc_decimals))

        buy = await self.quote(chain.usdc, mint, amount_in, slippage_bps)
        if "error" in buy:
            rt.error = str(buy["error"])
            return rt
        rt.bought = True
        rt.buy_impact_pct = _pct(buy.get("priceImpactPct"))
        rt.tokens_out = int(buy["outAmount"])
        if rt.tokens_out <= 0:
            rt.error = "buy quote returns zero tokens"
            return rt

        sell = await self.quote(mint, chain.usdc, rt.tokens_out, slippage_bps)
        if "error" in sell:
            rt.error = str(sell["error"])
            return rt
        rt.sold = True
        rt.sell_impact_pct = _pct(sell.get("priceImpactPct"))
        rt.usd_back = int(sell["outAmount"]) / 10 ** chain.usdc_decimals
        rt.round_trip_pct = 100.0 * (rt.usd_back - usd) / usd
        return rt


class LifiQuoter(Quoter):
    """EVM chains. Token decimals come from the aggregator rather than being
    assumed — USDC is 6 decimals on most chains and 18 on BNB Chain, and
    assuming wrongly produces a quote for a millionth of the intended size,
    which routes perfectly and means nothing."""

    def __init__(self, session, base: str = LIFI,
                 probe_address: str = LIFI_PROBE_ADDRESS) -> None:  # noqa: ANN001
        self._session = session
        self._base = base.rstrip("/")
        self._probe = probe_address
        self._decimals: Dict[Tuple[int, str], int] = {}

    async def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            async with self._session.get(f"{self._base}/{path}", params=params) as resp:
                return await resp.json()
        except Exception as exc:                 # noqa: BLE001
            return {"message": f"{type(exc).__name__}: {exc}"}

    async def decimals(self, chain_id: int, token: str) -> Optional[int]:
        key = (chain_id, token.lower())
        if key in self._decimals:
            return self._decimals[key]
        payload = await self._get("token", {"chain": chain_id, "token": token})
        value = payload.get("decimals")
        if value is None:
            return None
        self._decimals[key] = int(value)
        return int(value)

    async def quote(self, chain_id: int, frm: str, to: str,
                    amount: int) -> Dict[str, Any]:
        payload = await self._get("quote", {
            "fromChain": chain_id, "toChain": chain_id,
            "fromToken": frm, "toToken": to, "fromAmount": str(amount),
            "fromAddress": self._probe,
        })
        estimate = payload.get("estimate") or {}
        if not estimate.get("toAmount"):
            return {"error": payload.get("message") or "no route"}
        return estimate

    async def round_trip(self, chain: Chain, mint: str, usd: float,
                         slippage_bps: int) -> RoundTrip:
        rt = RoundTrip()
        chain_id = chain.evm_chain_id
        assert chain_id is not None

        usdc_decimals = await self.decimals(chain_id, chain.usdc)
        if usdc_decimals is None:
            rt.error = "aggregator does not know this chain's USDC"
            return rt
        if await self.decimals(chain_id, mint) is None:
            rt.error = "token unknown to the aggregator"
            return rt

        amount_in = int(round(usd * 10 ** usdc_decimals))
        buy = await self.quote(chain_id, chain.usdc, mint, amount_in)
        if "error" in buy:
            rt.error = str(buy["error"])
            return rt
        rt.bought = True
        rt.tokens_out = int(buy["toAmount"])
        if rt.tokens_out <= 0:
            rt.error = "buy quote returns zero tokens"
            return rt

        sell = await self.quote(chain_id, mint, chain.usdc, rt.tokens_out)
        if "error" in sell:
            rt.error = str(sell["error"])
            return rt
        rt.sold = True
        rt.usd_back = int(sell["toAmount"]) / 10 ** usdc_decimals
        rt.round_trip_pct = 100.0 * (rt.usd_back - usd) / usd
        return rt


# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SafetyVerdict:
    mint: str
    symbol: str
    chain: str
    quotable: bool
    tradable: bool
    sellable: bool
    round_trip_pct: Optional[float]
    buy_impact_pct: Optional[float]
    sell_impact_pct: Optional[float]
    liquidity_usd: float = 0.0
    volume_h24: float = 0.0
    txns_h24: int = 0
    age_hours: Optional[float] = None
    failures: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        return self.quotable and not self.failures

    @property
    def verdict(self) -> str:
        if not self.quotable:
            return f"UNQUOTABLE — {describe_unquotable(self.chain)}"
        if not self.tradable:
            return "UNTRADABLE — no route exists to buy this at all."
        if not self.sellable:
            return (
                "HONEYPOT — it can be bought and not sold. This takes the whole "
                "position, and nothing in liquidity or holder counts shows it."
            )
        if self.failures:
            return "REJECT — " + "; ".join(self.failures)
        return f"OK — round trip costs {abs(self.round_trip_pct or 0):.1f}% here."


@dataclass
class SafetyLimits:
    """Thresholds, all of which a caller can loosen and should not.

    The defaults sit where a 2 USDT position stops being a trade and starts
    being a donation.
    """

    min_liquidity_usd: float = 15_000.0
    min_volume_h24: float = 10_000.0
    min_txns_h24: int = 100
    min_age_hours: float = 1.0
    max_round_trip_loss_pct: float = 8.0
    max_buy_impact_pct: float = 3.0


class TokenSafetyChecker:
    def __init__(self, session, *, jupiter: str = JUPITER,
                 lifi: str = LIFI) -> None:      # noqa: ANN001
        self._solana = JupiterQuoter(session, jupiter)
        self._evm = LifiQuoter(session, lifi)

    def quoter_for(self, chain: Optional[Chain]) -> Optional[Quoter]:
        if chain is None or not chain.quotable:
            return None
        return self._solana if chain.kind == "solana" else self._evm

    async def check(
        self,
        mint: str,
        *,
        chain: str = "solana",
        symbol: str = "",
        notional_usd: float = 10.0,
        limits: Optional[SafetyLimits] = None,
        pair=None,                               # noqa: ANN001 - TokenPair
        slippage_bps: int = 300,
    ) -> SafetyVerdict:
        limits = limits or SafetyLimits()
        resolved = get_chain(chain)
        quoter = self.quoter_for(resolved)

        verdict = SafetyVerdict(
            mint=mint, symbol=symbol or "", chain=chain,
            quotable=quoter is not None, tradable=False, sellable=False,
            round_trip_pct=None, buy_impact_pct=None, sell_impact_pct=None,
        )
        if pair is not None:
            verdict.liquidity_usd = pair.liquidity_usd
            verdict.volume_h24 = pair.volume_h24
            verdict.txns_h24 = pair.txns_h24
            verdict.age_hours = pair.age_hours

        if quoter is None or resolved is None:
            return verdict

        rt = await quoter.round_trip(resolved, mint, notional_usd, slippage_bps)
        verdict.tradable = rt.bought
        verdict.sellable = rt.sold
        verdict.buy_impact_pct = rt.buy_impact_pct
        verdict.sell_impact_pct = rt.sell_impact_pct
        verdict.round_trip_pct = rt.round_trip_pct

        if not rt.bought:
            verdict.failures.append(f"no buy route ({rt.error})")
            return verdict
        if not rt.sold:
            verdict.failures.append(f"cannot sell back ({rt.error})")
            return verdict

        loss = -(rt.round_trip_pct or 0.0)
        if loss > limits.max_round_trip_loss_pct:
            verdict.failures.append(
                f"round trip costs {loss:.1f}% "
                f"(limit {limits.max_round_trip_loss_pct:.1f}%)"
            )
        if (rt.buy_impact_pct or 0.0) > limits.max_buy_impact_pct:
            verdict.failures.append(
                f"buy moves the price {rt.buy_impact_pct:.1f}% "
                f"(limit {limits.max_buy_impact_pct:.1f}%)"
            )

        if pair is not None:
            if pair.liquidity_usd < limits.min_liquidity_usd:
                verdict.failures.append(
                    f"liquidity ${pair.liquidity_usd:,.0f} below "
                    f"${limits.min_liquidity_usd:,.0f}"
                )
            if pair.volume_h24 < limits.min_volume_h24:
                verdict.failures.append(
                    f"24h volume ${pair.volume_h24:,.0f} below "
                    f"${limits.min_volume_h24:,.0f}"
                )
            if pair.txns_h24 < limits.min_txns_h24:
                verdict.failures.append(
                    f"only {pair.txns_h24} trades in 24h — there may be nobody "
                    f"to sell to"
                )
            age = pair.age_hours
            if age is not None and age < limits.min_age_hours:
                verdict.failures.append(f"pool is {age * 60:.0f} minutes old")
            if pair.looks_inactive and pair.liquidity_usd > 100_000:
                verdict.notes.append(
                    "large pool with almost no trading — manufactured depth"
                )

        return verdict


def _pct(value: Any) -> Optional[float]:
    try:
        return float(value or 0.0) * 100.0
    except (TypeError, ValueError):
        return None
