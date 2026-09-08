"""Deterministic, read-only V3 cognitive strategy attribution reports.

The module deliberately accepts plain mappings.  Storage adapters may build a
snapshot, but report calculation neither imports nor writes any store module.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any, cast

REPORT_VERSION = "cognitive-strategy-report-v1"
ENDPOINTS = ("immediate", "transfer", "retention")
STRATUM_FIELDS = (
    "topic_id",
    "hypothesis_id",
    "question_family_id",
    "difficulty_bucket",
    "hint_state",
    "version_set_id",
)
EXCLUSION_REASONS = (
    "invalid_catalog",
    "incompatible_version",
    "invalid_provenance",
    "late_outcome",
    "incomplete_chain",
    "development_time_override",
    "answer_disclosed",
    "independence_unknown",
    "ambiguous_attribution",
)
OUTCOME_STATUSES = (
    "observed",
    "pending",
    "missing_outcome",
    "abandoned",
    "replaced",
    "excluded",
)

_SIX_PLACES = 1_000_000
_Z_95 = 1.96
_DAY_SECONDS = 24 * 60 * 60


class CognitiveStrategyReportError(ValueError):
    """Raised when a report snapshot violates the public input contract."""


def _text(value: object) -> str:
    return str(value or "").strip()


def _hypothesis(record: Mapping[str, Any]) -> str:
    return _text(record.get("hypothesis_id", record.get("hypothesis_code")))


def _integer(value: object, *, name: str) -> int:
    if isinstance(value, bool):
        raise CognitiveStrategyReportError(f"{name}_invalid")
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CognitiveStrategyReportError(f"{name}_invalid") from exc
    if result < 0:
        raise CognitiveStrategyReportError(f"{name}_invalid")
    return result


def _rounded(value: float | Fraction) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise CognitiveStrategyReportError("non_finite_result")
    # All report values are non-tie ratios or deterministic analytic results.
    # Adding half a unit before flooring gives conventional half-up display.
    if number >= 0:
        return math.floor(number * _SIX_PLACES + 0.5) / _SIX_PLACES
    return math.ceil(number * _SIX_PLACES - 0.5) / _SIX_PLACES


def _fraction_payload(value: Fraction) -> dict[str, Any]:
    return {
        "numerator": value.numerator,
        "denominator": value.denominator,
        "value": _rounded(value),
    }


def _wilson_95(success_n: int, eligible_n: int) -> dict[str, float] | None:
    if eligible_n <= 0:
        return None
    rate = success_n / eligible_n
    z2 = _Z_95 * _Z_95
    denominator = 1.0 + z2 / eligible_n
    centre = (rate + z2 / (2.0 * eligible_n)) / denominator
    margin = (
        _Z_95
        * math.sqrt(
            rate * (1.0 - rate) / eligible_n
            + z2 / (4.0 * eligible_n * eligible_n)
        )
        / denominator
    )
    return {
        "lower": _rounded(max(0.0, centre - margin)),
        "upper": _rounded(min(1.0, centre + margin)),
    }


def _sort_key(record: Mapping[str, Any]) -> tuple[int, str, str]:
    raw_seq = record.get("root_fact_seq", 0)
    try:
        seq = int(raw_seq)
    except (TypeError, ValueError):
        seq = 0
    return seq, _text(record.get("source_id")), _text(record.get("exposure_id"))


def _dedupe(records: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    ordered = sorted((dict(item) for item in records), key=_sort_key)
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    duplicate_n = 0
    for record in ordered:
        exposure_id = _text(record.get("exposure_id"))
        # An absent ID cannot safely collapse two unrelated malformed records.
        dedupe_key = exposure_id or f"__missing__:{len(result)}"
        if dedupe_key in seen:
            duplicate_n += 1
            continue
        seen.add(dedupe_key)
        result.append(record)
    return result, duplicate_n


def _snapshot_parts(
    snapshot_or_exposures: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    as_of_root_fact_seq: int | None,
    catalog_version: str | None,
    version_set_id: str | None,
    catalog: Iterable[Mapping[str, Any]] | None,
) -> tuple[list[Mapping[str, Any]], int, str, str, list[Mapping[str, Any]]]:
    default_learner_id = ""
    if isinstance(snapshot_or_exposures, Mapping):
        snapshot = dict(cast(Mapping[str, Any], snapshot_or_exposures))
        raw_boundary = snapshot.get("boundary")
        boundary = dict(raw_boundary) if isinstance(raw_boundary, Mapping) else {}
        records = snapshot.get("exposures", snapshot.get("records", ()))
        if as_of_root_fact_seq is None:
            as_of_root_fact_seq = snapshot.get(  # type: ignore[assignment]
                "as_of_root_fact_seq", boundary.get("as_of_root_fact_seq")
            )
        catalog_version = catalog_version or _text(
            snapshot.get("catalog_version", boundary.get("catalog_version"))
        )
        version_set_id = version_set_id or _text(
            snapshot.get("version_set_id", boundary.get("version_set_id"))
        )
        if catalog is None:
            catalog = snapshot.get("catalog", snapshot.get("catalog_entries", ()))
        default_learner_id = _text(
            snapshot.get("learner_id", snapshot.get("user_id"))
        )
    else:
        records = snapshot_or_exposures
    if isinstance(records, (str, bytes, Mapping)) or not isinstance(records, Iterable):
        raise CognitiveStrategyReportError("exposures_invalid")
    if catalog is None:
        catalog = ()
    if isinstance(catalog, (str, bytes, Mapping)) or not isinstance(catalog, Iterable):
        raise CognitiveStrategyReportError("catalog_invalid")
    seq = _integer(as_of_root_fact_seq, name="as_of_root_fact_seq")
    catalog_name = _text(catalog_version)
    version_name = _text(version_set_id)
    if not catalog_name:
        raise CognitiveStrategyReportError("catalog_version_missing")
    if not version_name:
        raise CognitiveStrategyReportError("version_set_id_missing")
    raw_record_list = list(records)
    if not all(isinstance(item, Mapping) for item in raw_record_list):
        raise CognitiveStrategyReportError("exposures_invalid")
    record_list = [dict(item) for item in raw_record_list]
    if default_learner_id:
        for record in record_list:
            record.setdefault("learner_id", default_learner_id)
    catalog_list = list(catalog)
    if not all(isinstance(item, Mapping) for item in catalog_list):
        raise CognitiveStrategyReportError("catalog_invalid")
    return record_list, seq, catalog_name, version_name, catalog_list


def _catalog_entry_matches(
    entry: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    catalog_version: str,
    version_set_id: str,
) -> bool:
    if _text(entry.get("strategy_id")) != _text(record.get("strategy_id")):
        return False
    if _text(entry.get("strategy_version")) != _text(record.get("strategy_version")):
        return False
    entry_catalog = _text(entry.get("catalog_version")) or catalog_version
    entry_versions = _text(entry.get("version_set_id")) or version_set_id
    if entry_catalog != catalog_version or entry_versions != version_set_id:
        return False
    if entry.get("shadow_collection_eligible") is False:
        return False
    if _text(entry.get("measurement_purpose")) not in {"", "repair"}:
        return False
    if _text(entry.get("topic_id")) not in {"", _text(record.get("topic_id"))}:
        return False
    if _text(entry.get("hypothesis_code")) not in {"", _hypothesis(record)}:
        return False
    scope = entry.get("comparison_scope", entry.get("scope", {}))
    if isinstance(scope, Mapping):
        for field, expected in scope.items():
            if expected is not None and _text(record.get(str(field))) != _text(expected):
                return False
    return True


def _catalog_entry(
    record: Mapping[str, Any],
    catalog: Sequence[Mapping[str, Any]],
    *,
    catalog_version: str,
    version_set_id: str,
) -> Mapping[str, Any] | None:
    matches = [
        item
        for item in catalog
        if _catalog_entry_matches(
            item,
            record,
            catalog_version=catalog_version,
            version_set_id=version_set_id,
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _explicit_reasons(record: Mapping[str, Any]) -> set[str]:
    raw = record.get("exclusion_reasons", ())
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, Iterable):
        return set()
    return {_text(item) for item in raw if _text(item) in EXCLUSION_REASONS}


def _base_reasons(
    record: Mapping[str, Any],
    *,
    entry: Mapping[str, Any] | None,
    catalog_supplied: bool,
    as_of_root_fact_seq: int,
    catalog_version: str,
    version_set_id: str,
) -> set[str]:
    reasons = _explicit_reasons(record)
    required_identity = (
        "exposure_id",
        "source_id",
        "strategy_id",
        "strategy_version",
        "topic_id",
        "question_family_id",
        "learner_id",
    )
    if (
        any(not _text(record.get(field)) for field in required_identity)
        or not _hypothesis(record)
    ):
        reasons.add("incomplete_chain")
    try:
        root_seq = int(record.get("root_fact_seq", -1))
    except (TypeError, ValueError):
        root_seq = -1
    if root_seq < 0:
        reasons.add("incomplete_chain")
    elif root_seq > as_of_root_fact_seq:
        reasons.add("late_outcome")
    if _text(record.get("catalog_version")) != catalog_version:
        reasons.add("invalid_catalog")
    if _text(record.get("version_set_id")) != version_set_id:
        reasons.add("incompatible_version")
    if record.get("catalog_valid") is False or (catalog_supplied and entry is None):
        reasons.add("invalid_catalog")
    if record.get("question_committed") is False or record.get(
        "strategy_determined_before_commit"
    ) is False:
        reasons.add("invalid_provenance")
    if _text(record.get("question_purpose")) not in {"", "repair"}:
        reasons.add("invalid_provenance")
    if record.get("provenance_complete") is False:
        reasons.add("incomplete_chain")
    if record.get("development_time_override") is True:
        reasons.add("development_time_override")
    if record.get("answer_disclosed") is True:
        reasons.add("answer_disclosed")
    if record.get("independence_known") is False:
        reasons.add("independence_unknown")
    return reasons


def _raw_outcome(record: Mapping[str, Any], endpoint: str) -> Mapping[str, Any] | None:
    outcomes = record.get("outcomes")
    raw = outcomes.get(endpoint) if isinstance(outcomes, Mapping) else None
    if raw is None:
        raw = record.get(endpoint, record.get(f"{endpoint}_outcome"))
    if isinstance(raw, bool):
        return {"success": raw, "status": "observed"}
    return raw if isinstance(raw, Mapping) else None


def _normal_status(value: object) -> str:
    status = _text(value).lower()
    aliases = {
        "complete": "observed",
        "completed": "observed",
        "eligible": "observed",
        "success": "observed",
        "failure": "observed",
        "missing": "missing_outcome",
        "right_censored": "pending",
    }
    status = aliases.get(status, status)
    return status if status in OUTCOME_STATUSES else ""


def _endpoint_state(
    record: Mapping[str, Any],
    endpoint: str,
    *,
    as_of_root_fact_seq: int,
) -> tuple[str, bool | None, set[str]]:
    lifecycle = _normal_status(record.get("status"))
    if lifecycle in {"abandoned", "replaced"}:
        return lifecycle, None, set()
    raw = _raw_outcome(record, endpoint)
    if raw is not None:
        status = _normal_status(raw.get("status"))
        success_raw = raw.get("success", raw.get("correct"))
        success = success_raw if isinstance(success_raw, bool) else None
        if success is None:
            outcome = _text(raw.get("outcome")).lower()
            if outcome in {"correct", "success", "passed", "resolved"}:
                success = True
            elif outcome in {"incorrect", "wrong", "failure", "failed", "relapsed"}:
                success = False
        if not status and success is not None:
            status = "observed"
        reasons = _explicit_reasons(raw)
        try:
            outcome_seq = int(raw.get("root_fact_seq", record.get("root_fact_seq", 0)))
        except (TypeError, ValueError):
            outcome_seq = -1
        if outcome_seq < 0:
            reasons.add("incomplete_chain")
        elif outcome_seq > as_of_root_fact_seq or raw.get("late") is True:
            reasons.add("late_outcome")
        if raw.get("certified") is False or raw.get("evaluator_certified") is False:
            reasons.add("invalid_provenance")
        if raw.get("provenance_complete") is False:
            reasons.add("incomplete_chain")
        if raw.get("development_time_override") is True:
            reasons.add("development_time_override")
        if raw.get("answer_disclosed") is True:
            reasons.add("answer_disclosed")
        if raw.get("independence_known") is False:
            reasons.add("independence_unknown")
        if endpoint == "retention":
            interval = raw.get("interval_hours")
            if interval is not None:
                try:
                    valid_interval = 24.0 <= float(interval) <= 168.0
                except (TypeError, ValueError):
                    valid_interval = False
                if not valid_interval:
                    reasons.add("late_outcome")
            if raw.get("independent_family") is False:
                reasons.add("independence_unknown")
            if raw.get("hint_used") is True or raw.get("obligation_completed") is False:
                reasons.add("invalid_provenance")
        if reasons:
            return "excluded", None, reasons
        if status == "observed" and success is None:
            return "excluded", None, {"incomplete_chain"}
        if status:
            return status, success, set()

    explicit = _normal_status(record.get(f"{endpoint}_status"))
    if explicit:
        return explicit, None, set()
    closes_at = record.get(f"{endpoint}_window_closes_root_fact_seq")
    if closes_at is not None:
        try:
            return (
                "pending" if int(closes_at) > as_of_root_fact_seq else "missing_outcome",
                None,
                set(),
            )
        except (TypeError, ValueError):
            return "excluded", None, {"incomplete_chain"}
    if record.get(f"{endpoint}_window_closed") is False:
        return "pending", None, set()
    return "missing_outcome", None, set()


def _epoch_seconds(record: Mapping[str, Any]) -> float | None:
    raw = record.get("exposure_epoch_seconds")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    for field in ("exposed_at", "committed_at", "occurred_at", "created_at"):
        value = _text(record.get(field))
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return None
    return None


def _independent_exposure_ids(records: Sequence[Mapping[str, Any]]) -> set[str]:
    accepted: set[str] = set()
    last_epoch: dict[tuple[str, str, str, str, str], float | None] = {}
    for record in records:
        if record.get("independent_exposure") is False:
            continue
        key = (
            _text(record.get("learner_id")),
            _hypothesis(record),
            _text(record.get("episode_id")),
            _text(record.get("strategy_id")),
            _text(record.get("strategy_version")),
        )
        epoch = _epoch_seconds(record)
        if key not in last_epoch:
            accepted.add(_text(record.get("exposure_id")))
            last_epoch[key] = epoch
            continue
        prior = last_epoch[key]
        if epoch is not None and prior is not None and epoch - prior >= _DAY_SECONDS:
            accepted.add(_text(record.get("exposure_id")))
            last_epoch[key] = epoch
    return accepted


def _hint_state(record: Mapping[str, Any]) -> str:
    explicit = _text(record.get("hint_state"))
    if explicit:
        return explicit
    value = record.get("hint_used", record.get("used_hint"))
    if value is True:
        return "used"
    if value is False:
        return "none"
    return "unknown"


def _stratum(record: Mapping[str, Any], version_set_id: str) -> tuple[str, ...]:
    return (
        _text(record.get("topic_id")),
        _hypothesis(record),
        _text(record.get("question_family_id")),
        _text(record.get("difficulty_bucket", record.get("difficulty"))),
        _hint_state(record),
        version_set_id,
    )


def _strategy_key(record: Mapping[str, Any]) -> tuple[str, str]:
    return _text(record.get("strategy_id")), _text(record.get("strategy_version"))


def _strategy_stats(
    items: Sequence[tuple[Mapping[str, Any], str, bool | None]],
    *,
    independent_ids: set[str],
    baseline: bool,
) -> dict[str, Any]:
    statuses = Counter(status for _, status, _ in items)
    independent = [
        (record, status, success)
        for record, status, success in items
        if _text(record.get("exposure_id")) in independent_ids
    ]
    eligible = [(record, success) for record, status, success in independent if status == "observed"]
    success_n = sum(1 for _, success in eligible if success is True)
    eligible_n = len(eligible)
    missing_n = sum(1 for _, status, _ in independent if status == "missing_outcome")
    rate = Fraction(success_n, eligible_n) if eligible_n else None
    missing_low, missing_high = _sensitivity_rate(success_n, eligible_n, missing_n)
    first = items[0][0]
    return {
        "strategy_id": _text(first.get("strategy_id")),
        "strategy_version": _text(first.get("strategy_version")),
        "baseline": baseline,
        "exposure_n": len(items),
        "independent_n": len(independent),
        "eligible_n": eligible_n,
        "success_n": success_n,
        "rate": _rounded(rate) if rate is not None else None,
        "wilson_95": _wilson_95(success_n, eligible_n),
        "missing_outcome_n": missing_n,
        "missing_sensitivity": {
            "all_missing_fail": _rounded(missing_low),
            "all_missing_succeed": _rounded(missing_high),
        },
        "status_counts": {status: statuses.get(status, 0) for status in OUTCOME_STATUSES},
    }


def _sensitivity_rate(success_n: int, eligible_n: int, missing_n: int) -> tuple[Fraction, Fraction]:
    denominator = eligible_n + missing_n
    if denominator == 0:
        return Fraction(0), Fraction(0)
    return Fraction(success_n, denominator), Fraction(success_n + missing_n, denominator)


def _comparison(
    strategy: Mapping[str, Any], baseline: Mapping[str, Any]
) -> dict[str, Any]:
    strategy_n = int(strategy["eligible_n"])
    baseline_n = int(baseline["eligible_n"])
    payload: dict[str, Any] = {
        "strategy_id": strategy["strategy_id"],
        "strategy_version": strategy["strategy_version"],
        "baseline_strategy_id": baseline["strategy_id"],
        "baseline_strategy_version": baseline["strategy_version"],
        "strategy_eligible_n": strategy_n,
        "baseline_eligible_n": baseline_n,
        "strategy_success_n": int(strategy["success_n"]),
        "baseline_success_n": int(baseline["success_n"]),
        "strategy_missing_outcome_n": int(strategy["missing_outcome_n"]),
        "baseline_missing_outcome_n": int(baseline["missing_outcome_n"]),
    }
    if min(strategy_n, baseline_n) < 20:
        payload.update({"status": "insufficient_data", "effect": None, "sensitivity_bounds": None})
        return payload
    effect = Fraction(int(strategy["success_n"]), strategy_n) - Fraction(
        int(baseline["success_n"]), baseline_n
    )
    strategy_low, strategy_high = _sensitivity_rate(
        int(strategy["success_n"]), strategy_n, int(strategy["missing_outcome_n"])
    )
    baseline_low, baseline_high = _sensitivity_rate(
        int(baseline["success_n"]), baseline_n, int(baseline["missing_outcome_n"])
    )
    payload.update(
        {
            "status": "comparable",
            "effect": _fraction_payload(effect),
            "sensitivity_bounds": {
                "lower": _rounded(strategy_low - baseline_high),
                "upper": _rounded(strategy_high - baseline_low),
            },
        }
    )
    return payload


def _build_endpoint_report(
    endpoint: str,
    records: Sequence[Mapping[str, Any]],
    *,
    entries: Mapping[str, Mapping[str, Any] | None],
    base_reasons: Mapping[str, set[str]],
    independent_ids: set[str],
    as_of_root_fact_seq: int,
    version_set_id: str,
) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    exclusion_counts: Counter[str] = Counter()
    grouped: dict[
        tuple[str, ...],
        dict[tuple[str, str], list[tuple[Mapping[str, Any], str, bool | None]]],
    ] = defaultdict(lambda: defaultdict(list))
    for record in records:
        exposure_id = _text(record.get("exposure_id"))
        status, success, endpoint_reasons = _endpoint_state(
            record, endpoint, as_of_root_fact_seq=as_of_root_fact_seq
        )
        reasons = set(base_reasons[exposure_id]) | endpoint_reasons
        if reasons:
            status = "excluded"
            success = None
            exclusion_counts.update(reasons)
        status_counts[status] += 1
        if not reasons:
            grouped[_stratum(record, version_set_id)][_strategy_key(record)].append(
                (record, status, success)
            )

    strata: list[dict[str, Any]] = []
    aggregate_parts: dict[tuple[str, str, str, str], list[tuple[int, Mapping[str, Any]]]] = defaultdict(list)
    for key in sorted(grouped):
        by_strategy = grouped[key]
        strategy_rows: list[dict[str, Any]] = []
        for strategy_key in sorted(by_strategy):
            items = by_strategy[strategy_key]
            entry = entries.get(_text(items[0][0].get("exposure_id")))
            baseline = bool(entry.get("baseline")) if entry is not None else bool(items[0][0].get("baseline"))
            strategy_rows.append(
                _strategy_stats(items, independent_ids=independent_ids, baseline=baseline)
            )
        baselines = [item for item in strategy_rows if item["baseline"]]
        comparisons: list[dict[str, Any]] = []
        comparison_status = "no_comparator"
        if not baselines:
            comparison_status = "not_comparable"
        elif len(baselines) != 1:
            comparison_status = "invalid_baseline_catalog"
        else:
            baseline = baselines[0]
            for strategy in strategy_rows:
                if strategy["baseline"]:
                    continue
                item = _comparison(strategy, baseline)
                comparisons.append(item)
                if item["status"] == "comparable":
                    aggregate_key = (
                        str(item["strategy_id"]),
                        str(item["strategy_version"]),
                        str(item["baseline_strategy_id"]),
                        str(item["baseline_strategy_version"]),
                    )
                    aggregate_parts[aggregate_key].append(
                        (min(int(item["strategy_eligible_n"]), int(item["baseline_eligible_n"])), item)
                    )
            if comparisons:
                comparison_status = (
                    "comparable"
                    if any(item["status"] == "comparable" for item in comparisons)
                    else "insufficient_data"
                )
        strata.append(
            {
                "stratum": dict(zip(STRATUM_FIELDS, key, strict=True)),
                "strategies": strategy_rows,
                "comparison_status": comparison_status,
                "comparisons": comparisons,
            }
        )

    aggregates: list[dict[str, Any]] = []
    for key in sorted(aggregate_parts):
        parts = aggregate_parts[key]
        denominator = sum(weight for weight, _ in parts)
        weighted_effect = sum(
            Fraction(weight, denominator)
            * Fraction(
                int(item["effect"]["numerator"]), int(item["effect"]["denominator"])
            )
            for weight, item in parts
        )
        lower = Fraction(0)
        upper = Fraction(0)
        for weight, item in parts:
            strategy_low, strategy_high = _sensitivity_rate(
                int(item["strategy_success_n"]),
                int(item["strategy_eligible_n"]),
                int(item["strategy_missing_outcome_n"]),
            )
            baseline_low, baseline_high = _sensitivity_rate(
                int(item["baseline_success_n"]),
                int(item["baseline_eligible_n"]),
                int(item["baseline_missing_outcome_n"]),
            )
            rational_weight = Fraction(weight, denominator)
            lower += rational_weight * (strategy_low - baseline_high)
            upper += rational_weight * (strategy_high - baseline_low)
        aggregates.append(
            {
                "strategy_id": key[0],
                "strategy_version": key[1],
                "baseline_strategy_id": key[2],
                "baseline_strategy_version": key[3],
                "status": "comparable",
                "common_strata_n": len(parts),
                "common_eligible_n": denominator,
                "effect": _fraction_payload(weighted_effect),
                "sensitivity_bounds": {"lower": _rounded(lower), "upper": _rounded(upper)},
                "weights": [
                    _fraction_payload(Fraction(weight, denominator)) for weight, _ in parts
                ],
            }
        )
    return {
        "status_counts": {status: status_counts.get(status, 0) for status in OUTCOME_STATUSES},
        "exclusion_reason_counts": {
            reason: exclusion_counts.get(reason, 0) for reason in EXCLUSION_REASONS
        },
        "strata": strata,
        "aggregate_comparisons": aggregates,
    }


def build_cognitive_strategy_report(
    snapshot_or_exposures: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    as_of_root_fact_seq: int | None = None,
    catalog_version: str | None = None,
    version_set_id: str | None = None,
    catalog: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a deterministic point-in-time strategy attribution report.

    A mapping snapshot may contain ``exposures``, ``catalog`` and the three
    boundary fields.  An iterable of exposure mappings is also accepted when
    the boundary fields are supplied as keyword arguments.
    """

    raw_records, as_of, catalog_name, version_name, catalog_entries = _snapshot_parts(
        snapshot_or_exposures,
        as_of_root_fact_seq=as_of_root_fact_seq,
        catalog_version=catalog_version,
        version_set_id=version_set_id,
        catalog=catalog,
    )
    records, _duplicate_n = _dedupe(raw_records)
    catalog_sequence = [dict(item) for item in catalog_entries]
    entry_by_exposure: dict[str, Mapping[str, Any] | None] = {}
    reasons_by_exposure: dict[str, set[str]] = {}
    for record in records:
        exposure_id = _text(record.get("exposure_id"))
        entry = _catalog_entry(
            record,
            catalog_sequence,
            catalog_version=catalog_name,
            version_set_id=version_name,
        )
        entry_by_exposure[exposure_id] = entry
        reasons_by_exposure[exposure_id] = _base_reasons(
            record,
            entry=entry,
            catalog_supplied=bool(catalog_sequence),
            as_of_root_fact_seq=as_of,
            catalog_version=catalog_name,
            version_set_id=version_name,
        )

    base_valid = [record for record in records if not reasons_by_exposure[_text(record.get("exposure_id"))]]
    independent_ids = _independent_exposure_ids(base_valid)
    input_payload = {
        "boundary": {
            "as_of_root_fact_seq": as_of,
            "catalog_version": catalog_name,
            "version_set_id": version_name,
        },
        "catalog": sorted(
            catalog_sequence,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        ),
        "exposures": records,
    }
    input_snapshot_sha256 = hashlib.sha256(
        json.dumps(
            input_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    exposure_counts = Counter(_strategy_key(record) for record in records)
    catalog_coverage = [
        {
            "strategy_id": _text(entry.get("strategy_id")),
            "strategy_version": _text(entry.get("strategy_version")),
            "baseline": bool(entry.get("baseline")),
            "exposure_n": exposure_counts[
                (
                    _text(entry.get("strategy_id")),
                    _text(entry.get("strategy_version")),
                )
            ],
        }
        for entry in sorted(
            catalog_sequence,
            key=lambda item: (
                _text(item.get("strategy_id")),
                _text(item.get("strategy_version")),
            ),
        )
    ]
    endpoints = {
        endpoint: _build_endpoint_report(
            endpoint,
            records,
            entries=entry_by_exposure,
            base_reasons=reasons_by_exposure,
            independent_ids=independent_ids,
            as_of_root_fact_seq=as_of,
            version_set_id=version_name,
        )
        for endpoint in ENDPOINTS
    }
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "boundary": {
            "as_of_root_fact_seq": as_of,
            "catalog_version": catalog_name,
            "version_set_id": version_name,
        },
        "input": {
            "exposure_n": len(records),
            "independent_exposure_n": len(independent_ids),
            "within_24h_duplicate_n": len(base_valid) - len(independent_ids),
            "snapshot_sha256": input_snapshot_sha256,
            "dedupe_order": ["root_fact_seq", "source_id", "exposure_id"],
        },
        "rounding": {"decimal_places": 6, "mode": "half_up"},
        "minimum_comparison_n": 20,
        "observational_only": True,
        "catalog_coverage": catalog_coverage,
        "endpoints": endpoints,
    }
    report["content_sha256"] = report_digest(report)
    return report


def report_digest(report: Mapping[str, Any]) -> str:
    """Hash report content while excluding its self-referential digest field."""

    payload = copy.deepcopy(dict(report))
    payload.pop("content_sha256", None)
    return hashlib.sha256(canonical_report_json(payload).encode("utf-8")).hexdigest()


def canonical_report_json(report: Mapping[str, Any]) -> str:
    """Serialize a report as canonical, UTF-8-safe and byte-stable JSON text."""

    return json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def render_report_markdown(report: Mapping[str, Any]) -> str:
    """Render a deterministic human-readable view from a completed report."""

    boundary = report["boundary"]
    input_summary = report["input"]
    lines = [
        "# Cognitive strategy attribution",
        "",
        f"- Report version: `{report['report_version']}`",
        f"- Root fact boundary: `{boundary['as_of_root_fact_seq']}`",
        f"- Catalog: `{boundary['catalog_version']}`",
        f"- Version set: `{boundary['version_set_id']}`",
        f"- Content SHA-256: `{report['content_sha256']}`",
        f"- Exposures: {input_summary['exposure_n']}",
        "- Interpretation: observational association only",
        "",
        "## Catalog coverage",
        "",
        "| strategy | baseline | exposures |",
        "| --- | :---: | ---: |",
    ]
    for strategy in report.get("catalog_coverage", ()):
        lines.append(
            f"| {strategy['strategy_id']}@{strategy['strategy_version']} | "
            f"{'yes' if strategy['baseline'] else 'no'} | "
            f"{strategy['exposure_n']} |"
        )
    lines.append("")
    for endpoint in ENDPOINTS:
        payload = report["endpoints"][endpoint]
        counts = payload["status_counts"]
        lines.extend(
            (
                f"## {endpoint.title()}",
                "",
                "| observed | pending | missing | abandoned | replaced | excluded |",
                "| ---: | ---: | ---: | ---: | ---: | ---: |",
                f"| {counts['observed']} | {counts['pending']} | "
                f"{counts['missing_outcome']} | {counts['abandoned']} | "
                f"{counts['replaced']} | {counts['excluded']} |",
                "",
            )
        )
        for stratum in payload["strata"]:
            label = ", ".join(
                f"{field}={stratum['stratum'][field]}" for field in STRATUM_FIELDS
            )
            lines.extend((f"### {label}", ""))
            lines.extend(
                (
                    "| strategy | baseline | eligible | success | rate | Wilson 95% |",
                    "| --- | :---: | ---: | ---: | ---: | --- |",
                )
            )
            for strategy in stratum["strategies"]:
                interval = strategy["wilson_95"]
                wilson = (
                    "—"
                    if interval is None
                    else f"{interval['lower']:.6f}–{interval['upper']:.6f}"
                )
                rate = "—" if strategy["rate"] is None else f"{strategy['rate']:.6f}"
                lines.append(
                    f"| {strategy['strategy_id']}@{strategy['strategy_version']} | "
                    f"{'yes' if strategy['baseline'] else 'no'} | "
                    f"{strategy['eligible_n']} | {strategy['success_n']} | {rate} | {wilson} |"
                )
            lines.extend(("", f"Comparison status: `{stratum['comparison_status']}`", ""))
    return "\n".join(lines).rstrip() + "\n"


# Short alias for callers that already operate in a strategy-report namespace.
build_strategy_report = build_cognitive_strategy_report


__all__ = [
    "CognitiveStrategyReportError",
    "ENDPOINTS",
    "EXCLUSION_REASONS",
    "REPORT_VERSION",
    "STRATUM_FIELDS",
    "build_cognitive_strategy_report",
    "build_strategy_report",
    "canonical_report_json",
    "render_report_markdown",
    "report_digest",
]
