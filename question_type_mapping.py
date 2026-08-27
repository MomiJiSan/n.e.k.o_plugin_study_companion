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
    "unmapped_question_style_metrics",
]
