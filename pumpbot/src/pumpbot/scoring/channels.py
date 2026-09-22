"""Channel credibility and profitability scoring.

This is the part of the system that decides whether any of the rest is worth
running. A pump channel can fail you in four distinct ways, and a naive
"average return after the call" metric conflates all of them:

* **It is wrong.** Calls simply do not move. Caught by *hit rate*.
* **It is unprofitable after costs.** Calls move 0.4% while you pay 0.7% in
  fees and slippage. Caught by *median return*, measured net.
* **It is a relay.** It reposts calls that originated elsewhere, minutes late.
  By the time it posts, the move is done. Caught by *originator score*, which
  combines cross-channel lead time with the pre-post price run.
* **It is a distribution channel.** The organisers accumulated first and the
  post exists to create the exit liquidity. This is the dangerous one, because
  its *average* return can look fine while the median is negative — a few
  enormous winners for the insiders hide a long tail of losers for everyone
  else. Caught by reporting median and MAE, never mean alone, and by
  *pre-post run*.

Nothing here trusts a channel's own claimed track record. Those are marketing.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..config import ScoringConfig
from ..models import ScoredChannel


@dataclass(slots=True)
class SignalObservation:
    """One call, plus what the market did around it.

    ``returns_pct`` is keyed by horizon in seconds and measured from the price
    at post time — i.e. the best case a follower could theoretically get, with
    zero latency. Real fills will be worse, which is the point: if the ideal
    case is unprofitable, the real one cannot be rescued by faster hardware.
    """

    chat_id: int
    channel_name: str
    symbol: str
    post_ms: float
    price_at_post: float
    returns_pct: Dict[int, float] = field(default_factory=dict)
    mae_pct: float = 0.0              # worst drawdown within the primary horizon
    pre_run_pct: float = 0.0          # move over the short window before the post
    # Move over several windows before the post, keyed by seconds. Accumulation
    # ahead of a call is not a five-minute phenomenon: the buying that matters
    # can start days earlier, and a short window reports 0% for it — which
    # reads as "no pre-positioning" when it means "not measured".
    pre_run_windows: Dict[int, float] = field(default_factory=dict)
    cost_pct: float = 0.0             # modelled round-trip cost for this venue


class ChannelScorer:
    def __init__(self, cfg: ScoringConfig) -> None:
        self._cfg = cfg
        self._obs: Dict[int, List[SignalObservation]] = defaultdict(list)
        self._names: Dict[int, str] = {}

    # -- ingestion ------------------------------------------------------
    def observe(self, obs: SignalObservation) -> None:
        self._obs[obs.chat_id].append(obs)
        if obs.channel_name:
            self._names[obs.chat_id] = obs.channel_name

    def extend(self, observations: Iterable[SignalObservation]) -> None:
        for o in observations:
            self.observe(o)

    @property
    def channel_ids(self) -> List[int]:
        return list(self._obs)

    def observations(self, chat_id: int) -> List[SignalObservation]:
        return self._obs[chat_id]

    # -- cross-channel analysis -----------------------------------------
    def _lead_times(self, cluster_window_s: float = 900.0) -> Dict[int, List[float]]:
        """Seconds each channel led (+) or lagged (-) the first caller.

        Calls for the same symbol within ``cluster_window_s`` are treated as
        the same event. Whoever posted first defines t=0. A channel that is
        consistently 200 seconds behind is not generating alpha, it is
        forwarding somebody else's.
        """
        by_symbol: Dict[str, List[Tuple[float, int]]] = defaultdict(list)
        for chat_id, obs_list in self._obs.items():
            for o in obs_list:
                by_symbol[o.symbol].append((o.post_ms, chat_id))

        leads: Dict[int, List[float]] = defaultdict(list)
        for _symbol, entries in by_symbol.items():
            entries.sort()
            cluster: List[Tuple[float, int]] = []
            for post_ms, chat_id in entries:
                if cluster and (post_ms - cluster[0][0]) > cluster_window_s * 1000.0:
                    self._score_cluster(cluster, leads)
                    cluster = []
                cluster.append((post_ms, chat_id))
            if cluster:
                self._score_cluster(cluster, leads)
        return leads

    @staticmethod
    def _score_cluster(
        cluster: Sequence[Tuple[float, int]], leads: Dict[int, List[float]]
    ) -> None:
        # A symbol only one channel ever called carries no lead information;
        # crediting it would reward obscurity rather than speed.
        if len(cluster) < 2:
            return
        first_ms = cluster[0][0]
        for post_ms, chat_id in cluster:
            leads[chat_id].append((first_ms - post_ms) / 1000.0)

    # -- scoring --------------------------------------------------------
    def score_all(self) -> List[ScoredChannel]:
        leads = self._lead_times()
        out: List[ScoredChannel] = []
        for chat_id, obs_list in self._obs.items():
            scored = self._score_channel(chat_id, obs_list, leads.get(chat_id, []))
            if scored is not None:
                out.append(scored)
        out.sort(key=lambda c: -c.composite)
        return out

    def _score_channel(
        self, chat_id: int, obs_list: List[SignalObservation], leads: List[float]
    ) -> Optional[ScoredChannel]:
        h = self._cfg.primary_horizon_s
        usable = [o for o in obs_list if h in o.returns_pct]
        if len(usable) < self._cfg.min_signals_for_score:
            return None

        # Net of modelled costs throughout. A gross-return score is a score of
        # a strategy nobody can trade.
        nets = [o.returns_pct[h] - o.cost_pct for o in usable]

        hit_rate = sum(1 for r in nets if r > 0) / len(nets)
        median_ret = statistics.median(nets)
        mean_ret = statistics.fmean(nets)
        median_mae = statistics.median([o.mae_pct for o in usable])
        pre_run = statistics.median([o.pre_run_pct for o in usable])
        pre_windows = self._median_pre_windows(usable)

        originator = self._originator_score(leads, pre_run)
        consistency = self._consistency(nets)
        best_h, best_ret = self._best_horizon(usable)

        w = self._cfg.weights
        composite = (
            w.hit_rate * hit_rate
            + w.median_return * _squash(median_ret, scale=5.0)
            + w.originator * originator
            + w.consistency * consistency
        )
        composite = max(0.0, min(1.0, composite))

        return ScoredChannel(
            chat_id=chat_id,
            name=self._names.get(chat_id, str(chat_id)),
            signals=len(usable),
            hit_rate=hit_rate,
            median_return_pct=median_ret,
            mean_return_pct=mean_ret,
            median_mae_pct=median_mae,
            originator_score=originator,
            consistency=consistency,
            pre_pump_pct=pre_run,
            pre_run_windows=pre_windows,
            best_horizon_s=best_h,
            best_horizon_return_pct=best_ret,
            composite=composite,
            verdict=self._verdict(hit_rate, median_ret, mean_ret, originator, pre_run),
        )

    @staticmethod
    def _median_pre_windows(usable: List[SignalObservation]) -> Dict[int, float]:
        """Median pre-post move at each lookback that was measured."""
        buckets: Dict[int, List[float]] = defaultdict(list)
        for o in usable:
            for window, pct in o.pre_run_windows.items():
                buckets[window].append(pct)
        return {w: statistics.median(v) for w, v in sorted(buckets.items()) if v}

    @staticmethod
    def _best_horizon(usable: List[SignalObservation]) -> Tuple[int, float]:
        """Which holding period this channel's calls actually pay for.

        Pump spikes peak and then give it all back, so the horizon that
        maximises median net return *is* the answer to "how long should I
        hold". A channel whose best horizon is 15 s and whose 15-minute return
        is negative is tradable — but only by somebody who exits in 15 seconds.
        """
        horizons: Dict[int, List[float]] = defaultdict(list)
        for o in usable:
            for h, r in o.returns_pct.items():
                horizons[h].append(r - o.cost_pct)

        best_h, best_ret = 0, float("-inf")
        for h, values in sorted(horizons.items()):
            med = statistics.median(values)
            if med > best_ret:
                best_h, best_ret = h, med
        return best_h, (best_ret if best_ret > float("-inf") else 0.0)

    def _originator_score(self, leads: List[float], pre_run_pct: float) -> float:
        """1.0 = first to call and calling before the move; 0.0 = pure relay."""
        # Lead component: median seconds ahead of the first caller, squashed.
        if leads:
            median_lead = statistics.median(leads)
            lead_component = 1.0 / (1.0 + math.exp(-median_lead / 60.0))
        else:
            lead_component = 0.5      # no overlap observed: no evidence either way

        # Pre-run component: the more the price already moved before the post,
        # the later the channel effectively is, regardless of clock ordering.
        penalty = min(1.0, max(0.0, pre_run_pct) / 40.0)
        pre_component = 1.0 - penalty

        wp = self._cfg.relay_penalty_weight
        return max(0.0, min(1.0, (1.0 - wp) * lead_component + wp * pre_component))

    @staticmethod
    def _consistency(nets: Sequence[float]) -> float:
        """Reward a tight, repeatable return distribution.

        Uses the interquartile range against absolute median, which is robust
        to the handful of 300% outliers these channels produce and which no
        follower ever actually captures.
        """
        if len(nets) < 4:
            return 0.5
        s = sorted(nets)
        q1 = s[len(s) // 4]
        q3 = s[(3 * len(s)) // 4]
        iqr = q3 - q1
        scale = max(abs(statistics.median(s)), 1.0)
        return max(0.0, min(1.0, 1.0 / (1.0 + iqr / scale)))

    @staticmethod
    def _verdict(
        hit_rate: float,
        median_ret: float,
        mean_ret: float,
        originator: float,
        pre_run: float,
    ) -> str:
        if median_ret <= 0:
            if mean_ret > 0:
                return (
                    "AVOID — negative median with positive mean: a few outliers carry "
                    "the average while the typical call loses. Classic distribution profile."
                )
            return "AVOID — negative expectancy net of costs."
        if pre_run > 25.0:
            return (
                f"AVOID — price has typically already run {pre_run:.0f}% before the post. "
                "Followers are the exit, not the entry."
            )
        if originator < 0.35:
            return "RELAY — profitable on paper but consistently late; find its source."
        if hit_rate < 0.45:
            return "MARGINAL — positive median but fewer than half of calls work."
        return "CANDIDATE — positive net median, timely, acceptable hit rate."

    # -- independence ---------------------------------------------------
    def overlap_clusters(
        self, *, min_shared: int = 3, min_jaccard: float = 0.5
    ) -> List[List[Tuple[int, str]]]:
        """Group channels that call the same things.

        Two channels posting the same calls are one source, not two. Counting
        them separately inflates the apparent sample and makes a single
        caller's record look corroborated by an independent one — which is the
        most flattering error available when deciding what to trade.

        Similarity is Jaccard overlap on the set of symbols called, with a
        floor on the shared count so two channels that each called the same
        two majors are not declared identical.
        """
        symbols: Dict[int, Set[str]] = {}
        for chat_id, obs_list in self._obs.items():
            symbols[chat_id] = {o.symbol for o in obs_list}

        ids = [cid for cid, syms in symbols.items() if syms]
        parent = {cid: cid for cid in ids}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                shared = symbols[a] & symbols[b]
                if len(shared) < min_shared:
                    continue
                union = symbols[a] | symbols[b]
                if union and len(shared) / len(union) >= min_jaccard:
                    parent[find(a)] = find(b)

        groups: Dict[int, List[Tuple[int, str]]] = defaultdict(list)
        for cid in ids:
            groups[find(cid)].append((cid, self._names.get(cid, str(cid))))
        return [sorted(g, key=lambda x: x[1]) for g in groups.values() if len(g) > 1]

    # -- convenience ----------------------------------------------------
    def score_map(self) -> Dict[int, float]:
        return {c.chat_id: c.composite for c in self.score_all()}


def _squash(value_pct: float, scale: float) -> float:
    """Map a percentage return onto (0, 1) with 0% at 0.5.

    Bounded on purpose: a channel with a 400% median is either a data error or
    a survivorship artefact, and it should not be able to dominate the
    composite on that basis alone.
    """
    return 1.0 / (1.0 + math.exp(-value_pct / scale))
