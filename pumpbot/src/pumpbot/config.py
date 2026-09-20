"""Configuration loading and validation.

Plain dataclasses over YAML. The only rule enforced with real teeth is the one
around live mode: nothing here can set ``mode: live`` without the promotion
gate agreeing (see :mod:`pumpbot.gate`).
"""
from __future__ import annotations

import os
import functools
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Type, TypeVar, get_type_hints

import yaml

T = TypeVar("T")


class ConfigError(ValueError):
    pass


_SUPPORTED_VENUES = {"binance", "kucoin"}


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------
@dataclass
class TelegramSession:
    session_name: str
    api_id: int = 0
    api_hash: str = ""


@dataclass
class ChannelSpec:
    id: int
    name: str = ""
    tier: str = "candidate"          # candidate | trusted | blocked


@dataclass
class TelegramConfig:
    # Prefer the environment over these fields. A config file holding an
    # api_hash is one `git add -A` away from being published, and a leaked
    # api_hash plus a session file is full access to the account.
    api_id: int = 0
    api_hash: str = ""
    api_id_env: str = "PUMPBOT_TG_API_ID"
    api_hash_env: str = "PUMPBOT_TG_API_HASH"
    session_name: str = "state/pumpbot"
    extra_sessions: List[TelegramSession] = field(default_factory=list)
    channels: List[ChannelSpec] = field(default_factory=list)
    watch_edits: bool = True


@dataclass
class ParsingConfig:
    min_confidence: float = 0.55
    ignore_symbols: List[str] = field(default_factory=list)
    quote_assets: List[str] = field(default_factory=lambda: ["USDT"])
    accept_contracts: bool = True


@dataclass
class MarketDataConfig:
    venue: str = "kucoin"
    rest_base: str = "https://api.kucoin.com"
    ws_base: str = ""                 # KuCoin hands out its ws endpoint per session
    # How long to keep recording ticks after a symbol is called.
    record_window_s: int = 900
    return_horizons_s: List[int] = field(
        default_factory=lambda: [5, 15, 30, 60, 300, 900, 3600]
    )


@dataclass
class SimulationConfig:
    latency_ms_median: float = 220.0
    latency_ms_sigma: float = 0.55
    telegram_fanout_ms_median: float = 90.0
    telegram_fanout_ms_sigma: float = 0.6
    slippage_bps_base: float = 35.0
    slippage_impact_coeff: float = 250.0
    assumed_depth_quote: float = 25_000.0
    taker_fee_bps: float = 10.0
    reject_probability: float = 0.04


@dataclass
class LiveConfig:
    api_key_env: str = "PUMPBOT_API_KEY"
    api_secret_env: str = "PUMPBOT_API_SECRET"
    passphrase_env: str = "PUMPBOT_API_PASSPHRASE"   # KuCoin only
    recv_window_ms: int = 5000                        # Binance only
    max_notional_quote_hard_cap: float = 100.0


@dataclass
class ExecutionConfig:
    venue: str = "kucoin"
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    live: LiveConfig = field(default_factory=LiveConfig)


@dataclass
class RiskConfig:
    quote_asset: str = "USDT"
    starting_equity: float = 1000.0
    position_notional_quote: float = 50.0
    max_concurrent_positions: int = 3
    max_trades_per_run: int = 50
    daily_drawdown_stop_pct: float = 10.0
    symbol_cooldown_s: int = 900
    min_channel_score: float = 0.0
    blocked_symbols: List[str] = field(default_factory=list)


@dataclass
class LadderRung:
    gain_pct: float
    fraction: float


def _default_ladder() -> List["LadderRung"]:
    """Scale out into strength rather than guessing the top.

    The defaults have to be a working strategy, not an empty shell: with no
    ladder the trailing stop never arms (it arms on the first rung), so a
    default-configured run silently degrades to stop-loss and time-exit only
    and quietly measures something other than what is documented.
    """
    return [
        LadderRung(gain_pct=4.0, fraction=0.40),
        LadderRung(gain_pct=9.0, fraction=0.35),
        LadderRung(gain_pct=20.0, fraction=0.25),
    ]


@dataclass
class StrategyConfig:
    take_profit_ladder: List[LadderRung] = field(default_factory=_default_ladder)
    stop_loss_pct: float = 6.0
    trailing_stop_pct: float = 5.0
    max_hold_s: int = 180
    max_entry_chase_pct: float = 8.0


@dataclass
class ScoringWeights:
    hit_rate: float = 0.30
    median_return: float = 0.30
    originator: float = 0.20
    consistency: float = 0.20


@dataclass
class ScoringConfig:
    min_signals_for_score: int = 12
    primary_horizon_s: int = 30
    relay_penalty_weight: float = 0.35
    weights: ScoringWeights = field(default_factory=ScoringWeights)


@dataclass
class GateConfig:
    required_profitable_runs: int = 10
    require_consecutive: bool = True
    min_trades_per_run: int = 5
    min_net_return_pct: float = 0.0
    state_file: str = "state/gate.json"


@dataclass
class ReportingConfig:
    output_dir: str = "runs"
    formats: List[str] = field(default_factory=lambda: ["markdown", "json"])


@dataclass
class Config:
    mode: str = "simulate"
    run_label: str = "default"
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    parsing: ParsingConfig = field(default_factory=ParsingConfig)
    marketdata: MarketDataConfig = field(default_factory=MarketDataConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    reporting: ReportingConfig = field(default_factory=ReportingConfig)

    # ---- derived -----------------------------------------------------
    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    def apply_env_overrides(self) -> List[str]:
        """Let the environment supply secrets so no file has to hold them.

        Returns the names of the variables that were used, for the startup
        banner — silently picking up credentials from somewhere the operator
        cannot see is its own kind of bug.
        """
        used: List[str] = []
        tg = self.telegram

        raw_id = os.environ.get(tg.api_id_env, "")
        if raw_id:
            try:
                tg.api_id = int(raw_id)
            except ValueError as exc:
                raise ConfigError(
                    f"{tg.api_id_env} must be an integer, got {raw_id!r}"
                ) from exc
            used.append(tg.api_id_env)

        raw_hash = os.environ.get(tg.api_hash_env, "")
        if raw_hash:
            tg.api_hash = raw_hash
            used.append(tg.api_hash_env)

        return used

    def validate(self) -> None:
        if self.mode not in {"simulate", "record", "live"}:
            raise ConfigError(f"unknown mode: {self.mode!r}")

        w = self.scoring.weights
        total = w.hit_rate + w.median_return + w.originator + w.consistency
        if abs(total - 1.0) > 1e-6:
            raise ConfigError(f"scoring.weights must sum to 1.0, got {total}")

        ladder_total = sum(r.fraction for r in self.strategy.take_profit_ladder)
        if self.strategy.take_profit_ladder and ladder_total > 1.0 + 1e-9:
            raise ConfigError(
                f"strategy.take_profit_ladder fractions sum to {ladder_total}, must be <= 1.0"
            )

        if self.risk.position_notional_quote <= 0:
            raise ConfigError("risk.position_notional_quote must be positive")

        if self.risk.max_concurrent_positions < 1:
            raise ConfigError("risk.max_concurrent_positions must be >= 1")

        for venue in (self.execution.venue, self.marketdata.venue):
            if venue not in _SUPPORTED_VENUES:
                raise ConfigError(
                    f"unsupported venue {venue!r}; supported: "
                    f"{', '.join(sorted(_SUPPORTED_VENUES))}"
                )

        if self.is_live:
            cap = self.execution.live.max_notional_quote_hard_cap
            if self.risk.position_notional_quote > cap:
                raise ConfigError(
                    f"live mode: position_notional_quote "
                    f"({self.risk.position_notional_quote}) exceeds hard cap ({cap})"
                )
            required = [self.execution.live.api_key_env, self.execution.live.api_secret_env]
            if self.execution.venue == "kucoin":
                # KuCoin signs with a third factor. Discovering this at the
                # first order rather than at startup wastes a live session.
                required.append(self.execution.live.passphrase_env)
            for env in required:
                if not os.environ.get(env):
                    raise ConfigError(f"live mode: environment variable {env} is not set")


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
@functools.lru_cache(maxsize=None)
def _hints(cls: type) -> Dict[str, Any]:
    """Resolve annotations to real types.

    ``from __future__ import annotations`` turns every ``f.type`` into a
    string, so ``is_dataclass(f.type)`` would silently be False and nested
    sections would arrive as raw dicts.
    """
    return get_type_hints(cls, globalns=globals())


def _build(cls: Type[T], data: Any) -> T:
    """Recursively construct nested dataclasses from plain dicts.

    Unknown keys are an error rather than a shrug: a silently ignored typo in
    a risk limit is exactly the kind of thing you find out about from your
    account balance.
    """
    if not isinstance(data, dict):
        raise ConfigError(f"expected a mapping for {cls.__name__}, got {type(data).__name__}")

    known = {f.name for f in fields(cls)}             # type: ignore[arg-type]
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown key(s) in {cls.__name__}: {', '.join(sorted(unknown))}")

    hints = _hints(cls)
    kwargs: Dict[str, Any] = {}
    for name in known:
        if name not in data:
            continue
        value = data[name]
        ftype = hints.get(name)
        if is_dataclass(ftype) and isinstance(value, dict):
            kwargs[name] = _build(ftype, value)       # type: ignore[arg-type]
        else:
            kwargs[name] = value

    return cls(**kwargs)                              # type: ignore[call-arg]


_LIST_OF: Dict[str, Type[Any]] = {
    "telegram.extra_sessions": TelegramSession,
    "telegram.channels": ChannelSpec,
    "strategy.take_profit_ladder": LadderRung,
}


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}\n"
            f"Copy config.example.yaml to {path} and fill it in."
        )
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    # Promote the handful of list-of-dataclass fields before generic building.
    for dotted, cls in _LIST_OF.items():
        section, key = dotted.split(".")
        block = data.get(section)
        if isinstance(block, dict) and isinstance(block.get(key), list):
            block[key] = [_build(cls, item) if isinstance(item, dict) else item
                          for item in block[key]]

    cfg = _build(Config, data)
    cfg.apply_env_overrides()
    cfg.validate()
    return cfg
