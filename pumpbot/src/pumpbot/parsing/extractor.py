"""Signal extraction from free-text channel messages.

Design constraints, in order:

1. **Speed.** This runs inside the Telegram update handler. Budget is ~200 µs.
   Every pattern is precompiled at import; nothing allocates unless a match
   actually fires; the cheapest high-precision patterns run first and we return
   as soon as one is decisive.
2. **Precision over recall.** A missed call costs an opportunity. A false
   positive costs money, because it means sending a market order into a symbol
   nobody called. When in doubt, emit low confidence and let risk reject it.
3. **Explainability.** Every signal carries ``matched_by`` so a bad fill can be
   traced back to the exact pattern that produced it, and the pattern fixed.
"""
from __future__ import annotations

import re
from typing import List, Optional, Sequence, Set, Tuple

from ..models import RawMessage, Signal, SignalKind

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
# Cashtag: "$PEPE", "$pepe". The dollar sign is the ticker convention, so
# any case is accepted.
_RE_CASHTAG = re.compile(r"\$([A-Z][A-Z0-9]{1,14})\b")

# Hashtag: "#PEPE". Run against the ORIGINAL text and case-sensitive, because
# "#" marks topics at least as often as tickers — "#base hype heating up",
# "#Gaming narrative" — and case is what separates the two in practice. A
# ticker hashtag is written in caps.
_RE_HASHTAG_CASED = re.compile(r"#([A-Z][A-Z0-9]{1,14})\b")

# Explicit pair, run against the ORIGINAL text and case-sensitive, so a space
# is allowed as the separator: "MYRO USDT" is a real format, but matching it on
# case-folded text would also swallow "get your USDT ready". Requiring the base
# to be written in caps is what makes the space safe.
_RE_PAIR_CASED = re.compile(
    r"\b([A-Z][A-Z0-9]{1,14})\s*[/\-_]?\s*(?:USDT|USDC|FDUSD|BUSD)\b"
)

# Same idea without the space, run on case-folded text so "pepe/usdt" is caught.
_RE_PAIR = re.compile(
    r"\b([A-Z][A-Z0-9]{1,14})[/\-_]?(?:USDT|USDC|FDUSD|BUSD|BTC|ETH)\b"
)

# Imperative call: "BUY PEPE", "LONG $PEPE NOW", "APE INTO PEPE".
_RE_IMPERATIVE = re.compile(
    r"\b(?:BUY|LONG|ENTRY|ENTER|APE(?:\s+INTO)?|SNIPE|ACCUMULATE|GRAB)\b"
    r"[\s:>\-]*"
    r"[$#]?([A-Z][A-Z0-9]{1,14})\b"
)

# Labelled field: "Coin: PEPE", "Token - PEPE", "Symbol: PEPE".
_RE_LABELLED = re.compile(
    r"\b(?:COIN|TOKEN|SYMBOL|TICKER|PAIR|ASSET)\b\s*[:\-=]\s*[$#]?([A-Z][A-Z0-9]{1,14})\b"
)

# On-chain addresses.
_RE_EVM = re.compile(r"\b(0x[a-fA-F0-9]{40})\b")
# Solana base58: no 0, O, I, l. 32-44 chars.
_RE_SOL = re.compile(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b")

# Targets and stops, e.g. "TP1: 10%", "Target 25%", "SL: 5%".
_RE_TARGET = re.compile(r"\b(?:TP\d?|TARGET\d?|TAKE\s*PROFIT)\b\s*[:\-=]?\s*(\d{1,3}(?:\.\d+)?)\s*%")
_RE_STOP = re.compile(r"\b(?:SL|STOP(?:\s*LOSS)?)\b\s*[:\-=]?\s*(\d{1,3}(?:\.\d+)?)\s*%")

# Urgency markers. These do not identify the token, but they separate "here is
# our weekly market commentary" from "BUY NOW", and that distinction is most of
# the precision.
_RE_URGENCY = re.compile(
    r"\b(?:NOW|FAST|HURRY|QUICK|IMMEDIATELY|PUMP|MOON|LAUNCH|LIVE|GO+)\b|🚀|🔥|⚡"
)

# Words that look like tickers but never are. Cheap set membership beats a
# clever regex here.
_STOPWORDS: Set[str] = {
    "THE", "AND", "FOR", "YOU", "ALL", "NEW", "NOW", "BUY", "SELL", "USD",
    "CEX", "DEX", "ATH", "ATL", "TP", "SL", "PNL", "ROI", "APY", "APR",
    "NFA", "DYOR", "FOMO", "HODL", "LFG", "GM", "GN", "OTC", "IDO", "ICO",
    "AI", "VIP", "PRO", "MAX", "MIN", "BIG", "TOP", "LOW", "HIGH", "NEXT",
    "SOON", "PUMP", "DUMP", "MOON", "LONG", "SHORT", "ENTRY", "EXIT",
    "TARGET", "STOP", "LOSS", "PROFIT", "GAIN", "CALL", "COIN", "TOKEN",
    "USDT", "USDC", "BUSD", "FDUSD", "HTTP", "HTTPS", "WWW", "COM", "IO",
    # Words that follow a verb in ordinary channel prose and therefore land
    # in the ticker slot: "LONG SETUP", "trade CLOSED", "quick SCALP". Every
    # one of these was mined from a real channel's month of posts, where the
    # parser reported 35 calls and not one named a tradable symbol.
    "SETUP", "SCALP", "SWING", "CLOSED", "CLOSE", "OPEN", "OPENED", "UPDATE",
    "FEEDBACK", "RESULT", "RESULTS", "SIGNAL", "SIGNALS", "TRADE", "TRADES",
    "POSITION", "LEVERAGE", "MARGIN", "FUTURES", "SPOT", "CHART", "ANALYSIS",
    "SUPPORT", "RESISTANCE", "BREAKOUT", "REVERSAL", "TREND", "VOLUME",
    "MARKET", "PRICE", "ZONE", "RANGE", "LEVEL", "LEVELS", "MOVE", "READY",
    "LIVE", "FREE", "PAID", "PLAN", "RISK", "SIZE", "PART", "TEAM", "NEWS",
    "ALERT", "WATCH", "READ", "JOIN", "LINK", "HERE", "THIS", "THAT", "WITH",
    "FROM", "INTO", "OVER", "MORE", "LESS", "BEST", "GOOD", "NICE", "WELL",
    "DONE", "SAFE", "SURE", "HOLD", "WAIT", "KEEP", "TAKE", "MAKE", "SEND",
    "BITCOIN", "ETHEREUM", "SOLANA", "RIPPLE", "CARDANO", "TETHER",
}

# Suffixes that a hyphen or space can strand, leaving a fragment that is not
# itself a stopword: "SET-UP" -> "SET", "BREAK-OUT" -> "BREAK".
_SPLIT_TAILS = ("UP", "OUT", "OFF", "IN", "DOWN", "BACK", "OVER")


class SignalExtractor:
    """Stateless, thread-safe, allocation-light message → signal converter."""

    __slots__ = ("_ignore", "_quotes", "_accept_contracts", "_min_conf")

    def __init__(
        self,
        ignore_symbols: Sequence[str] = (),
        quote_assets: Sequence[str] = ("USDT",),
        accept_contracts: bool = True,
        min_confidence: float = 0.0,
    ) -> None:
        self._ignore = {s.upper() for s in ignore_symbols}
        self._quotes = [q.upper() for q in quote_assets] or ["USDT"]
        self._accept_contracts = accept_contracts
        self._min_conf = min_confidence

    # -- public ---------------------------------------------------------
    def extract(self, raw: RawMessage) -> Optional[Signal]:
        """Return a Signal, or None if the message carries no trading intent.

        Returning None is the common case by a large margin — most traffic in
        these channels is chatter — so the early exits matter.
        """
        text = raw.text
        if not text or len(text) < 3:
            return None

        upper = text.upper()
        urgency = bool(_RE_URGENCY.search(upper))

        base, matched_by, pattern_conf = self._find_base(text, upper)

        if base is None:
            if self._accept_contracts:
                contract, chain = self._find_contract(text)
                if contract:
                    conf = 0.70 + (0.15 if urgency else 0.0)
                    return Signal.new(
                        raw,
                        kind=SignalKind.CONTRACT,
                        symbol=None,
                        base=None,
                        contract=contract,
                        chain=chain,
                        confidence=min(conf, 1.0),
                        matched_by=f"contract:{chain}",
                        target_pcts=self._targets(upper),
                        stop_pct=self._stop(upper),
                    )
            return None

        if base in self._ignore or self._strip_quote(base) in self._ignore:
            return None

        confidence = pattern_conf
        if urgency:
            confidence += 0.15
        # A message that also states targets or a stop reads like a trade plan
        # rather than a passing mention.
        targets = self._targets(upper)
        stop = self._stop(upper)
        if targets:
            confidence += 0.08
        if stop:
            confidence += 0.05
        confidence = min(confidence, 1.0)

        if confidence < self._min_conf:
            return None

        return Signal.new(
            raw,
            kind=SignalKind.SYMBOL,
            symbol=self._as_pair(base),
            base=base,
            contract=None,
            chain=None,
            confidence=confidence,
            matched_by=matched_by,
            target_pcts=targets,
            stop_pct=stop,
        )

    # -- internals ------------------------------------------------------
    def _find_base(self, text: str, upper: str) -> Tuple[Optional[str], str, float]:
        """Most specific pattern wins; each carries its own base confidence."""
        # An explicit pair is the strongest evidence available: it names both
        # the token and the quote asset.
        m = _RE_PAIR_CASED.search(text)
        if m and self._acceptable(m.group(1)):
            return m.group(1), "pair", 0.85

        m = _RE_PAIR.search(upper)
        if m and self._acceptable(m.group(1)):
            return m.group(1), "pair", 0.85

        m = _RE_LABELLED.search(upper)
        if m and self._acceptable(m.group(1)):
            return m.group(1), "labelled", 0.80

        m = _RE_IMPERATIVE.search(upper)
        if m and self._acceptable(m.group(1)):
            return m.group(1), "imperative", 0.78

        m = _RE_CASHTAG.search(upper)
        if m and self._acceptable(m.group(1)):
            return m.group(1), "cashtag", 0.72

        m = _RE_HASHTAG_CASED.search(text)
        if m and self._acceptable(m.group(1)):
            return m.group(1), "hashtag", 0.70

        return None, "", 0.0

    def _strip_quote(self, base: str) -> str:
        for quote in self._quotes:
            if len(base) > len(quote) and base.endswith(quote):
                return base[: -len(quote)]
        return base

    def _as_pair(self, base: str) -> str:
        """Append the quote asset, unless the base already ends with one.

        "#BTCUSDT" parses to a base of BTCUSDT, and naively appending the quote
        produced BTCUSDTUSDT — a symbol no venue lists, so the call was silently
        dropped as untradable rather than recognised as BTC.
        """
        for quote in self._quotes:
            if len(base) > len(quote) and base.endswith(quote):
                return base
        return f"{base}{self._quotes[0]}"

    def _acceptable(self, token: str) -> bool:
        if token in _STOPWORDS or token in self._ignore:
            return False
        # A hyphen splits a stopword into a fragment that is not one: "LONG
        # SET-UP" yielded SET, which is a real KuCoin-adjacent-looking ticker
        # and reached the order path as a call.
        if any(token + tail in _STOPWORDS for tail in _SPLIT_TAILS):
            return False
        # A bare two-letter token is almost always an abbreviation, not a
        # ticker, unless it arrived with a cashtag (handled by ordering above).
        if len(token) < 3:
            return False
        # All-digits, or a year, is not a ticker.
        if token.isdigit():
            return False
        return True

    def _find_contract(self, text: str) -> Tuple[Optional[str], Optional[str]]:
        m = _RE_EVM.search(text)
        if m:
            return m.group(1), "evm"
        m = _RE_SOL.search(text)
        if m:
            candidate = m.group(1)
            # Base58 of the right length is common in URLs and hashes. Require
            # a plausible mint length to cut the worst of the false positives.
            if 32 <= len(candidate) <= 44 and not candidate.isalpha():
                return candidate, "solana"
        return None, None

    @staticmethod
    def _targets(upper: str) -> List[float]:
        return [float(x) for x in _RE_TARGET.findall(upper)][:5]

    @staticmethod
    def _stop(upper: str) -> Optional[float]:
        m = _RE_STOP.search(upper)
        return float(m.group(1)) if m else None
