"""Audit deterministic V4 teaching coverage against the bundled graph seed."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adaptive_learning.cognitive_catalog import (  # noqa: E402
    COGNITIVE_CATALOG_V2,
    KNOWLEDGE_GRAPH_HYPOTHESES,
    build_knowledge_graph_cognitive_catalog,
)
from adaptive_learning.cognitive_versions import (  # noqa: E402
    COMPREHENSIVE_TEACHING_COVERAGE_VERSION_SET,
    get_cognitive_version_set,
)

REPORT_JSON = "cognitive-v4-teaching-coverage.json"
REPORT_MARKDOWN = "cognitive-v4-teaching-coverage.md"
EXPECTED_SUBJECTS = frozenset(
    {
        "biology",
        "chemistry",
        "chinese",
        "computer_science",
        "economics",
        "english",
        "geography",
        "history",
        "math",
        "physics",
        "politics",
    }
)
EXPECTED_INTENTS = frozenset(
    {"misconception_probe", "misconception_repair", "transfer_check"}
)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid JSON object: {path}")
    return payload


def load_topics(static_root: Path) -> tuple[dict[str, dict[str, Any]], tuple[Path, ...]]:
    manifest_path = static_root / "knowledge_graph_seed.json"
    manifest = _read_json(manifest_path)
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("knowledge seed manifest files are required")
    topics: dict[str, dict[str, Any]] = {}
    source_paths = [manifest_path]
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("knowledge seed manifest entry is invalid")
        path = static_root / item["path"]
        source_paths.append(path)
        payload = _read_json(path)
        raw_topics = payload.get("topics")
        if not isinstance(raw_topics, list):
            raise ValueError(f"knowledge seed topics are required: {path}")
        for topic in raw_topics:
            if not isinstance(topic, dict):
                raise ValueError(f"knowledge seed topic is invalid: {path}")
            topic_id = str(topic.get("id") or "").strip()
            if not topic_id or topic_id in topics:
                raise ValueError(f"duplicate or empty knowledge topic: {topic_id}")
            topics[topic_id] = topic
    return topics, tuple(source_paths)


def _source_digest(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def build_report(static_root: Path) -> dict[str, Any]:
    topics, source_paths = load_topics(static_root)
    versions = get_cognitive_version_set(
        COMPREHENSIVE_TEACHING_COVERAGE_VERSION_SET
    )
    if versions is None or versions.catalog_version != "cognitive-catalog-v3":
        raise ValueError("comprehensive teaching version is not registered")
    catalog = build_knowledge_graph_cognitive_catalog(
        topics.get,
        base_catalog=COGNITIVE_CATALOG_V2,
        comprehensive_teaching=True,
    )

    subjects = Counter(str(topic.get("subject") or "").strip() for topic in topics.values())
    if frozenset(subjects) != EXPECTED_SUBJECTS:
        raise ValueError("bundled subject coverage is incomplete")

    hypothesis_counts: Counter[str] = Counter()
    generic_codes = {item.code for item in KNOWLEDGE_GRAPH_HYPOTHESES}
    blueprint_count = 0
    fully_covered_topics = 0
    covered_topic_count = 0
    for topic_id in sorted(topics):
        active_codes = catalog.active_codes(topic_id)
        if not active_codes:
            raise ValueError(f"topic has no active teaching path: {topic_id}")
        covered_topic_count += 1
        if len(active_codes) == 3:
            fully_covered_topics += 1
        for code in active_codes:
            hypothesis_counts[code] += 1
            blueprints = catalog.blueprints(topic_id, hypothesis_code=code)
            if frozenset(item.learning_intent for item in blueprints) != EXPECTED_INTENTS:
                raise ValueError(f"topic/hypothesis lacks three intents: {topic_id}/{code}")
            if (
                code in generic_codes
                and len({item.question_family_id for item in blueprints}) != 3
            ):
                raise ValueError(f"question families are not independent: {topic_id}/{code}")
            if any(catalog.get_blueprint(item.blueprint_id) != item for item in blueprints):
                raise ValueError(f"blueprint lookup is not deterministic: {topic_id}/{code}")
            blueprint_count += len(blueprints)

    reviewed_blueprints = len(COGNITIVE_CATALOG_V2.blueprints("calculus.chain_rule"))
    generic_pairs = sum(hypothesis_counts[code] for code in generic_codes)
    return {
        "schema_version": "cognitive-teaching-coverage-report-v1",
        "status": "PASS",
        "version_set": COMPREHENSIVE_TEACHING_COVERAGE_VERSION_SET,
        "catalog_version": versions.catalog_version,
        "validator_version": versions.validator_version,
        "source_digest": _source_digest(source_paths),
        "source_file_count": len(source_paths),
        "subject_count": len(subjects),
        "topic_count": len(topics),
        "covered_topic_count": covered_topic_count,
        "fully_covered_topic_count": fully_covered_topics,
        "active_topic_hypothesis_count": sum(hypothesis_counts.values()),
        "generic_topic_hypothesis_count": generic_pairs,
        "reviewed_blueprint_count": reviewed_blueprints,
        "deterministic_graph_blueprint_count": blueprint_count - reviewed_blueprints,
        "blueprint_count": blueprint_count,
        "hypothesis_topic_counts": dict(sorted(hypothesis_counts.items())),
        "subject_topic_counts": dict(sorted(subjects.items())),
        "default_release_unchanged": True,
        "retention_expanded": False,
        "personalization_expanded": False,
        "human_effectiveness_evaluated": False,
    }


def render_markdown(report: dict[str, Any]) -> str:
    subject_rows = "\n".join(
        f"| {subject} | {count} |"
        for subject, count in report["subject_topic_counts"].items()
    )
    hypothesis_rows = "\n".join(
        f"| `{code}` | {count} |"
        for code, count in report["hypothesis_topic_counts"].items()
    )
    return f"""# V4 全面教学覆盖确定性验收

- 状态：**{report['status']}**
- 版本集：`{report['version_set']}`
- 目录版本：`{report['catalog_version']}`
- 知识种子摘要：`{report['source_digest']}`

## 覆盖结果

- 学科：{report['subject_count']}
- 知识点：{report['covered_topic_count']} / {report['topic_count']}
- 同时具备三类错因的知识点：{report['fully_covered_topic_count']}
- 知识点 × 错因：{report['active_topic_hypothesis_count']}
- 诊断/纠偏/迁移蓝图：{report['blueprint_count']}
- 其中固定审定蓝图：{report['reviewed_blueprint_count']}
- 其中知识种子确定性蓝图：{report['deterministic_graph_blueprint_count']}

| 学科 | 知识点 |
|---|---:|
{subject_rows}

| 错因 | 覆盖知识点 |
|---|---:|
{hypothesis_rows}

## 边界

发布默认版本与开关保持不变。全面覆盖仍需显式选择版本并开启认知门禁；本批次不扩大 retention 或 V3 个性化，不代表真人教学效果已经验证。
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--static-root",
        type=Path,
        default=ROOT / "static",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=ROOT / "docs" / "reports" / REPORT_JSON,
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=ROOT / "docs" / "reports" / REPORT_MARKDOWN,
    )
    args = parser.parse_args()
    report = build_report(args.static_root)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    args.markdown_output.write_text(
        render_markdown(report),
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
