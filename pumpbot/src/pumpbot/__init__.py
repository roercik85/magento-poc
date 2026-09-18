"""pumpbot — Telegram pump-channel signal research and trading harness.

Simulation first. Live trading is gated behind a record of profitable simulated
runs, an explicit acknowledgement flag, and a hard notional cap.

See ``LEGAL.md`` before using live mode.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
