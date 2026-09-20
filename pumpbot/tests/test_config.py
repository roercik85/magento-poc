import pytest
import yaml

from pumpbot.config import ConfigError, load_config


def write(tmp_path, data):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_missing_file_explains_itself(tmp_path):
    with pytest.raises(ConfigError, match="config.example.yaml"):
        load_config(tmp_path / "nope.yaml")


def test_defaults_load(tmp_path):
    cfg = load_config(write(tmp_path, {"mode": "simulate"}))
    assert cfg.mode == "simulate"
    assert cfg.risk.position_notional_quote == 50.0


def test_nested_sections_are_built_not_left_as_dicts(tmp_path):
    cfg = load_config(write(tmp_path, {"risk": {"position_notional_quote": 25.0}}))
    assert cfg.risk.position_notional_quote == 25.0
    assert not isinstance(cfg.risk, dict)


def test_unknown_key_is_an_error(tmp_path):
    """A silently ignored typo in a risk limit is found out about from the
    account balance."""
    with pytest.raises(ConfigError, match="positon_notional_quote"):
        load_config(write(tmp_path, {"risk": {"positon_notional_quote": 25.0}}))


def test_unknown_top_level_key_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="riskk"):
        load_config(write(tmp_path, {"riskk": {}}))


def test_ladder_is_built_into_dataclasses(tmp_path):
    cfg = load_config(write(tmp_path, {
        "strategy": {"take_profit_ladder": [
            {"gain_pct": 5.0, "fraction": 0.5},
            {"gain_pct": 10.0, "fraction": 0.5},
        ]}
    }))
    assert cfg.strategy.take_profit_ladder[0].gain_pct == 5.0


def test_ladder_over_one_hundred_percent_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="take_profit_ladder"):
        load_config(write(tmp_path, {
            "strategy": {"take_profit_ladder": [
                {"gain_pct": 5.0, "fraction": 0.7},
                {"gain_pct": 10.0, "fraction": 0.7},
            ]}
        }))


def test_scoring_weights_must_sum_to_one(tmp_path):
    with pytest.raises(ConfigError, match="sum to 1.0"):
        load_config(write(tmp_path, {
            "scoring": {"weights": {"hit_rate": 0.9, "median_return": 0.9,
                                    "originator": 0.1, "consistency": 0.1}}
        }))


def test_unknown_mode_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown mode"):
        load_config(write(tmp_path, {"mode": "yolo"}))


def test_channels_are_built(tmp_path):
    cfg = load_config(write(tmp_path, {
        "telegram": {"channels": [{"id": -1001, "name": "x", "tier": "trusted"}]}
    }))
    assert cfg.telegram.channels[0].tier == "trusted"


# --- live guards ----------------------------------------------------------
def test_live_requires_credentials(tmp_path, monkeypatch):
    for var in ("PUMPBOT_API_KEY", "PUMPBOT_API_SECRET", "PUMPBOT_API_PASSPHRASE"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ConfigError, match="PUMPBOT_API_KEY"):
        load_config(write(tmp_path, {"mode": "live"}))


def test_kucoin_live_requires_the_passphrase(tmp_path, monkeypatch):
    """KuCoin signs with a third factor. Finding that out at the first order
    rather than at startup wastes a live session."""
    monkeypatch.setenv("PUMPBOT_API_KEY", "k")
    monkeypatch.setenv("PUMPBOT_API_SECRET", "s")
    monkeypatch.delenv("PUMPBOT_API_PASSPHRASE", raising=False)
    with pytest.raises(ConfigError, match="PUMPBOT_API_PASSPHRASE"):
        load_config(write(tmp_path, {
            "mode": "live",
            "execution": {"venue": "kucoin"},
            "risk": {"position_notional_quote": 5.0},
        }))


def test_binance_live_does_not_require_a_passphrase(tmp_path, monkeypatch):
    monkeypatch.setenv("PUMPBOT_API_KEY", "k")
    monkeypatch.setenv("PUMPBOT_API_SECRET", "s")
    monkeypatch.delenv("PUMPBOT_API_PASSPHRASE", raising=False)
    cfg = load_config(write(tmp_path, {
        "mode": "live",
        "execution": {"venue": "binance"},
        "marketdata": {"venue": "binance", "rest_base": "https://api.binance.com"},
        "risk": {"position_notional_quote": 5.0},
    }))
    assert cfg.execution.venue == "binance"


def test_unsupported_venue_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unsupported venue"):
        load_config(write(tmp_path, {"execution": {"venue": "mtgox"}}))


def test_live_enforces_the_notional_hard_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("PUMPBOT_API_KEY", "k")
    monkeypatch.setenv("PUMPBOT_API_SECRET", "s")
    monkeypatch.setenv("PUMPBOT_API_PASSPHRASE", "p")
    with pytest.raises(ConfigError, match="hard cap"):
        load_config(write(tmp_path, {
            "mode": "live",
            "risk": {"position_notional_quote": 10_000.0},
            "execution": {"live": {"max_notional_quote_hard_cap": 100.0}},
        }))


def test_live_passes_with_credentials_and_a_small_size(tmp_path, monkeypatch):
    monkeypatch.setenv("PUMPBOT_API_KEY", "k")
    monkeypatch.setenv("PUMPBOT_API_SECRET", "s")
    monkeypatch.setenv("PUMPBOT_API_PASSPHRASE", "p")
    cfg = load_config(write(tmp_path, {
        "mode": "live", "risk": {"position_notional_quote": 20.0},
    }))
    assert cfg.is_live


def test_example_config_is_valid():
    """The shipped example must actually load, or the first-run experience is
    a stack trace."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    cfg = load_config(example)
    assert cfg.mode == "simulate"
    assert len(cfg.strategy.take_profit_ladder) == 3


def test_ten_usd_profile_is_valid_and_guarded():
    """The shipped live-test profile must load and keep exposure bounded."""
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parents[1] / "config.10usd.yaml")
    assert cfg.execution.venue == "kucoin"
    assert cfg.risk.starting_equity == 10.0
    # Worst case exposure cannot exceed the account.
    exposure = cfg.risk.position_notional_quote * cfg.risk.max_concurrent_positions
    assert exposure < cfg.risk.starting_equity
    # A config mistake cannot put more than the hard cap into one order.
    assert cfg.risk.position_notional_quote <= cfg.execution.live.max_notional_quote_hard_cap
    assert cfg.execution.live.max_notional_quote_hard_cap < cfg.risk.starting_equity
