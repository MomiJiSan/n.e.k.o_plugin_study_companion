"""Deterministically bridge seed teaching styles to executable question types.

Knowledge seeds describe the *teaching style* a topic benefits from (for
example ``几何证明`` or ``程序阅读``).  The question/evaluation pipeline needs a
small, machine-readable format instead.  Keeping the two vocabularies apart
means seed data can remain pedagogical without asking an LLM to invent a
format mapping at generation time.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping

MACHINE_QUESTION_TYPES = frozenset(
    {"short_answer", "math_exact", "math_reasoning"}
)


def _style_key(value: object) -> str:
    return re.sub(r"[\s_-]+", "", str(value or "").strip().casefold())


# These cover the common seed styles.  New seed styles deliberately do not
# silently become a guessed alias: they use a documented subject default and
# are counted below so the mapping can be expanded from real usage.
_STYLE_TO_MACHINE_TYPE = {
    # Compact factual/explanatory responses.
    "概念辨析": "short_answer",
    "conceptcheck": "short_answer",
    "程序阅读": "short_answer",
    "文本分析": "short_answer",
    "规范表达": "short_answer",
    "情境解释": "short_answer",
    "实验分析": "short_answer",
    "信息推断": "short_answer",
    "材料分析": "short_answer",
    "图像分析": "short_answer",
    "语篇理解": "short_answer",
    "表达运用": "short_answer",
    "原因影响": "short_answer",
    "比较题": "short_answer",
    "措施题": "short_answer",
    "阶段特征": "short_answer",
    "分类讨论": "short_answer",
    # Exact calculation formats.
    "基础计算": "math_exact",
    "calculation": "math_exact",
    "appliedcalculation": "math_exact",
    "计算题": "math_exact",
    "概率计算": "math_exact",
    "化简求值": "math_exact",
    "面积计算": "math_exact",
    "比例计算": "math_exact",
    "坐标计算": "math_exact",
    "体积计算": "math_exact",
    "单位换算": "math_exact",
    "读写数": "math_exact",
    # A written derivation, proof, or construction.
    "几何证明": "math_reasoning",
    "证明题": "math_reasoning",
    "算法设计": "math_reasoning",
    "计算推理": "math_reasoning",
    "模型构建": "math_reasoning",
    "建模求解": "math_reasoning",
    "模型分析": "math_reasoning",
    "综合应用": "math_reasoning",
    "实际应用": "math_reasoning",
    "计算应用": "math_reasoning",
    "方法选择": "math_reasoning",
    "逻辑推理": "math_reasoning",
    # PR3: the 20 highest-frequency first-choice styles that previously
    # degraded to a subject fallback in the seed corpus, plus two styles at
    # the frequency cutoff.  The tie-aware cutoff is necessary to reach the
    # <=17% fallback target without adding a broad generalized mapping.  Keep
    # these explicit: a teaching style is pedagogical data, not an LLM-selected
    # output format.
    "模型建构": "math_reasoning",
    "概念推导": "math_reasoning",
    "过程分析": "short_answer",
    "实验探究": "math_reasoning",
    "区位分析": "short_answer",
    "材料题": "short_answer",
    "动点问题": "math_reasoning",
    "区域分析": "short_answer",
    "统计图题": "short_answer",
    "规律探究": "math_reasoning",
    "图形变换": "math_reasoning",
    "数据分析": "math_reasoning",
    "地图题": "short_answer",
    "史实理解": "short_answer",
    "图表读取": "short_answer",
    "空间想象": "math_reasoning",
    "解析式求解": "math_exact",
    "圆锥曲线压轴": "math_reasoning",
    "导数压轴": "math_reasoning",
    "计数问题": "math_reasoning",
    "统计推断": "math_reasoning",
    "空间向量": "math_reasoning",
}

_SUBJECT_DEFAULT_MACHINE_TYPE = {
    "math": "math_reasoning",
    "physics": "math_reasoning",
    "chemistry": "math_reasoning",
}
_DEFAULT_MACHINE_TYPE = "short_answer"
_UNMAPPED_STYLE_COUNTS: Counter[str] = Counter()


@dataclass(frozen=True)
class QuestionTypeMapping:
    """The server-owned choice for one targeted question generation."""

    question_style: str
    machine_question_type: str
    allowed_machine_question_types: tuple[str, ...]
    unmapped_question_style: str = ""

    def to_context(self) -> dict[str, object]:
        return {
            "question_style": self.question_style,
            "required_question_type": self.machine_question_type,
            "allowed_question_types": list(self.allowed_machine_question_types),
            "unmapped_question_style": self.unmapped_question_style,
        }


def resolve_target_question_type(topic: Mapping[str, Any] | None) -> QuestionTypeMapping:
    """Choose the first declared teaching style and map it without LLM input.

    Seed ordering is preserved as the author-provided priority.  A topic with
    an unknown style still receives a stable executable type; its raw style is
    counted for a later, data-backed mapping addition.
    """

    payload = topic if isinstance(topic, Mapping) else {}
    styles = [str(item).strip() for item in payload.get("question_types") or []]
    style = next((item for item in styles if item), "")
    mapped = _STYLE_TO_MACHINE_TYPE.get(_style_key(style)) if style else None
    if mapped in MACHINE_QUESTION_TYPES:
        return QuestionTypeMapping(
            question_style=style,
            machine_question_type=mapped,
            allowed_machine_question_types=(mapped,),
        )

    subject = _style_key(payload.get("subject"))
    machine_type = _SUBJECT_DEFAULT_MACHINE_TYPE.get(subject, _DEFAULT_MACHINE_TYPE)
    metric_key = _style_key(style) or "missing"
    _UNMAPPED_STYLE_COUNTS[metric_key] += 1
    return QuestionTypeMapping(
        question_style=style or "default",
        machine_question_type=machine_type,
        allowed_machine_question_types=(machine_type,),
        unmapped_question_style=style or "missing",
    )


_RETRY_SELECTION_REASONS = frozenset({"retry", "wrong_retry"})


def select_question_style(
    topic: Mapping[str, Any] | None,
    *,
    attempt_count: int,
    selection_reason: str,
    previous_question_style: str = "",
    error_type: str = "",
) -> QuestionTypeMapping:
    """Select a declared teaching style deterministically for one attempt.

    This is deliberately a pure policy function: it does not write attempt
    state, change public payloads, or mutate fallback metrics.  The selected
    raw ``question_style`` remains in :class:`QuestionTypeMapping` and its
    private prompt context, while the executable type remains one of the
    server-owned ``MACHINE_QUESTION_TYPES``.

    A first attempt selects the author-prioritized first style.  Later attempts
    rotate through all declared styles.  Retries avoid the previous style when
    there is another declared option.  Due reviews prefer the first declared
    style that has an explicit short-answer mapping, which is the quickest
    recall format supported by the existing machine question types.
    """

    payload = topic if isinstance(topic, Mapping) else {}
    styles = [
        str(item).strip()
        for item in payload.get("question_types") or []
        if str(item).strip()
    ]
    if not styles:
        return _mapping_for_selected_style(payload, "")

    try:
        normalized_attempt_count = max(0, int(attempt_count))
    except (TypeError, ValueError):
        normalized_attempt_count = 0
    selection_key = str(selection_reason or "").strip().casefold()
    index = normalized_attempt_count % len(styles)

    if selection_key == "due_review":
        rapid_recall_index = next(
            (
                candidate_index
                for candidate_index, style in enumerate(styles)
                if _STYLE_TO_MACHINE_TYPE.get(_style_key(style)) == "short_answer"
            ),
            None,
        )
        if rapid_recall_index is not None:
            index = rapid_recall_index

    # ``error_type`` is intentionally accepted as part of the stable policy
    # input, but selection reason remains the authority for retry semantics.
    # This prevents arbitrary error labels from changing a normal practice or
    # due-review selection.
    _ = error_type
    previous_key = _style_key(previous_question_style)
    if selection_key in _RETRY_SELECTION_REASONS and previous_key:
        index = next(
            (
                candidate_index
                for offset in range(len(styles))
                for candidate_index in [(index + offset) % len(styles)]
                if _style_key(styles[candidate_index]) != previous_key
            ),
            index,
        )

    return _mapping_for_selected_style(payload, styles[index])


def _mapping_for_selected_style(
    topic: Mapping[str, Any], style: str
) -> QuestionTypeMapping:
    """Map one already-selected raw style without mutating policy state."""

    mapped = _STYLE_TO_MACHINE_TYPE.get(_style_key(style)) if style else None
    if mapped in MACHINE_QUESTION_TYPES:
        return QuestionTypeMapping(
            question_style=style,
            machine_question_type=mapped,
            allowed_machine_question_types=(mapped,),
        )

    subject = _style_key(topic.get("subject"))
    machine_type = _SUBJECT_DEFAULT_MACHINE_TYPE.get(subject, _DEFAULT_MACHINE_TYPE)
    return QuestionTypeMapping(
        question_style=style or "default",
        machine_question_type=machine_type,
        allowed_machine_question_types=(machine_type,),
        unmapped_question_style=style or "missing",
    )


def enforce_mapped_question_type(
    payload: Mapping[str, Any] | None, mapping: QuestionTypeMapping | None
) -> dict[str, Any]:
    """Return a candidate payload with the server-selected executable type."""

    result = dict(payload or {})
    if mapping is not None:
        result["question_type"] = mapping.machine_question_type
        result["question_style"] = mapping.question_style
    return result


def unmapped_question_style_metrics() -> dict[str, int]:
    """Return a snapshot of fallback counts without exposing mutable state."""

    return dict(_UNMAPPED_STYLE_COUNTS)


__all__ = [
    "MACHINE_QUESTION_TYPES",
    "QuestionTypeMapping",
    "enforce_mapped_question_type",
    "resolve_target_question_type",
    "select_question_style",
    "unmapped_question_style_metrics",
]
