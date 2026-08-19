from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

_DUPLICATE_SIGNATURE_FIELDS = (
    "food_name",
    "category",
    "portion",
    "nutrients",
    "source",
    "manufacturer",
)


def normalize_food_selection_candidates(
    raw_candidates: Any,
) -> list[dict[str, Any]]:
    """Return distinct candidates with unique v1.3 selection strings."""

    if not isinstance(raw_candidates, list):
        return []

    candidates: list[dict[str, Any]] = []
    seen_signatures: set[tuple[Any, ...]] = set()
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        food_name = str(raw.get("food_name") or "").strip()
        if not food_name:
            continue
        candidate = dict(raw)
        candidate["food_name"] = food_name
        signature = _candidate_signature(candidate)
        if signature in seen_signatures:
            continue
        candidates.append(candidate)
        seen_signatures.add(signature)

    name_counts = Counter(
        str(candidate["food_name"])
        for candidate in candidates
    )
    name_positions: defaultdict[str, int] = defaultdict(int)
    used_values: set[str] = set()
    for candidate in candidates:
        food_name = str(candidate["food_name"])
        if name_counts[food_name] == 1:
            preferred_value = food_name
        else:
            name_positions[food_name] += 1
            portion = str(candidate.get("portion") or "").strip()
            portion_suffix = f" ({portion})" if portion else ""
            preferred_value = (
                f"{name_positions[food_name]}. "
                f"{food_name}{portion_suffix}"
            )
        candidate["selection_value"] = _unique_selection_value(
            preferred_value,
            used_values,
        )

    return candidates


def _candidate_signature(
    candidate: dict[str, Any],
) -> tuple[Any, ...]:
    return tuple(
        _freeze_signature_value(candidate.get(field))
        for field in _DUPLICATE_SIGNATURE_FIELDS
    )


def _freeze_signature_value(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(
            sorted(
                (
                    str(key),
                    _freeze_signature_value(item),
                )
                for key, item in value.items()
            )
        )
    if isinstance(value, list):
        return tuple(_freeze_signature_value(item) for item in value)
    if isinstance(value, str):
        return value.strip()
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _unique_selection_value(
    preferred_value: str,
    used_values: set[str],
) -> str:
    if preferred_value not in used_values:
        used_values.add(preferred_value)
        return preferred_value
    suffix = 2
    while f"{preferred_value} [{suffix}]" in used_values:
        suffix += 1
    value = f"{preferred_value} [{suffix}]"
    used_values.add(value)
    return value
