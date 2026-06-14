"""Single source of truth for strategy priority ordering.

Higher number = higher priority. Used for:
  - Capital allocation (highest priority gets first pick)
  - Dedup (when two strategies signal same symbol, highest priority wins)
"""

PRIORITY = {
    "MA Pullback": 4,
    "Supertrend":  3,
    "BB Squeeze":  2,
    "Black Swan":  1,
}


def strategy_score(name: str) -> int:
    """Return priority score for a strategy name. Unknown strategies return 0."""
    return PRIORITY.get(name, 0)
