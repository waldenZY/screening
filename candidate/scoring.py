"""Stage1 collection-specific scores, independent of preprocessing indexes."""
from collections.abc import Mapping
from typing import Any


def sampling_scores(candidate: Mapping[str, Any]) -> dict[str, float]:
    probability = float(candidate.get("signals", candidate)["cluster_probability"])
    frequency = float(candidate["features"]["normalized_frequency"])
    return {
        "p0": 0.6 * probability + 0.4 * frequency,
        "p2": 0.5 * (1.0 - probability) + 0.5 * (1.0 - frequency),
    }
