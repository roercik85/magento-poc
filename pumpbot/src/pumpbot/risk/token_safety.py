"""Pre-trade safety for on-chain tokens.

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
detection, and it is measured at *your* size rather than in the abstract. It is
not a guarantee: a contract can be upgraded, selling can be enabled only for
some addresses, and a quote is not a fill. It is the cheapest large filter
available, and running it is the difference between losing some trades and
losing the account.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

JUPITER = "https://lite-api.jup.ag/swap/v1"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6


@dataclass(slots=True)
class SafetyVerdict:
    mint: str
    symbol: str
    tradable: bool
    sellable: bool
    round_trip_pct: Optional[float]      # negative is a loss
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
        return not self.failures

    @property
    def verdict(self) -> str:
        if not self.tradable:
            return "UNTRADABLE — no route exists to buy this at all."
        if not self.sellable:
            return (
                "HONEYPOT — it can be bought and not sold. This takes the whole "
                "position, and nothing in liquidity or holder counts shows it."
            )
        if self.failures:
            return "REJECT — " + "; ".join(self.failures)
        return (
            f"OK — round trip costs {abs(self.round_trip_pct or 0):.1f}% at this size."
        )


@dataclass
class SafetyLimits:
    """Thresholds, all of which a caller can loosen and should not.

    The defaults are set where a 2 USDT position stops being a trade and starts
    being a donation.
    """

    min_liquidity_usd: float = 15_000.0
    min_volume_h24: float = 10_000.0
    min_txns_h24: int = 100
    min_age_hours: float = 1.0
    # A round trip costing more than this leaves no move large enough to pay
    # for it, at any realistic hit rate.
    max_round_trip_loss_pct: float = 8.0
    max_buy_impact_pct: float = 3.0


class TokenSafetyChecker:
    def __init__(self, session, jupiter: str = JUPITER) -> None:  # noqa: ANN001
        self._session = session
        self._jup = jupiter.rstrip("/")

    async def quote(
        self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 300
    ) -> Dict[str, Any]:
        """A raw aggregator quote. Errors come back as ``{"error": ...}``."""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
        }
        try:
            async with self._session.get(f"{self._jup}/quote", params=params) as resp:
                payload = await resp.json()
        except Exception as exc:                 # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        if "outAmount" not in payload:
            return {"error": payload.get("error") or payload.get("errorCode")
                    or "no route"}
        return payload

    async def check(
        self,
        mint: str,
        *,
        symbol: str = "",
        notional_usd: float = 10.0,
        limits: Optional[SafetyLimits] = None,
        pair=None,                               # noqa: ANN001 - TokenPair
        slippage_bps: int = 300,
    ) -> SafetyVerdict:
        limits = limits or SafetyLimits()
        verdict = SafetyVerdict(
            mint=mint, symbol=symbol or "", tradable=False, sellable=False,
            round_trip_pct=None, buy_impact_pct=None, sell_impact_pct=None,
        )

        if pair is not None:
            verdict.liquidity_usd = pair.liquidity_usd
            verdict.volume_h24 = pair.volume_h24
            verdict.txns_h24 = pair.txns_h24
            verdict.age_hours = pair.age_hours

        amount_in = int(round(notional_usd * 10 ** USDC_DECIMALS))
        buy = await self.quote(USDC, mint, amount_in, slippage_bps)
        if "error" in buy:
            verdict.failures.append(f"no buy route ({buy['error']})")
            return verdict

        verdict.tradable = True
        verdict.buy_impact_pct = _impact(buy)
        tokens_out = int(buy["outAmount"])
        if tokens_out <= 0:
            verdict.failures.append("buy quote returns zero tokens")
            return verdict

        # Sell back exactly what the buy would hand us. This is the honeypot
        # test, and it has to use the real quantity — a smaller probe can route
        # where the full position cannot.
        sell = await self.quote(mint, USDC, tokens_out, slippage_bps)
        if "error" in sell:
            verdict.failures.append(f"cannot sell back ({sell['error']})")
            return verdict

        verdict.sellable = True
        verdict.sell_impact_pct = _impact(sell)
        usd_back = int(sell["outAmount"]) / 10 ** USDC_DECIMALS
        verdict.round_trip_pct = 100.0 * (usd_back - notional_usd) / notional_usd

        # -- thresholds --------------------------------------------------
        loss = -(verdict.round_trip_pct or 0.0)
        if loss > limits.max_round_trip_loss_pct:
            verdict.failures.append(
                f"round trip costs {loss:.1f}% (limit {limits.max_round_trip_loss_pct:.1f}%)"
            )
        if (verdict.buy_impact_pct or 0.0) > limits.max_buy_impact_pct:
            verdict.failures.append(
                f"buy moves the price {verdict.buy_impact_pct:.1f}% "
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
                verdict.failures.append(
                    f"pool is {age * 60:.0f} minutes old"
                )
            if pair.looks_inactive and pair.liquidity_usd > 100_000:
                verdict.notes.append(
                    "large pool with almost no trading — manufactured depth"
                )

        return verdict


def _impact(quote: Dict[str, Any]) -> Optional[float]:
    try:
        return float(quote.get("priceImpactPct") or 0.0) * 100.0
    except (TypeError, ValueError):
        return None
