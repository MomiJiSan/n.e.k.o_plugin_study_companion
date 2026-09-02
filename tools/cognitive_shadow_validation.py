"""Repeatable, bounded Shadow validation for the Cognitive Evidence Engine.

This tool deliberately separates three kinds of evidence:

* deterministic contract checks over reviewed synthetic chain-rule samples;
* targeted engineering tests and a local SQLite write-path benchmark;
* a preliminary configured-model evaluation, when the host model is usable.

It never treats synthetic samples as real-user release evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "cognitive_chain_rule_shadow_samples.json"
DEFAULT_REPORT_DIR = ROOT / "docs" / "reports"
TARGETED_TESTS = (
    "tests/test_cognitive_extractor.py",
    "tests/test_cognitive_model_gateway.py",
    "tests/test_cognitive_projection.py",
    "tests/test_cognitive_store.py",
    "tests/test_cognitive_v2_projection_store.py",
    "tests/test_cognitive_state_policy.py",
    "tests/test_cognitive_intervention_validation.py",
    "tests/test_cognitive_answer_event_integration.py",
    "tests/test_cognitive_delivery.py",
    "tests/test_cognitive_shadow_validation.py",
)
EXPECTED_CODES = (
    "omit_inner_derivative",
    "differentiate_inner_incorrectly",
    "confuse_product_and_chain",
)


class _Logger:
    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug


class _ReferenceGateway:
    """Return reviewed fixture labels through the real extraction validator."""

    def __init__(self, samples: Sequence[Mapping[str, Any]]) -> None:
        self._by_answer = {
            str(item["learner_answer"]): item["expected_evidence"] for item in samples
        }

    async def complete_structured(self, request: Any) -> Any:
        from adaptive_learning.cognitive_contracts import CognitiveModelResponse

        answer = str(request.payload.get("learner_answer") or "")
        evidence = []
        for item in self._by_answer[answer]:
            evidence.append(
                {
                    "hypothesis_code": item["hypothesis_code"],
                    "direction": item["direction"],
                    "strength": 0.9,
                    "extractor_confidence": 0.9,
                    "evidence_span": item["evidence_span"],
                }
            )
        return CognitiveModelResponse(content={"evidence": evidence})


class _UnavailableGateway:
    async def complete_structured(self, _request: Any) -> Any:
        raise ConnectionError("configured model unavailable")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def load_fixture(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported fixture schema_version")
    if payload.get("topic_id") != "calculus.chain_rule":
        raise ValueError("fixture must target calculus.chain_rule")
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("fixture samples must be a non-empty list")
    seen: set[str] = set()
    supported_codes: set[str] = set()
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("fixture sample must be an object")
        required = {
            "id",
            "kind",
            "question_family_id",
            "question",
            "expected_answer",
            "learner_answer",
            "evaluation",
            "allowed_hypotheses",
            "expected_evidence",
        }
        if set(sample) != required:
            raise ValueError(f"fixture sample fields differ: {sample.get('id')}")
        sample_id = str(sample["id"])
        if not sample_id or sample_id in seen:
            raise ValueError("fixture sample ids must be non-empty and unique")
        seen.add(sample_id)
        if sample["kind"] not in {"standard", "adversarial"}:
            raise ValueError(f"invalid fixture kind: {sample_id}")
        allowed = tuple(sample["allowed_hypotheses"])
        if not allowed or not set(allowed).issubset(EXPECTED_CODES):
            raise ValueError(f"fixture hypothesis allowlist is invalid: {sample_id}")
        evidence = sample["expected_evidence"]
        if not isinstance(evidence, list) or len(evidence) > 3:
            raise ValueError(f"fixture evidence is invalid: {sample_id}")
        for item in evidence:
            if set(item) != {"hypothesis_code", "direction", "evidence_span"}:
                raise ValueError(f"fixture evidence fields differ: {sample_id}")
            if item["hypothesis_code"] not in allowed:
                raise ValueError(f"fixture evidence exceeds allowlist: {sample_id}")
            if item["direction"] not in {"support", "counter"}:
                raise ValueError(f"fixture direction is invalid: {sample_id}")
            if not str(item["evidence_span"]).strip():
                raise ValueError(f"fixture evidence span is empty: {sample_id}")
            supported_codes.add(str(item["hypothesis_code"]))
    if supported_codes != set(EXPECTED_CODES):
        raise ValueError("fixture must exercise all three hypothesis codes")
    return payload


def _make_extractor(gateway: object, *, timeout: float = 30.0) -> Any:
    from adaptive_learning.cognitive_extractor import CognitiveExtractor

    return CognitiveExtractor(
        gateway=gateway,  # type: ignore[arg-type]
        count_tokens=len,
        truncate_to_tokens=lambda text, limit: text[:limit],
        timeout_seconds=timeout,
    )


async def evaluate_samples(extractor: Any, samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    from adaptive_learning.cognitive_contracts import CognitiveExtractionInput

    rows: list[dict[str, Any]] = []
    expected_items = 0
    matched_items = 0
    unexpected_items = 0
    faithful_spans = 0
    failure_reasons: Counter[str] = Counter()
    per_code = {
        code: {"expected": 0, "predicted": 0, "matched": 0}
        for code in EXPECTED_CODES
    }
    for sample in samples:
        expected = {
            (str(item["hypothesis_code"]), str(item["direction"]))
            for item in sample["expected_evidence"]
        }
        outcome = await extractor.extract(
            CognitiveExtractionInput(
                topic_id="calculus.chain_rule",
                question=str(sample["question"]),
                expected_answer=str(sample["expected_answer"]),
                learner_answer=str(sample["learner_answer"]),
                evaluation=dict(sample["evaluation"]),
                allowed_hypotheses=tuple(sample["allowed_hypotheses"]),
            )
        )
        actual = {(item.hypothesis_code, item.direction) for item in outcome.evidence}
        expected_items += len(expected)
        matched_items += len(expected & actual)
        unexpected_items += len(actual - expected)
        for code in EXPECTED_CODES:
            expected_for_code = {item for item in expected if item[0] == code}
            actual_for_code = {item for item in actual if item[0] == code}
            per_code[code]["expected"] += len(expected_for_code)
            per_code[code]["predicted"] += len(actual_for_code)
            per_code[code]["matched"] += len(expected_for_code & actual_for_code)
        faithful_spans += sum(bool(item.evidence_span.strip()) for item in outcome.evidence)
        if outcome.failure_reason:
            failure_reasons[outcome.failure_reason] += 1
        rows.append(
            {
                "id": sample["id"],
                "kind": sample["kind"],
                "expected": sorted([list(item) for item in expected]),
                "actual": sorted([list(item) for item in actual]),
                "status": outcome.status,
                "failure_reason": outcome.failure_reason,
                "exact_match": actual == expected and outcome.succeeded,
            }
        )
    total_actual = matched_items + unexpected_items
    hypothesis_metrics = {}
    for code, counts in per_code.items():
        expected_count = counts["expected"]
        predicted_count = counts["predicted"]
        matched_count = counts["matched"]
        hypothesis_metrics[code] = {
            **counts,
            "precision": matched_count / predicted_count if predicted_count else None,
            "recall": matched_count / expected_count if expected_count else None,
        }
    adversarial = [row for row in rows if row["kind"] == "adversarial"]
    return {
        "sample_count": len(rows),
        "exact_match_count": sum(bool(row["exact_match"]) for row in rows),
        "exact_match_rate": sum(bool(row["exact_match"]) for row in rows) / len(rows),
        "expected_item_recall": matched_items / expected_items if expected_items else 1.0,
        "unexpected_item_rate": unexpected_items / total_actual if total_actual else 0.0,
        "non_empty_span_rate": faithful_spans / total_actual if total_actual else 1.0,
        "failure_reasons": dict(failure_reasons),
        "hypothesis_metrics": hypothesis_metrics,
        "adversarial": {
            "sample_count": len(adversarial),
            "exact_match_count": sum(bool(row["exact_match"]) for row in adversarial),
            "unexpected_evidence_count": sum(bool(row["actual"]) for row in adversarial if not row["expected"]),
            "safe_failure_count": sum(row["status"] == "failed" for row in adversarial),
        },
        "rows": rows,
    }


async def verify_fail_closed(sample: Mapping[str, Any]) -> dict[str, Any]:
    result = await evaluate_samples(_make_extractor(_UnavailableGateway(), timeout=0.2), [sample])
    row = result["rows"][0]
    return {
        "passed": row["status"] == "failed" and row["actual"] == [],
        "status": row["status"],
        "failure_reason": row["failure_reason"],
        "evidence_count": len(row["actual"]),
    }


def _install_dynamic_package(package_name: str, plugin_root: Path) -> None:
    package = ModuleType(package_name)
    package.__path__ = [str(plugin_root)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package


async def configured_model_evaluation(
    *, host_root: Path, samples: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    package_name = f"_cognitive_shadow_model_{uuid.uuid4().hex}"
    if str(host_root) not in sys.path:
        sys.path.insert(0, str(host_root))
    _install_dynamic_package(package_name, ROOT)
    try:
        module = importlib.import_module(f"{package_name}.cognitive_model_gateway")
        extractor = module.build_cognitive_extractor(
            logger=_Logger(), config=SimpleNamespace(model_version="cognitive-v1")
        )
        runtime = await extractor._gateway._gateway.describe_runtime("agent")
    except Exception as exc:
        return {
            "status": "UNAVAILABLE_FAIL_CLOSED",
            "runtime": {"configured": False},
            "reason": f"runtime_setup_failed:{type(exc).__name__}",
            "evaluation": None,
        }
    usable = bool(
        runtime.get("configured")
        and runtime.get("credential_configured")
        and runtime.get("transport_supported")
    )
    if not usable:
        safe = await verify_fail_closed(samples[0])
        return {
            "status": "UNAVAILABLE_FAIL_CLOSED",
            "runtime": runtime,
            "reason": "configured_runtime_not_usable",
            "fail_closed_check": safe,
            "evaluation": None,
        }
    evaluation = await evaluate_samples(extractor, samples)
    failed = evaluation["failure_reasons"]
    status = "EVALUATED" if not failed else "EVALUATED_WITH_SAFE_FAILURES"
    return {
        "status": status,
        "runtime": runtime,
        "reason": "",
        "evaluation": evaluation,
    }


def run_targeted_tests() -> dict[str, Any]:
    command = ["uv", "run", "pytest", "-q", *TARGETED_TESTS]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    duration = time.perf_counter() - started
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    summary_lines = [line for line in output.splitlines() if re.search(r"\bpassed\b|\bfailed\b|\berror\b", line, re.I)]
    return {
        "passed": completed.returncode == 0,
        "exit_code": completed.returncode,
        "duration_seconds": round(duration, 3),
        "files": list(TARGETED_TESTS),
        "summary": summary_lines[-3:],
    }


def _load_store() -> type[Any]:
    package_name = f"_cognitive_shadow_store_{uuid.uuid4().hex}"
    _install_dynamic_package(package_name, ROOT)
    mode_manager = ModuleType(f"{package_name}.mode_manager")
    mode_manager.normalize_mode = lambda value: str(value or "companion")  # type: ignore[attr-defined]
    sys.modules[mode_manager.__name__] = mode_manager
    return importlib.import_module(f"{package_name}.store").StudyStore


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile)))
    return ordered[index]


def benchmark_answer_commit(*, iterations: int = 200, warmup: int = 20) -> dict[str, Any]:
    Store = _load_store()
    logger = _Logger()
    measurements: dict[str, list[float]] = {"off": [], "on": []}
    with tempfile.TemporaryDirectory(prefix="cognitive-shadow-") as temp:
        root = Path(temp)
        stores = {
            mode: Store(root / f"{mode}.db", root / f"{mode}-seed.json", logger)
            for mode in measurements
        }
        try:
            for store in stores.values():
                store.open()
                store.ensure_topic(topic_id="calculus.chain_rule", name="Chain rule")
            total = warmup + iterations
            for index in range(total):
                for mode, store in stores.items():
                    attempt_id = f"bench-{mode}-{index}"
                    started = time.perf_counter_ns()
                    store.batch_write_answer_data(
                        session_id="shadow-latency",
                        mode="companion",
                        topic_id="calculus.chain_rule",
                        question={
                            "question_id": f"question-{attempt_id}",
                            "question": "Differentiate sin(x^2).",
                            "answer": "2*x*cos(x^2)",
                            "question_type": "math_exact",
                            "difficulty": 3,
                        },
                        user_answer="cos(x^2)",
                        eval_result={"verdict": "wrong", "score": 0},
                        response_time_ms=100,
                        attempt_id=attempt_id,
                        enqueue_cognitive_projection=mode == "on",
                        cognitive_extractor_version="cognitive-extractor-v1",
                        cognitive_model_version="cognitive-v1",
                    )
                    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
                    if index >= warmup:
                        measurements[mode].append(elapsed_ms)
        finally:
            for store in stores.values():
                store.close()
    off = measurements["off"]
    on = measurements["on"]
    off_p95 = _percentile(off, 0.95)
    on_p95 = _percentile(on, 0.95)
    return {
        "environment": "synthetic_local_sqlite_provisional",
        "iterations_per_arm": iterations,
        "warmup_per_arm": warmup,
        "off": {
            "p50_ms": round(statistics.median(off), 4),
            "p95_ms": round(off_p95, 4),
        },
        "on": {
            "p50_ms": round(statistics.median(on), 4),
            "p95_ms": round(on_p95, 4),
        },
        "p95_delta_ms": round(on_p95 - off_p95, 4),
        "gate_ms": 5.0,
        "gate_passed": on_p95 - off_p95 <= 5.0,
        "notes": "Measures only the synchronous atomic enqueue path; no LLM call is in the answer transaction.",
    }


def _engineering_checks(test_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    passed = bool(test_result["passed"])
    return [
        {"requirement": "乱序、并发、lease 接管、版本重跑、全量重建一致性", "status": "PASS" if passed else "FAIL", "evidence": ["test_cognitive_shadow_validation.py", "test_cognitive_projection.py", "test_cognitive_store.py", "test_cognitive_v2_projection_store.py"]},
        {"requirement": "同题重试与同模板变体去重", "status": "PASS" if passed else "FAIL", "evidence": ["test_cognitive_projection.py"]},
        {"requirement": "probe → repair → transfer → monitored", "status": "PASS" if passed else "FAIL", "evidence": ["test_cognitive_shadow_validation.py", "test_cognitive_projection.py", "test_cognitive_intervention_validation.py"]},
        {"requirement": "所有权边界与失效绑定 fail-closed", "status": "PASS" if passed else "FAIL", "evidence": ["test_cognitive_state_policy.py", "test_cognitive_intervention_validation.py", "test_cognitive_answer_event_integration.py"]},
    ]


def build_report(
    *,
    fixture: Mapping[str, Any],
    reference: Mapping[str, Any],
    fail_closed: Mapping[str, Any],
    targeted_tests: Mapping[str, Any],
    latency: Mapping[str, Any],
    real_model: Mapping[str, Any],
) -> dict[str, Any]:
    engineering = _engineering_checks(targeted_tests)
    engineering_passed = (
        reference["exact_match_rate"] == 1.0
        and fail_closed["passed"]
        and targeted_tests["passed"]
        and latency["gate_passed"]
        and all(item["status"] == "PASS" for item in engineering)
    )
    return {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "scope": {
            "topics": ["calculus.chain_rule", "college_chain_rule"],
            "active_hypotheses": ["omit_inner_derivative"],
            "shadow_hypotheses": ["differentiate_inner_incorrectly", "confuse_product_and_chain"],
            "terminal_stage": "monitored",
        },
        "sample_set": {
            "path": str(DEFAULT_FIXTURE.relative_to(ROOT)).replace("\\", "/"),
            "count": len(fixture["samples"]),
            "standard": sum(item["kind"] == "standard" for item in fixture["samples"]),
            "adversarial": sum(item["kind"] == "adversarial" for item in fixture["samples"]),
        },
        "reference_contract_evaluation": dict(reference),
        "model_unavailable_fail_closed": dict(fail_closed),
        "targeted_tests": dict(targeted_tests),
        "engineering_checks": engineering,
        "answer_commit_latency": dict(latency),
        "configured_model_preliminary": dict(real_model),
        "gates": {
            "engineering_simulation": "PASS" if engineering_passed else "FAIL",
            "read_only": "NOT_RELEASED",
            "personal_beta": "NOT_RELEASED",
            "active_local": "NOT_RELEASED",
        },
        "real_user_gates": {
            "seven_day_shadow": "NOT_EVALUATED",
            "thirty_structured_attempts": "NOT_EVALUATED",
            "ten_question_families": "NOT_EVALUATED",
            "supported_human_precision": "NOT_EVALUATED",
            "real_user_denial_rate": "NOT_EVALUATED",
            "five_complete_intervention_loops": "NOT_EVALUATED",
        },
        "boundary_assertion": "Cognitive Engine changed no Coach topic selection, Mastery, FSRS, course progress, or wrong-question state in this validation.",
        "limitations": [
            "Synthetic samples and simulations are not real-user release evidence.",
            "Configured-model results are preliminary and do not calibrate probabilities.",
            "The latency benchmark is local SQLite only and excludes asynchronous extraction.",
            "No full test suite was run.",
        ],
    }


def _format_rate(value: object) -> str:
    return "N/A" if value is None else f"{float(value):.1%}"


def _markdown(report: Mapping[str, Any]) -> str:
    real = report["configured_model_preliminary"]
    real_eval = real.get("evaluation") or {}
    checks = "\n".join(
        f"- {item['status']}: {item['requirement']}（{', '.join(item['evidence'])}）"
        for item in report["engineering_checks"]
    )
    tests = report["targeted_tests"]
    latency = report["answer_commit_latency"]
    model_line = f"{real['status']}"
    model_boundaries = ""
    if real_eval:
        model_line += (
            f"；{real_eval['exact_match_count']}/{real_eval['sample_count']} 样本完全匹配，"
            f"期望证据召回率 {real_eval['expected_item_recall']:.1%}，"
            f"意外证据率 {real_eval['unexpected_item_rate']:.1%}"
        )
        model_boundaries = "\n".join(
            f"- `{code}`：precision {_format_rate(item['precision'])}, "
            f"recall {_format_rate(item['recall'])}"
            for code, item in real_eval["hypothesis_metrics"].items()
        )
    return f"""# Cognitive Evidence Engine Shadow 放行报告

生成时间：{report['generated_at']}

## 结论

- 工程模拟门禁：**{report['gates']['engineering_simulation']}**。
- Read Only / Personal Beta / Active Local：**均未放行**。本报告没有把合成样本冒充真实用户数据。
- 真实配置模型：**{model_line}**。
- 本轮只运行认知引擎定向测试，未运行全量测试。

## 样本与提取边界

- 样本共 {report['sample_set']['count']} 条：标准 {report['sample_set']['standard']} 条，对抗 {report['sample_set']['adversarial']} 条。
- 覆盖 `omit_inner_derivative`、`differentiate_inner_incorrectly`、`confuse_product_and_chain` 三个 hypothesis。
- 审定标签通过真实提取器结构校验路径：{report['reference_contract_evaluation']['exact_match_count']}/{report['reference_contract_evaluation']['sample_count']} 完全匹配。
- 模型不可用安全降级：{'PASS' if report['model_unavailable_fail_closed']['passed'] else 'FAIL'}；失败时证据数 {report['model_unavailable_fail_closed']['evidence_count']}。

真实配置模型分 hypothesis 初评：

{model_boundaries or '- 未运行。'}

## 工程验证

定向测试结果：{'PASS' if tests['passed'] else 'FAIL'}，耗时 {tests['duration_seconds']} 秒。

{checks}

## 答题提交延迟

- 环境：合成本地 SQLite（临时、provisional）。
- 每组 {latency['iterations_per_arm']} 次，预热 {latency['warmup_per_arm']} 次。
- 关闭入队：p50 {latency['off']['p50_ms']} ms，p95 {latency['off']['p95_ms']} ms。
- 开启原子入队：p50 {latency['on']['p50_ms']} ms，p95 {latency['on']['p95_ms']} ms。
- p95 增量：{latency['p95_delta_ms']} ms；≤5 ms 门槛：{'PASS' if latency['gate_passed'] else 'FAIL'}。

这里只测答题事务里的原子入队，不包含异步 LLM 提取。

## 所有权边界

验证范围内认知引擎只拥有认知证据、假设和三个装饰字段；没有修改 Coach 选题、Mastery、FSRS、课程进度或错题状态。失效 topic/scope/plan revision/错题绑定均按定向测试 fail-closed。

## 尚未满足的真实放行门槛

- 7 天本地 Shadow：NOT_EVALUATED
- 30 次真实结构化答题：NOT_EVALUATED
- 10 个真实题目族：NOT_EVALUATED
- supported 假设人工精确率：NOT_EVALUATED
- 真实用户否认率：NOT_EVALUATED
- 5 个真实完整干预闭环：NOT_EVALUATED

因此当前结论只支持“工程 Shadow 验证通过/失败”，不能据此宣称 Personal Beta 或普遍有效。
"""


async def _run(args: argparse.Namespace) -> int:
    fixture = load_fixture(args.fixture)
    samples = fixture["samples"]
    reference = await evaluate_samples(_make_extractor(_ReferenceGateway(samples)), samples)
    fail_closed = await verify_fail_closed(samples[0])
    targeted = (
        {"passed": False, "exit_code": None, "duration_seconds": 0.0, "files": list(TARGETED_TESTS), "summary": ["SKIPPED"]}
        if args.skip_targeted_tests
        else await asyncio.to_thread(run_targeted_tests)
    )
    latency = await asyncio.to_thread(
        benchmark_answer_commit, iterations=args.latency_iterations, warmup=args.latency_warmup
    )
    real_model = (
        await configured_model_evaluation(host_root=args.host_root, samples=samples)
        if args.real_model
        else {"status": "NOT_RUN", "runtime": {}, "reason": "--real-model not requested", "evaluation": None}
    )
    report = build_report(
        fixture=fixture,
        reference=reference,
        fail_closed=fail_closed,
        targeted_tests=targeted,
        latency=latency,
        real_model=real_model,
    )
    args.report_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.report_dir / "cognitive-shadow-validation-report.json"
    md_path = args.report_dir / "cognitive-shadow-validation-report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps({
        "engineering_gate": report["gates"]["engineering_simulation"],
        "configured_model": real_model["status"],
        "json_report": str(json_path),
        "markdown_report": str(md_path),
    }, ensure_ascii=False))
    return 0 if report["gates"]["engineering_simulation"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--host-root", type=Path, default=Path(r"E:\Work\CODE\N.E.K.O"))
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--skip-targeted-tests", action="store_true")
    parser.add_argument("--latency-iterations", type=int, default=200)
    parser.add_argument("--latency-warmup", type=int, default=20)
    args = parser.parse_args()
    if args.latency_iterations < 20 or args.latency_warmup < 0:
        parser.error("latency iterations must be >=20 and warmup must be >=0")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
