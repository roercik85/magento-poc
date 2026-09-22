"""Chain registry and the quoting each one needs.

The first version of this checked Solana only, because the channel being read
said "on sol". It also said "on Base", "on Rh" and "on Arc", and several tokens
reported as having no market turned out to trade fine elsewhere: EVE round
trips at -1.2% on BSC and -2.9% on Base while its Solana pools are empty.
Measuring one chain and reporting the answer as "not tradable" was wrong.

So chains are a registry, and a token is looked for on all of them.

Two kinds of quoting are needed, because no single aggregator spans both
worlds: Jupiter for Solana, LI.FI for EVM chains. Both answer the only
question that matters before sizing a position — what does buying this and
selling it straight back cost — and both are free and keyless, which is why
they are the ones here.

**Some chains have no public aggregator at all.** DexScreener reports markets
on Robinhood Chain and Arc, and HEDGE's largest market by far is on Robinhood
with $260k of daily volume. Nothing here can quote those. That is reported as
*unquotable*, never as untradable: the distinction matters, because one means
"look elsewhere for a router" and the other means "there is no market".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

SOLANA = "solana"


@dataclass(frozen=True, slots=True)
class Chain:
    """How to talk to one chain."""

    name: str                        # DexScreener's chainId
    kind: str                        # "solana" | "evm"
    label: str
    evm_chain_id: Optional[int] = None
    usdc: str = ""
    usdc_decimals: int = 6

    @property
    def quotable(self) -> bool:
        return bool(self.usdc)


# Chains a public, keyless aggregator can price. Adding one means adding its
# USDC address and confirming the aggregator actually routes there.
CHAINS: Dict[str, Chain] = {
    SOLANA: Chain(
        SOLANA, "solana", "Solana",
        usdc="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", usdc_decimals=6,
    ),
    "ethereum": Chain(
        "ethereum", "evm", "Ethereum", evm_chain_id=1,
        usdc="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", usdc_decimals=6,
    ),
    "bsc": Chain(
        "bsc", "evm", "BNB Chain", evm_chain_id=56,
        usdc="0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", usdc_decimals=18,
    ),
    "base": Chain(
        "base", "evm", "Base", evm_chain_id=8453,
        usdc="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", usdc_decimals=6,
    ),
    "arbitrum": Chain(
        "arbitrum", "evm", "Arbitrum", evm_chain_id=42161,
        usdc="0xaf88d065e77c8cC2239327C5EDb3A432268e5831", usdc_decimals=6,
    ),
    "polygon": Chain(
        "polygon", "evm", "Polygon", evm_chain_id=137,
        usdc="0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", usdc_decimals=6,
    ),
    "optimism": Chain(
        "optimism", "evm", "Optimism", evm_chain_id=10,
        usdc="0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", usdc_decimals=6,
    ),
    "avalanche": Chain(
        "avalanche", "evm", "Avalanche", evm_chain_id=43114,
        usdc="0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E", usdc_decimals=6,
    ),
}


def get_chain(name: Optional[str]) -> Optional[Chain]:
    return CHAINS.get((name or "").lower())


def is_quotable(name: Optional[str]) -> bool:
    chain = get_chain(name)
    return chain is not None and chain.quotable


def describe_unquotable(name: Optional[str]) -> str:
    """Why a chain cannot be priced — never that the token is dead."""
    return (
        f"no keyless aggregator routes {name or 'this chain'}; the market may "
        f"be real and is simply unreachable from here"
    )
