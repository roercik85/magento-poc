"""Promotion gate: simulation → real money.

The rule you asked for is "ten profitable runs, then we go live". That rule is
implemented here literally, plus three guards that make it mean what you
intended rather than what it says:

* **A run with two trades is not evidence.** ``min_trades_per_run`` stops a
  single lucky fill from counting as a profitable run. Ten runs of one trade
  each is a coin flipped ten times, not a validated strategy.
* **Simulated runs only.** A live run can never count toward the gate; that
  would let the gate be unlocked by the thing it exists to authorise.
* **Config fingerprint.** Each run records a hash of the parameters that affect
  results. Change the strategy and the streak is not transferable — a streak
  earned with a 3% stop tells you nothing about a 12% stop.

The gate does not open anything by itself. It reports its state, and live mode
additionally requires an explicit ``--i-accept-live-trading-risk`` flag and
credentials in the environment. Three independent locks, because the cost of
this one being wrong is your money.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List

from .config import Config, GateConfig


@dataclass
class RunRecord:
    run_id: str
    label: str
    finished_at: str
    mode: str
    trades: int
    net_return_pct: float
    net_pnl_quote: float
    config_fingerprint: str
    counts_toward_gate: bool
    excluded_because: str = ""


@dataclass
class GateStatus:
    unlocked: bool
    qualifying_runs: int
    required: int
    current_streak: int
    fingerprint: str
    reason: str
    recent: List[RunRecord] = field(default_factory=list)


def config_fingerprint(cfg: Config) -> str:
    """Hash the parameters that change results.

    Deliberately excludes cosmetics (run label, reporting formats) and anything
    that cannot alter a simulated outcome.
    """
    payload = {
        "risk": {
            "position_notional_quote": cfg.risk.position_notional_quote,
            "max_concurrent_positions": cfg.risk.max_concurrent_positions,
            "max_trades_per_run": cfg.risk.max_trades_per_run,
            "daily_drawdown_stop_pct": cfg.risk.daily_drawdown_stop_pct,
            "symbol_cooldown_s": cfg.risk.symbol_cooldown_s,
            "min_channel_score": cfg.risk.min_channel_score,
        },
        "strategy": {
            "ladder": [(r.gain_pct, r.fraction) for r in cfg.strategy.take_profit_ladder],
            "stop_loss_pct": cfg.strategy.stop_loss_pct,
            "trailing_stop_pct": cfg.strategy.trailing_stop_pct,
            "max_hold_s": cfg.strategy.max_hold_s,
            "max_entry_chase_pct": cfg.strategy.max_entry_chase_pct,
        },
        "parsing": {"min_confidence": cfg.parsing.min_confidence},
        "simulation": asdict(cfg.execution.simulation),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class PromotionGate:
    def __init__(self, cfg: GateConfig, fingerprint: str) -> None:
        self._cfg = cfg
        self._fingerprint = fingerprint
        self._path = Path(cfg.state_file)
        self._runs: List[RunRecord] = self._load()

    # -- persistence ----------------------------------------------------
    def _load(self) -> List[RunRecord]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return [RunRecord(**r) for r in data.get("runs", [])]

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"runs": [asdict(r) for r in self._runs]}, indent=2),
            encoding="utf-8",
        )

    # -- recording ------------------------------------------------------
    def record_run(
        self,
        *,
        run_id: str,
        label: str,
        finished_at: str,
        mode: str,
        trades: int,
        net_return_pct: float,
        net_pnl_quote: float,
    ) -> RunRecord:
        counts = True
        why = ""
        if mode != "simulate":
            counts, why = False, f"mode was {mode!r}; only simulated runs count"
        elif trades < self._cfg.min_trades_per_run:
            counts, why = False, (
                f"{trades} trades is below the {self._cfg.min_trades_per_run} "
                f"required for a run to be meaningful"
            )
        elif net_return_pct <= self._cfg.min_net_return_pct:
            counts, why = False, (
                f"net return {net_return_pct:.2f}% did not clear "
                f"{self._cfg.min_net_return_pct:.2f}%"
            )

        record = RunRecord(
            run_id=run_id,
            label=label,
            finished_at=finished_at,
            mode=mode,
            trades=trades,
            net_return_pct=net_return_pct,
            net_pnl_quote=net_pnl_quote,
            config_fingerprint=self._fingerprint,
            counts_toward_gate=counts,
            excluded_because=why,
        )
        self._runs.append(record)
        self._save()
        return record

    # -- evaluation -----------------------------------------------------
    def status(self) -> GateStatus:
        same_config = [r for r in self._runs if r.config_fingerprint == self._fingerprint]
        required = self._cfg.required_profitable_runs

        if self._cfg.require_consecutive:
            streak = 0
            for r in reversed(same_config):
                # A run that does not count (too few trades, wrong mode) is
                # skipped rather than treated as a loss: it carries no
                # information either way.
                if not r.counts_toward_gate:
                    if r.mode == "simulate" and r.trades >= self._cfg.min_trades_per_run:
                        break        # it traded enough and still lost: streak over
                    continue
                streak += 1
            qualifying = streak
        else:
            streak = 0
            qualifying = sum(1 for r in same_config if r.counts_toward_gate)

        unlocked = qualifying >= required
        if unlocked:
            reason = (
                f"{qualifying}/{required} qualifying runs on config {self._fingerprint}. "
                "Live mode is permitted — it still requires the explicit risk flag "
                "and credentials in the environment."
            )
        else:
            reason = (
                f"{qualifying}/{required} qualifying runs on config {self._fingerprint}. "
                f"{required - qualifying} more profitable simulated run(s) needed, each "
                f"with at least {self._cfg.min_trades_per_run} trades."
            )

        return GateStatus(
            unlocked=unlocked,
            qualifying_runs=qualifying,
            required=required,
            current_streak=streak,
            fingerprint=self._fingerprint,
            reason=reason,
            recent=same_config[-required:],
        )

    def assert_live_allowed(self, risk_flag_given: bool) -> None:
        st = self.status()
        if not st.unlocked:
            raise PermissionError(f"live trading is gated: {st.reason}")
        if not risk_flag_given:
            raise PermissionError(
                "live trading requires --i-accept-live-trading-risk. "
                "Read LEGAL.md before you pass it."
            )
