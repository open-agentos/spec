#!/usr/bin/env python3
"""Cost rate constants for API token accounting.

Rates are defined in a single place so they can be updated in one edit when
pricing changes. These constants are used to calculate USD cost from token counts.

Rates as of 2026-06:
  - Input tokens: $1.00 per million
  - Output tokens: $5.00 per million

Per-model rates live in ``model_rates.yml`` (same directory). Use
``calculate_cost_usd_for_model()`` when the provider and model are known;
``calculate_cost_usd()`` remains for callers that only have token counts.
"""

from pathlib import Path

import yaml

# Cost per million tokens (USD)
COST_PER_M_INPUT_USD = 1.00
COST_PER_M_OUTPUT_USD = 5.00

_RATES_PATH = Path(__file__).parent / "model_rates.yml"


def _load_model_rates() -> dict | None:
    """Load ``model_rates.yml`` and return the parsed dict, or ``None`` on failure."""
    try:
        with _RATES_PATH.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return None


def get_model_rates(provider: str | None, model: str | None) -> tuple[float, float]:
    """Return the (input_per_m_usd, output_per_m_usd) for a provider/model pair.

    Looks up ``models[provider][model]`` in ``model_rates.yml``. Falls back to
    the ``default`` entry when the provider or model is not found. If the rates
    file is missing or unreadable, falls back to the hardcoded constants
    :data:`COST_PER_M_INPUT_USD` / :data:`COST_PER_M_OUTPUT_USD`.

    A missing provider or model does not raise; it simply returns the default or
    hardcoded rates so cost calculation stays resilient in monitoring paths.
    """
    data = _load_model_rates()

    if data is None:
        return COST_PER_M_INPUT_USD, COST_PER_M_OUTPUT_USD

    models = data.get("models", {})
    provider_rates = models.get((provider or "").lower().strip(), {})
    rates = provider_rates.get((model or "").strip())

    if rates is None:
        rates = data.get("default", {})

    input_rate = rates.get("input_per_m_usd", COST_PER_M_INPUT_USD)
    output_rate = rates.get("output_per_m_usd", COST_PER_M_OUTPUT_USD)
    return float(input_rate), float(output_rate)


def calculate_cost_usd(input_tokens: int, output_tokens: int) -> dict[str, float]:
    """Calculate USD cost for a given number of input and output tokens.

    Returns a dict with three keys:
      - input_cost_usd: cost of input tokens
      - output_cost_usd: cost of output tokens
      - total_cost_usd: sum of input and output costs

    All values are rounded to 6 decimal places. Zero-token inputs produce zero cost.
    """
    input_cost = (input_tokens / 1_000_000) * COST_PER_M_INPUT_USD
    output_cost = (output_tokens / 1_000_000) * COST_PER_M_OUTPUT_USD
    total_cost = input_cost + output_cost

    return {
        "input_cost_usd": round(input_cost, 6),
        "output_cost_usd": round(output_cost, 6),
        "total_cost_usd": round(total_cost, 6),
    }


def calculate_cost_usd_for_model(
    provider: str, model: str, input_tokens: int, output_tokens: int
) -> dict[str, float]:
    """Calculate USD cost using per-model rates from ``model_rates.yml``.

    Looks up ``models[provider][model]`` for ``input_per_m_usd`` and
    ``output_per_m_usd``.  Falls back to the ``default`` entry when the
    provider or model is not found.  If ``model_rates.yml`` is missing or
    unreadable, falls back to the hardcoded constants
    (:data:`COST_PER_M_INPUT_USD` / :data:`COST_PER_M_OUTPUT_USD`).

    Returns a dict with ``input_cost_usd``, ``output_cost_usd``, and
    ``total_cost_usd`` (all rounded to 6 decimal places), matching the shape
    of :func:`calculate_cost_usd`. In addition, when rates are successfully
    resolved, the dict contains ``model_input_rate_usd`` and
    ``model_output_rate_usd`` so callers can record which rates were used.
    """
    input_rate, output_rate = get_model_rates(provider, model)

    input_cost = (input_tokens / 1_000_000) * input_rate
    output_cost = (output_tokens / 1_000_000) * output_rate
    total_cost = input_cost + output_cost

    return {
        "input_cost_usd": round(input_cost, 6),
        "output_cost_usd": round(output_cost, 6),
        "total_cost_usd": round(total_cost, 6),
        "model_input_rate_usd": input_rate,
        "model_output_rate_usd": output_rate,
    }
