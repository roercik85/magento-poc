"""Exit logic for pump entries.

Entry is the easy half and it gets all the attention. Exit is where the money
is, because the defining property of a pump is that the price comes back. Four
independent exits, evaluated in priority order on every tick:

1. **Stop loss** — the call was wrong, or we were late.
2. **Trailing stop** — armed only after the first profit rung, so a normal
   entry wobble cannot trip it.
3. **Take-profit ladder** — scale out into strength rather than guessing the
   top, because nobody catches the top of a 40-second spike.
4. **Time exit** — the hard backstop. If nothing has happened in
   ``max_hold_s``, the thesis has expired; the trade was "this pumps now", and
   it didn't.

There is also a pre-entry guard, :meth:`entry_allowed`, which refuses to chase.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..config import StrategyConfig
from ..models import ExitReason, Position


@dataclass(slots=True)
class ExitInstruction:
    fraction: float               # of the ORIGINAL position size
    reason: ExitReason
    note: str = ""


class PumpStrategy:
    def __init__(self, cfg: StrategyConfig) -> None:
        self._cfg = cfg
        self._ladder = sorted(cfg.take_profit_ladder, key=lambda r: r.gain_pct)

    # -- entry ----------------------------------------------------------
    def entry_allowed(
        self, reference_price: Optional[float], current_price: float
    ) -> Tuple[bool, str]:
        """Refuse entries where the move already happened without us.

        If the price has run more than ``max_entry_chase_pct`` between the
        message and our fill, the remaining upside is somebody else's exit
        liquidity and we are it.
        """
        if reference_price is None or reference_price <= 0:
            return True, ""
        chase_pct = 100.0 * (current_price - reference_price) / reference_price
        if chase_pct > self._cfg.max_entry_chase_pct:
            return False, (
                f"price already +{chase_pct:.2f}% since the signal "
                f"(limit {self._cfg.max_entry_chase_pct:.2f}%)"
            )
        return True, ""

    # -- exits ----------------------------------------------------------
    def evaluate(self, pos: Position, price: float, now_ms: float) -> List[ExitInstruction]:
        if pos.closed or pos.qty_open <= 0:
            return []

        entry = pos.entry.price
        if entry <= 0:
            return []

        pos.peak_price = max(pos.peak_price, price)
        gain_pct = 100.0 * (price - entry) / entry
        held_s = (now_ms - pos.opened_wall_ms) / 1000.0
        out: List[ExitInstruction] = []

        # 1. Stop loss — whole position, immediately.
        if gain_pct <= -self._cfg.stop_loss_pct:
            return [ExitInstruction(1.0, ExitReason.STOP_LOSS, f"{gain_pct:.2f}%")]

        # 2. Trailing stop, once armed.
        if pos.trailing_armed and pos.peak_price > 0:
            drop_pct = 100.0 * (pos.peak_price - price) / pos.peak_price
            if drop_pct >= self._cfg.trailing_stop_pct:
                return [
                    ExitInstruction(
                        1.0,
                        ExitReason.TRAILING_STOP,
                        f"-{drop_pct:.2f}% off peak, still {gain_pct:+.2f}% on entry",
                    )
                ]

        # 3. Ladder. Rungs are cumulative and fire at most once each.
        while pos.ladder_hit < len(self._ladder):
            rung = self._ladder[pos.ladder_hit]
            if gain_pct < rung.gain_pct:
                break
            pos.ladder_hit += 1
            pos.trailing_armed = True
            out.append(
                ExitInstruction(
                    rung.fraction,
                    ExitReason.TAKE_PROFIT,
                    f"rung {pos.ladder_hit} at +{rung.gain_pct:.1f}% (actual {gain_pct:+.2f}%)",
                )
            )

        if out:
            return out

        # 4. Time exit.
        if held_s >= self._cfg.max_hold_s:
            return [
                ExitInstruction(
                    1.0, ExitReason.TIME_EXIT, f"held {held_s:.0f}s at {gain_pct:+.2f}%"
                )
            ]

        return []
