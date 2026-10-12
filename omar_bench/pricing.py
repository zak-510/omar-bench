"""Tokens to USD at the pinned list prices in bench.toml."""
from __future__ import annotations

from .usage import LONG

RATES = ("input", "cache_read", "cache_write_5m", "cache_write_1h", "output")


def long_over(prices: dict) -> dict[str, int]:
    """Prompt-size thresholds, keyed by every id a model is served under."""
    out = {}
    for model, row in prices.get("models", {}).items():
        if "long_over" in row:
            for name in (model, *row.get("aliases", ())):
                out[name] = row["long_over"]
    return out


def _rates(prices: dict, name: str) -> dict | None:
    model, is_long = name.removesuffix(LONG), name.endswith(LONG)
    for key, row in prices.get("models", {}).items():
        if model == key or model in row.get("aliases", ()):
            return row.get("long") if is_long else row
    return None


def cost(by_model: dict[str, dict], prices: dict) -> tuple[float | None, list[str]]:
    """(USD, models that could not be priced). USD is None unless every token has a rate."""
    usd, missing = 0.0, []
    for name, row in by_model.items():
        rates = _rates(prices, name)
        if rates is None or any(row[f] and f not in rates for f in RATES):
            missing.append(name)
            continue
        usd += sum(row[f] * rates[f] for f in RATES if row[f]) / 1e6
    return (None if missing else round(usd, 6)), sorted(missing)
