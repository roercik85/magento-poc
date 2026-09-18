"""Executor interface."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional

from ..models import Fill, Side


@dataclass(slots=True)
class OrderRequest:
    symbol: str
    side: Side
    quote_notional: Optional[float] = None   # for buys: spend this much quote
    qty: Optional[float] = None              # for sells: unload this many base
    signal_wall_ms: float = 0.0              # when the signal was observed
    reference_price: Optional[float] = None  # price at signal time, if known


class OrderRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Executor(abc.ABC):
    """Anything that can turn an OrderRequest into a Fill."""

    simulated: bool = True

    @abc.abstractmethod
    async def submit(self, req: OrderRequest) -> Fill:
        """Execute ``req`` or raise :class:`OrderRejected`."""

    async def start(self) -> None:
        """Warm up connections, sync clocks, preload symbol metadata."""

    async def close(self) -> None:
        ...
