"""The liquidity-incentive (LIP) maker shadow study - pre-registered in docs/lip-preregistration.md.

Research, paper and shadow only. Nothing in this package sends an order, calls a
write endpoint or reads an API key. It records public books and prints for markets
that carry a Kalshi liquidity program, and replays a declared family of maker
policies against them, scoring the program's reward and the fills side by side.

Deliberately separate from the maker study: nothing the maker study imports is
changed, and the maker study's cron never imports this package.
"""

from .version import LIP_RECORDER_VERSION, LIP_SIM_VERSION, SAMPLE_SALT

__all__ = ["LIP_RECORDER_VERSION", "LIP_SIM_VERSION", "SAMPLE_SALT"]
