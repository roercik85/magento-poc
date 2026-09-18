import pytest

from pumpbot.config import ScoringConfig, ScoringWeights
from pumpbot.scoring.channels import ChannelScorer, SignalObservation


def cfg(**kw):
    base = dict(min_signals_for_score=5, primary_horizon_s=60,
                relay_penalty_weight=0.35, weights=ScoringWeights())
    base.update(kw)
    return ScoringConfig(**base)


def obs(chat_id, name, symbol, ret, *, post_ms=0.0, pre_run=5.0, mae=-1.0, cost=1.0):
    return SignalObservation(
        chat_id=chat_id, channel_name=name, symbol=symbol, post_ms=post_ms,
        price_at_post=1.0, returns_pct={60: ret}, mae_pct=mae,
        pre_run_pct=pre_run, cost_pct=cost,
    )


def test_channel_below_the_minimum_is_not_scored():
    s = ChannelScorer(cfg(min_signals_for_score=10))
    for i in range(5):
        s.observe(obs(-1, "thin", f"S{i}", 10.0))
    assert s.score_all() == []


def test_returns_are_scored_net_of_costs():
    s = ChannelScorer(cfg())
    # Gross +0.5% every time, but costs are 1.0%: a losing channel.
    for i in range(10):
        s.observe(obs(-1, "grossly_profitable", f"S{i}", 0.5, cost=1.0))
    scored = s.score_all()[0]
    assert scored.median_return_pct == pytest.approx(-0.5)
    assert scored.hit_rate == 0.0
    assert "AVOID" in scored.verdict


def test_profitable_channel_outranks_a_losing_one():
    s = ChannelScorer(cfg())
    for i in range(10):
        s.observe(obs(-1, "good", f"G{i}", 8.0))
        s.observe(obs(-2, "bad", f"B{i}", -4.0))
    ranked = s.score_all()
    assert ranked[0].name == "good"
    assert ranked[0].composite > ranked[1].composite


def test_relay_is_penalised_against_the_originator():
    """Same symbols, same returns — only the posting time differs."""
    s = ChannelScorer(cfg())
    for i in range(10):
        sym = f"SYM{i}"
        s.observe(obs(-1, "originator", sym, 6.0, post_ms=i * 100_000.0))
        # 200 s behind, every time.
        s.observe(obs(-2, "relay", sym, 6.0, post_ms=i * 100_000.0 + 200_000.0))
    by_name = {c.name: c for c in s.score_all()}
    assert by_name["originator"].originator_score > by_name["relay"].originator_score
    assert by_name["originator"].composite > by_name["relay"].composite
    assert "RELAY" in by_name["relay"].verdict or "AVOID" in by_name["relay"].verdict


def test_heavy_pre_post_run_is_flagged_as_distribution():
    s = ChannelScorer(cfg())
    for i in range(10):
        s.observe(obs(-1, "vip_exit_liquidity", f"S{i}", 2.0, pre_run=60.0))
    scored = s.score_all()[0]
    assert scored.pre_pump_pct == pytest.approx(60.0)
    assert "AVOID" in scored.verdict
    assert "exit" in scored.verdict.lower()


def test_positive_mean_negative_median_is_called_out():
    """The distribution profile: a few huge winners hiding many losers."""
    s = ChannelScorer(cfg())
    returns = [-3.0] * 9 + [200.0]
    for i, r in enumerate(returns):
        s.observe(obs(-1, "outlier_driven", f"S{i}", r, cost=0.0))
    scored = s.score_all()[0]
    assert scored.median_return_pct < 0 < scored.mean_return_pct
    assert "AVOID" in scored.verdict
    assert "outlier" in scored.verdict.lower()


def test_single_caller_symbols_carry_no_lead_information():
    """A channel calling only symbols nobody else touches must not be
    rewarded for it — that measures obscurity, not speed."""
    s = ChannelScorer(cfg())
    for i in range(10):
        s.observe(obs(-1, "solo", f"UNIQUE{i}", 5.0))
    scored = s.score_all()[0]
    # Neutral lead component (0.5) blended with the pre-run component.
    assert 0.3 < scored.originator_score < 0.8


def test_score_map_shape():
    s = ChannelScorer(cfg())
    for i in range(10):
        s.observe(obs(-1, "a", f"S{i}", 5.0))
    m = s.score_map()
    assert set(m) == {-1}
    assert 0.0 <= m[-1] <= 1.0


def test_composite_is_bounded():
    s = ChannelScorer(cfg())
    for i in range(20):
        s.observe(obs(-1, "absurd", f"S{i}", 5000.0, pre_run=0.0))
    assert s.score_all()[0].composite <= 1.0


def test_best_horizon_finds_where_the_money_is():
    """A pump that pays at 15 s and is gone by 15 min must report 15 s."""
    s = ChannelScorer(cfg())
    for i in range(10):
        s.observe(SignalObservation(
            chat_id=-1, channel_name="spiky", symbol=f"S{i}", post_ms=0.0,
            price_at_post=1.0,
            returns_pct={5: 4.0, 15: 12.0, 30: 6.0, 60: 1.0, 900: -20.0},
            mae_pct=-1.0, pre_run_pct=2.0, cost_pct=1.0,
        ))
    scored = s.score_all()[0]
    assert scored.best_horizon_s == 15
    assert scored.best_horizon_return_pct == pytest.approx(11.0)


def test_best_horizon_can_be_the_shortest_one():
    s = ChannelScorer(cfg())
    for i in range(10):
        s.observe(SignalObservation(
            chat_id=-1, channel_name="instant", symbol=f"S{i}", post_ms=0.0,
            price_at_post=1.0,
            returns_pct={5: 9.0, 15: 3.0, 60: -5.0},
            mae_pct=0.0, pre_run_pct=0.0, cost_pct=0.0,
        ))
    assert s.score_all()[0].best_horizon_s == 5
