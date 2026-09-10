"""Closed-world misconception and intervention catalog.

The catalog is the reviewed source of truth for V2 teaching mechanics.  A
model may paraphrase a prompt, but it cannot add hypotheses, select a repair
strategy, or change the protected mathematical and diagnostic signatures.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Literal, Mapping

from .contracts import LearningIntent, RepairStrategy

CHAIN_RULE_TOPIC_ID = "calculus.chain_rule"
COLLEGE_CHAIN_RULE_TOPIC_ID = "college_chain_rule"

HypothesisAvailability = Literal["shadow", "active"]


@dataclass(frozen=True, slots=True)
class CognitiveHypothesisSpec:
    """Stable model-facing definition of one falsifiable error mechanism."""

    topic_id: str
    code: str
    description: str
    availability: HypothesisAvailability = "shadow"
    competing_hypothesis_codes: tuple[str, ...] = ()

    def to_model_payload(self) -> dict[str, str]:
        return {
            "code": self.code,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class CognitiveQuestionBlueprint:
    """Human-reviewed, immutable mechanics for one cognitive question.

    ``question_text`` may be paraphrased by a generator.  Every other field is
    protected and must match exactly before a cognitive question can be used.
    """

    blueprint_id: str
    topic_id: str
    hypothesis_code: str
    learning_intent: LearningIntent
    repair_strategy: RepairStrategy
    question_family_id: str
    question_text: str
    math_expression: str
    expected_answer: str
    diagnostic_signature: str
    competing_hypothesis_codes: tuple[str, ...]


class CognitiveCatalog:
    """Immutable lookup catalog; model output cannot extend it at runtime."""

    def __init__(
        self,
        hypotheses: tuple[CognitiveHypothesisSpec, ...],
        *,
        topic_aliases: Mapping[str, str] | None = None,
        question_blueprints: tuple[CognitiveQuestionBlueprint, ...] = (),
    ) -> None:
        by_topic: dict[str, dict[str, CognitiveHypothesisSpec]] = {}
        for hypothesis in hypotheses:
            topic_id = hypothesis.topic_id.strip()
            code = hypothesis.code.strip()
            if not topic_id or not code:
                raise ValueError("cognitive hypothesis topic_id and code are required")
            topic = by_topic.setdefault(topic_id, {})
            if code in topic:
                raise ValueError(f"duplicate cognitive hypothesis: {topic_id}/{code}")
            if hypothesis.availability not in {"shadow", "active"}:
                raise ValueError("invalid cognitive hypothesis availability")
            topic[code] = hypothesis
        self._by_topic: Mapping[str, Mapping[str, CognitiveHypothesisSpec]] = (
            MappingProxyType(
                {
                    topic_id: MappingProxyType(dict(topic))
                    for topic_id, topic in by_topic.items()
                }
            )
        )
        aliases = dict(topic_aliases or {})
        for alias, canonical in aliases.items():
            if not alias.strip() or canonical not in self._by_topic:
                raise ValueError("cognitive topic alias must target a catalog topic")
            if alias in self._by_topic or alias == canonical:
                raise ValueError("cognitive topic alias must be distinct")
        self._topic_aliases: Mapping[str, str] = MappingProxyType(aliases)

        for topic_id, topic in self._by_topic.items():
            for hypothesis in topic.values():
                competitors = hypothesis.competing_hypothesis_codes
                if hypothesis.code in competitors or any(
                    competitor not in topic for competitor in competitors
                ):
                    raise ValueError(
                        f"invalid competing hypothesis for {topic_id}/{hypothesis.code}"
                    )

        by_blueprint: dict[str, CognitiveQuestionBlueprint] = {}
        blueprints_by_topic: dict[str, list[CognitiveQuestionBlueprint]] = {}
        for blueprint in question_blueprints:
            if not blueprint.blueprint_id.strip():
                raise ValueError("cognitive blueprint_id is required")
            if blueprint.blueprint_id in by_blueprint:
                raise ValueError(
                    f"duplicate cognitive blueprint: {blueprint.blueprint_id}"
                )
            canonical = self.canonical_topic_id(blueprint.topic_id)
            topic = self._by_topic.get(canonical) if canonical is not None else None
            if topic is None or blueprint.hypothesis_code not in topic:
                raise ValueError("cognitive blueprint must target a known hypothesis")
            hypothesis = topic[blueprint.hypothesis_code]
            if hypothesis.availability != "active":
                raise ValueError("cognitive blueprint must target an active hypothesis")
            if blueprint.learning_intent not in {
                "misconception_probe",
                "misconception_repair",
                "transfer_check",
            }:
                raise ValueError("cognitive blueprint has an unsupported V2 intent")
            if not all(
                (
                    blueprint.repair_strategy,
                    blueprint.question_family_id.strip(),
                    blueprint.math_expression.strip(),
                    blueprint.expected_answer.strip(),
                    blueprint.diagnostic_signature.strip(),
                )
            ):
                raise ValueError("cognitive blueprint protected fields are required")
            if (
                not blueprint.competing_hypothesis_codes
                or blueprint.hypothesis_code
                in blueprint.competing_hypothesis_codes
                or any(
                    competitor not in topic
                    for competitor in blueprint.competing_hypothesis_codes
                )
            ):
                raise ValueError("cognitive blueprint competitors are invalid")
            by_blueprint[blueprint.blueprint_id] = blueprint
            blueprints_by_topic.setdefault(canonical, []).append(blueprint)
        self._by_blueprint: Mapping[str, CognitiveQuestionBlueprint] = (
            MappingProxyType(by_blueprint)
        )
        self._blueprints_by_topic: Mapping[
            str, tuple[CognitiveQuestionBlueprint, ...]
        ] = MappingProxyType(
            {
                topic_id: tuple(blueprints)
                for topic_id, blueprints in blueprints_by_topic.items()
            }
        )

    def canonical_topic_id(self, topic_id: str) -> str | None:
        normalized = str(topic_id or "").strip()
        if normalized in self._by_topic:
            return normalized
        return self._topic_aliases.get(normalized)

    def supports_topic(self, topic_id: str) -> bool:
        return self.canonical_topic_id(topic_id) is not None

    def allowed_codes(self, topic_id: str) -> tuple[str, ...]:
        canonical = self.canonical_topic_id(topic_id)
        topic = self._by_topic.get(canonical) if canonical is not None else None
        return tuple(topic) if topic is not None else ()

    def get(
        self, topic_id: str, hypothesis_code: str
    ) -> CognitiveHypothesisSpec | None:
        canonical = self.canonical_topic_id(topic_id)
        topic = self._by_topic.get(canonical) if canonical is not None else None
        if topic is None:
            return None
        return topic.get(str(hypothesis_code or "").strip())

    def hypotheses(
        self, topic_id: str, allowed_codes: tuple[str, ...] | None = None
    ) -> tuple[CognitiveHypothesisSpec, ...]:
        canonical = self.canonical_topic_id(topic_id)
        topic = self._by_topic.get(canonical) if canonical is not None else None
        if topic is None:
            return ()
        codes = tuple(topic) if allowed_codes is None else allowed_codes
        return tuple(topic[code] for code in codes if code in topic)

    def is_active(self, topic_id: str, hypothesis_code: str) -> bool:
        hypothesis = self.get(topic_id, hypothesis_code)
        return hypothesis is not None and hypothesis.availability == "active"

    def active_codes(self, topic_id: str) -> tuple[str, ...]:
        return tuple(
            hypothesis.code
            for hypothesis in self.hypotheses(topic_id)
            if hypothesis.availability == "active"
        )

    def get_blueprint(self, blueprint_id: str) -> CognitiveQuestionBlueprint | None:
        return self._by_blueprint.get(str(blueprint_id or "").strip())

    def blueprints(
        self,
        topic_id: str,
        *,
        hypothesis_code: str = "",
        learning_intent: LearningIntent | None = None,
    ) -> tuple[CognitiveQuestionBlueprint, ...]:
        canonical = self.canonical_topic_id(topic_id)
        if canonical is None:
            return ()
        return tuple(
            blueprint
            for blueprint in self._blueprints_by_topic.get(canonical, ())
            if (not hypothesis_code or blueprint.hypothesis_code == hypothesis_code)
            and (learning_intent is None or blueprint.learning_intent == learning_intent)
        )


KNOWLEDGE_GRAPH_HYPOTHESES = (
    CognitiveHypothesisSpec(
        topic_id="__knowledge_graph__",
        code="concept_misunderstanding",
        description=(
            "The learner applies an incorrect definition, rule, or conceptual "
            "relationship for the target knowledge-graph topic. Compare the "
            "answer with topic_context.typical_misconceptions when available."
        ),
    ),
    CognitiveHypothesisSpec(
        topic_id="__knowledge_graph__",
        code="prerequisite_gap",
        description=(
            "The error is better explained by a missing or confused prerequisite "
            "listed in topic_context.prerequisites than by the target concept itself."
        ),
    ),
    CognitiveHypothesisSpec(
        topic_id="__knowledge_graph__",
        code="procedure_or_representation_error",
        description=(
            "The learner appears to understand the target concept but makes a "
            "procedural, conversion, simplification, notation, or representation error."
        ),
    ),
)


class KnowledgeGraphCognitiveCatalog(CognitiveCatalog):
    """Extend the reviewed catalog only to topics verified by the graph store.

    Graph topics receive three conservative shadow-only mechanisms.  Reviewed
    topic-specific hypotheses and intervention blueprints remain authoritative,
    so this adapter cannot activate generic teaching interventions.
    """

    def __init__(
        self,
        base_catalog: CognitiveCatalog,
        topic_provider: Callable[[str], Mapping[str, Any] | None],
    ) -> None:
        self._base_catalog = base_catalog
        self._topic_provider = topic_provider
        self._generic_by_code = MappingProxyType(
            {item.code: item for item in KNOWLEDGE_GRAPH_HYPOTHESES}
        )

    def canonical_topic_id(self, topic_id: str) -> str | None:
        normalized = str(topic_id or "").strip()
        canonical = self._base_catalog.canonical_topic_id(normalized)
        if canonical is not None:
            return canonical
        if not normalized:
            return None
        try:
            topic = self._topic_provider(normalized)
        except Exception:
            return None
        if not isinstance(topic, Mapping):
            return None
        return normalized if str(topic.get("id") or "").strip() == normalized else None

    def supports_topic(self, topic_id: str) -> bool:
        return self.canonical_topic_id(topic_id) is not None

    def is_graph_topic(self, topic_id: str) -> bool:
        normalized = str(topic_id or "").strip()
        if not normalized:
            return False
        try:
            topic = self._topic_provider(normalized)
        except Exception:
            return False
        return bool(
            isinstance(topic, Mapping)
            and str(topic.get("id") or "").strip() == normalized
        )

    def allowed_codes(self, topic_id: str) -> tuple[str, ...]:
        if self._base_catalog.supports_topic(topic_id):
            return self._base_catalog.allowed_codes(topic_id)
        return tuple(self._generic_by_code) if self.supports_topic(topic_id) else ()

    def get(
        self, topic_id: str, hypothesis_code: str
    ) -> CognitiveHypothesisSpec | None:
        if self._base_catalog.supports_topic(topic_id):
            return self._base_catalog.get(topic_id, hypothesis_code)
        canonical = self.canonical_topic_id(topic_id)
        generic = self._generic_by_code.get(str(hypothesis_code or "").strip())
        if canonical is None or generic is None:
            return None
        return replace(generic, topic_id=canonical)

    def hypotheses(
        self, topic_id: str, allowed_codes: tuple[str, ...] | None = None
    ) -> tuple[CognitiveHypothesisSpec, ...]:
        if self._base_catalog.supports_topic(topic_id):
            return self._base_catalog.hypotheses(topic_id, allowed_codes)
        canonical = self.canonical_topic_id(topic_id)
        if canonical is None:
            return ()
        codes = tuple(self._generic_by_code) if allowed_codes is None else allowed_codes
        return tuple(
            replace(self._generic_by_code[code], topic_id=canonical)
            for code in codes
            if code in self._generic_by_code
        )

    def is_active(self, topic_id: str, hypothesis_code: str) -> bool:
        return self._base_catalog.is_active(topic_id, hypothesis_code)

    def active_codes(self, topic_id: str) -> tuple[str, ...]:
        return self._base_catalog.active_codes(topic_id)

    def get_blueprint(self, blueprint_id: str) -> CognitiveQuestionBlueprint | None:
        return self._base_catalog.get_blueprint(blueprint_id)

    def blueprints(
        self,
        topic_id: str,
        *,
        hypothesis_code: str = "",
        learning_intent: LearningIntent | None = None,
    ) -> tuple[CognitiveQuestionBlueprint, ...]:
        return self._base_catalog.blueprints(
            topic_id,
            hypothesis_code=hypothesis_code,
            learning_intent=learning_intent,
        )


_GRAPH_TEACHING_INTENTS: tuple[LearningIntent, ...] = (
    "misconception_probe",
    "misconception_repair",
    "transfer_check",
)
_GRAPH_TEACHING_STRATEGIES: Mapping[
    tuple[str, LearningIntent], RepairStrategy
] = MappingProxyType(
    {
        ("concept_misunderstanding", "misconception_probe"): "structure_classification",
        ("concept_misunderstanding", "misconception_repair"): "compare_steps",
        ("concept_misunderstanding", "transfer_check"): "cross_form_transfer",
        ("prerequisite_gap", "misconception_probe"): "structure_classification",
        ("prerequisite_gap", "misconception_repair"): "minimal_change",
        ("prerequisite_gap", "transfer_check"): "cross_form_transfer",
        (
            "procedure_or_representation_error",
            "misconception_probe",
        ): "compare_steps",
        (
            "procedure_or_representation_error",
            "misconception_repair",
        ): "minimal_change",
        (
            "procedure_or_representation_error",
            "transfer_check",
        ): "cross_form_transfer",
    }
)


def _text_items(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        text
        for item in value
        if (text := str(item or "").strip())
    )


def _mapping_items(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


class ComprehensiveKnowledgeGraphCognitiveCatalog(KnowledgeGraphCognitiveCatalog):
    """Activate deterministic teaching contracts backed by complete graph metadata."""

    def _topic(self, topic_id: str) -> Mapping[str, Any] | None:
        canonical = self.canonical_topic_id(topic_id)
        if canonical is None or self._base_catalog.supports_topic(canonical):
            return None
        try:
            topic = self._topic_provider(canonical)
        except Exception:
            return None
        if not isinstance(topic, Mapping):
            return None
        return topic if str(topic.get("id") or "").strip() == canonical else None

    @staticmethod
    def _example(topic: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return next(
            (
                item
                for item in _mapping_items(topic.get("examples"))
                if str(item.get("prompt") or "").strip()
                and _text_items(item.get("answer_outline"))
            ),
            None,
        )

    @staticmethod
    def _prerequisite(topic: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return next(
            (
                item
                for item in _mapping_items(topic.get("prerequisites"))
                if str(item.get("id") or "").strip()
                and str(item.get("reason") or "").strip()
            ),
            None,
        )

    @staticmethod
    def _related(topic: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return next(
            (
                item
                for item in _mapping_items(topic.get("related"))
                if str(item.get("id") or "").strip()
                and str(item.get("reason") or "").strip()
            ),
            None,
        )

    def _active_graph_codes(self, topic_id: str) -> tuple[str, ...]:
        topic = self._topic(topic_id)
        if topic is None or not str(topic.get("name") or "").strip():
            return ()
        if self._example(topic) is None or self._related(topic) is None:
            return ()
        active: list[str] = []
        if _text_items(topic.get("typical_misconceptions")):
            active.append("concept_misunderstanding")
        if self._prerequisite(topic) is not None:
            active.append("prerequisite_gap")
        if len(_text_items(topic.get("skills"))) >= 2:
            active.append("procedure_or_representation_error")
        return tuple(active)

    def get(
        self, topic_id: str, hypothesis_code: str
    ) -> CognitiveHypothesisSpec | None:
        hypothesis = super().get(topic_id, hypothesis_code)
        if hypothesis is None:
            return None
        if hypothesis.code in self._active_graph_codes(topic_id):
            competitors = tuple(
                code for code in self._generic_by_code if code != hypothesis.code
            )
            return replace(
                hypothesis,
                availability="active",
                competing_hypothesis_codes=competitors,
            )
        return hypothesis

    def hypotheses(
        self, topic_id: str, allowed_codes: tuple[str, ...] | None = None
    ) -> tuple[CognitiveHypothesisSpec, ...]:
        if self._base_catalog.supports_topic(topic_id):
            return self._base_catalog.hypotheses(topic_id, allowed_codes)
        codes = self.allowed_codes(topic_id) if allowed_codes is None else allowed_codes
        return tuple(
            hypothesis
            for code in codes
            if (hypothesis := self.get(topic_id, code)) is not None
        )

    def is_active(self, topic_id: str, hypothesis_code: str) -> bool:
        hypothesis = self.get(topic_id, hypothesis_code)
        return hypothesis is not None and hypothesis.availability == "active"

    def active_codes(self, topic_id: str) -> tuple[str, ...]:
        if self._base_catalog.supports_topic(topic_id):
            return self._base_catalog.active_codes(topic_id)
        return self._active_graph_codes(topic_id)

    def _topic_name(self, topic_id: str) -> str:
        try:
            topic = self._topic_provider(topic_id)
        except Exception:
            topic = None
        if isinstance(topic, Mapping):
            name = str(topic.get("name") or "").strip()
            if name:
                return name
        return topic_id

    def _blueprint(
        self,
        topic_id: str,
        hypothesis_code: str,
        learning_intent: LearningIntent,
    ) -> CognitiveQuestionBlueprint | None:
        if (
            learning_intent not in _GRAPH_TEACHING_INTENTS
            or hypothesis_code not in self._active_graph_codes(topic_id)
        ):
            return None
        topic = self._topic(topic_id)
        if topic is None:
            return None
        example = self._example(topic)
        related = self._related(topic)
        if example is None or related is None:
            return None
        name = str(topic.get("name") or "").strip()
        prompt = str(example.get("prompt") or "").strip()
        outline = "；".join(_text_items(example.get("answer_outline")))
        misconceptions = _text_items(topic.get("typical_misconceptions"))
        skills = _text_items(topic.get("skills"))
        misconception = misconceptions[0] if misconceptions else "未核对核心条件"
        skill = skills[0] if skills else f"解释{name}的核心条件"
        second_skill = skills[1] if len(skills) > 1 else skill
        related_id = str(related.get("id") or "").strip()
        related_name = self._topic_name(related_id)
        related_reason = str(related.get("reason") or "").strip()
        prerequisite = self._prerequisite(topic)

        if hypothesis_code == "concept_misunderstanding":
            if learning_intent == "misconception_probe":
                question_text = (
                    f"围绕“{name}”完成诊断题：{prompt} "
                    "作答时明确核心概念、适用条件和判断依据。"
                )
                expected_answer = f"需排除误区：{misconception}；答案要点：{outline}"
            elif learning_intent == "misconception_repair":
                question_text = (
                    f"常见错误是“{misconception}”。请先说明错在哪里，再重新完成：{prompt}"
                )
                expected_answer = f"纠正：{misconception}；答案要点：{outline}"
            else:
                question_text = (
                    f"把“{name}”迁移到关联知识“{related_name}”："
                    f"{related_reason} 请解释两者关系并给出判断依据。"
                )
                expected_answer = f"关联依据：{related_reason}；原知识要点：{skill}"
        elif hypothesis_code == "prerequisite_gap":
            if prerequisite is None:
                return None
            prerequisite_id = str(prerequisite.get("id") or "").strip()
            prerequisite_name = self._topic_name(prerequisite_id)
            prerequisite_reason = str(prerequisite.get("reason") or "").strip()
            if learning_intent == "misconception_probe":
                question_text = (
                    f"在完成“{name}”前，先说明先修知识“{prerequisite_name}”为什么必要，"
                    f"再指出它会用于题目中的哪一步：{prompt}"
                )
                expected_answer = (
                    f"先修知识：{prerequisite_name}；必要性：{prerequisite_reason}"
                )
            elif learning_intent == "misconception_repair":
                question_text = (
                    f"先用一句话补齐“{prerequisite_name}”与“{name}”的联系，"
                    f"再按关键步骤完成：{prompt}"
                )
                expected_answer = (
                    f"先修联系：{prerequisite_reason}；答案要点：{outline}"
                )
            else:
                question_text = (
                    f"把已补齐的先修关系迁移到“{related_name}”：{related_reason} "
                    "说明应先调用哪项基础知识以及理由。"
                )
                expected_answer = (
                    f"先修知识：{prerequisite_name}；迁移依据：{related_reason}"
                )
        else:
            if learning_intent == "misconception_probe":
                question_text = (
                    f"完成“{name}”的步骤诊断：{prompt} "
                    f"请按“{skill}”逐步作答，不要只写结论。"
                )
                expected_answer = f"步骤要求：{skill}；答案要点：{outline}"
            elif learning_intent == "misconception_repair":
                question_text = (
                    f"针对“{misconception}”，请按“{second_skill}”修正过程并完成：{prompt}"
                )
                expected_answer = f"过程修正：{second_skill}；答案要点：{outline}"
            else:
                question_text = (
                    f"把“{name}”的解题过程迁移到“{related_name}”：{related_reason} "
                    "列出需要保留和需要调整的步骤。"
                )
                expected_answer = f"迁移依据：{related_reason}；保留步骤：{skill}"

        return CognitiveQuestionBlueprint(
            blueprint_id=(
                f"graph.{topic_id}.{hypothesis_code}.{learning_intent}.v1"
            ),
            topic_id=topic_id,
            hypothesis_code=hypothesis_code,
            learning_intent=learning_intent,
            repair_strategy=_GRAPH_TEACHING_STRATEGIES[
                (hypothesis_code, learning_intent)
            ],
            question_family_id=(
                f"graph.{topic_id}.{hypothesis_code}.{learning_intent}"
            ),
            question_text=question_text,
            math_expression=f"topic:{topic_id}",
            expected_answer=expected_answer,
            diagnostic_signature=(
                f"topic:{topic_id}|hypothesis:{hypothesis_code}|"
                f"intent:{learning_intent}|source:knowledge-seed-v3"
            ),
            competing_hypothesis_codes=tuple(
                code for code in self._generic_by_code if code != hypothesis_code
            ),
        )

    def get_blueprint(self, blueprint_id: str) -> CognitiveQuestionBlueprint | None:
        reviewed = self._base_catalog.get_blueprint(blueprint_id)
        if reviewed is not None:
            return reviewed
        normalized = str(blueprint_id or "").strip()
        if not normalized.startswith("graph.") or not normalized.endswith(".v1"):
            return None
        for code in self._generic_by_code:
            for intent in _GRAPH_TEACHING_INTENTS:
                suffix = f".{code}.{intent}.v1"
                if normalized.endswith(suffix):
                    topic_id = normalized[len("graph.") : -len(suffix)]
                    return self._blueprint(topic_id, code, intent)
        return None

    def blueprints(
        self,
        topic_id: str,
        *,
        hypothesis_code: str = "",
        learning_intent: LearningIntent | None = None,
    ) -> tuple[CognitiveQuestionBlueprint, ...]:
        if self._base_catalog.supports_topic(topic_id):
            return self._base_catalog.blueprints(
                topic_id,
                hypothesis_code=hypothesis_code,
                learning_intent=learning_intent,
            )
        codes = (
            (hypothesis_code,)
            if hypothesis_code
            else self._active_graph_codes(topic_id)
        )
        intents = (
            (learning_intent,)
            if learning_intent is not None
            else _GRAPH_TEACHING_INTENTS
        )
        return tuple(
            blueprint
            for code in codes
            for intent in intents
            if (blueprint := self._blueprint(topic_id, code, intent)) is not None
        )


def build_knowledge_graph_cognitive_catalog(
    topic_provider: Callable[[str], Mapping[str, Any] | None],
    *,
    base_catalog: CognitiveCatalog | None = None,
    comprehensive_teaching: bool = False,
) -> KnowledgeGraphCognitiveCatalog:
    catalog_type = (
        ComprehensiveKnowledgeGraphCognitiveCatalog
        if comprehensive_teaching
        else KnowledgeGraphCognitiveCatalog
    )
    return catalog_type(
        base_catalog or COGNITIVE_CATALOG_V1,
        topic_provider,
    )


CHAIN_RULE_HYPOTHESES = (
    CognitiveHypothesisSpec(
        topic_id=CHAIN_RULE_TOPIC_ID,
        code="omit_inner_derivative",
        description=(
            "The learner differentiates the outer function but omits the "
            "derivative of the inner function entirely. If a non-constant "
            "inner-derivative factor is attempted but is mathematically "
            "wrong, use differentiate_inner_incorrectly instead."
        ),
        availability="active",
        competing_hypothesis_codes=(
            "differentiate_inner_incorrectly",
            "confuse_product_and_chain",
        ),
    ),
    CognitiveHypothesisSpec(
        topic_id=CHAIN_RULE_TOPIC_ID,
        code="differentiate_inner_incorrectly",
        description=(
            "The learner attempts the chain rule but differentiates the inner "
            "function incorrectly. This requires an attempted non-constant "
            "inner-derivative factor; a completely absent factor belongs to "
            "omit_inner_derivative."
        ),
        competing_hypothesis_codes=("omit_inner_derivative",),
    ),
    CognitiveHypothesisSpec(
        topic_id=CHAIN_RULE_TOPIC_ID,
        code="confuse_product_and_chain",
        description=(
            "The learner treats a composition as a product, or applies the "
            "product rule where the chain rule is required."
        ),
        competing_hypothesis_codes=("omit_inner_derivative",),
    ),
)

CHAIN_RULE_QUESTION_BLUEPRINTS = (
    CognitiveQuestionBlueprint(
        blueprint_id="chain.omit-inner.compare-steps.v1",
        topic_id=CHAIN_RULE_TOPIC_ID,
        hypothesis_code="omit_inner_derivative",
        learning_intent="misconception_probe",
        repair_strategy="compare_steps",
        question_family_id="chain.sin-square.compare-steps",
        question_text=(
            "Compare the two shown derivations of d/dx sin(x^2). Identify "
            "which derivation applies every required factor and explain why."
        ),
        math_expression="d/dx sin(x^2)",
        expected_answer="2*x*cos(x^2)",
        diagnostic_signature=(
            "composition:sin(x^2)|outer:cos(x^2)|inner:2*x|"
            "omission:cos(x^2)"
        ),
        competing_hypothesis_codes=(
            "differentiate_inner_incorrectly",
            "confuse_product_and_chain",
        ),
    ),
    CognitiveQuestionBlueprint(
        blueprint_id="chain.omit-inner.fill-factor.v1",
        topic_id=CHAIN_RULE_TOPIC_ID,
        hypothesis_code="omit_inner_derivative",
        learning_intent="misconception_repair",
        repair_strategy="complete_inner_derivative",
        question_family_id="chain.cos-cube.fill-factor",
        question_text=(
            "Complete the missing factor: d/dx cos(x^3) = "
            "-sin(x^3) * ____."
        ),
        math_expression="d/dx cos(x^3)",
        expected_answer="3*x^2",
        diagnostic_signature=(
            "composition:cos(x^3)|outer:-sin(x^3)|inner:3*x^2|"
            "blank:inner"
        ),
        competing_hypothesis_codes=("differentiate_inner_incorrectly",),
    ),
    CognitiveQuestionBlueprint(
        blueprint_id="chain.omit-inner.classify-structure.v1",
        topic_id=CHAIN_RULE_TOPIC_ID,
        hypothesis_code="omit_inner_derivative",
        learning_intent="misconception_probe",
        repair_strategy="structure_classification",
        question_family_id="chain.exp-affine.structure-classification",
        question_text=(
            "Classify exp(3x+1) as a composition or a product, then name "
            "the inner-derivative factor required by differentiation."
        ),
        math_expression="d/dx exp(3*x+1)",
        expected_answer="composition; inner factor 3",
        diagnostic_signature=(
            "composition:exp(3*x+1)|outer:exp(3*x+1)|inner:3|"
            "competition:product"
        ),
        competing_hypothesis_codes=("confuse_product_and_chain",),
    ),
    CognitiveQuestionBlueprint(
        blueprint_id="chain.omit-inner.minimal-change.v1",
        topic_id=CHAIN_RULE_TOPIC_ID,
        hypothesis_code="omit_inner_derivative",
        learning_intent="misconception_repair",
        repair_strategy="minimal_change",
        question_family_id="chain.sin-power.minimal-change",
        question_text=(
            "The derivative of sin(x^2) is 2*x*cos(x^2). Change only the "
            "inner power to x^3 and give the new derivative."
        ),
        math_expression="d/dx sin(x^3)",
        expected_answer="3*x^2*cos(x^3)",
        diagnostic_signature=(
            "composition:sin(x^3)|outer:cos(x^3)|inner:3*x^2|"
            "contrast:x^2-to-x^3"
        ),
        competing_hypothesis_codes=("differentiate_inner_incorrectly",),
    ),
    CognitiveQuestionBlueprint(
        blueprint_id="chain.omit-inner.cross-form-transfer.v1",
        topic_id=CHAIN_RULE_TOPIC_ID,
        hypothesis_code="omit_inner_derivative",
        learning_intent="transfer_check",
        repair_strategy="cross_form_transfer",
        question_family_id="chain.polynomial-power.cross-form-transfer",
        question_text="Differentiate (x^2 + 1)^4.",
        math_expression="d/dx (x^2+1)^4",
        expected_answer="8*x*(x^2+1)^3",
        diagnostic_signature=(
            "composition:(x^2+1)^4|outer:4*(x^2+1)^3|inner:2*x|"
            "transfer:trigonometric-to-polynomial-power"
        ),
        competing_hypothesis_codes=("differentiate_inner_incorrectly",),
    ),
    CognitiveQuestionBlueprint(
        blueprint_id="chain.omit-inner.cross-form-transfer.v2-retest",
        topic_id=CHAIN_RULE_TOPIC_ID,
        hypothesis_code="omit_inner_derivative",
        learning_intent="transfer_check",
        repair_strategy="cross_form_transfer",
        question_family_id="chain.polynomial-power.cross-form-transfer",
        question_text="Differentiate (x^2 + 3)^5.",
        math_expression="d/dx (x^2+3)^5",
        expected_answer="10*x*(x^2+3)^4",
        diagnostic_signature=(
            "composition:(x^2+3)^5|outer:5*(x^2+3)^4|inner:2*x|"
            "transfer:trigonometric-to-polynomial-power"
        ),
        competing_hypothesis_codes=("differentiate_inner_incorrectly",),
    ),
)


CHAIN_RULE_TEACHING_COVERAGE_BLUEPRINTS = (
    CognitiveQuestionBlueprint(
        blueprint_id="chain.inner-incorrect.compare-factor.v1",
        topic_id=CHAIN_RULE_TOPIC_ID,
        hypothesis_code="differentiate_inner_incorrectly",
        learning_intent="misconception_probe",
        repair_strategy="compare_steps",
        question_family_id="chain.sin-cube.compare-inner-factor",
        question_text=(
            "A learner writes d/dx sin(x^3) = 2*x*cos(x^3). "
            "Identify the incorrect factor and give the corrected derivative."
        ),
        math_expression="d/dx sin(x^3)",
        expected_answer="3*x^2*cos(x^3)",
        diagnostic_signature=(
            "composition:sin(x^3)|outer:cos(x^3)|inner:3*x^2|"
            "attempted_inner:2*x"
        ),
        competing_hypothesis_codes=("omit_inner_derivative",),
    ),
    CognitiveQuestionBlueprint(
        blueprint_id="chain.inner-incorrect.recompute-inner.v1",
        topic_id=CHAIN_RULE_TOPIC_ID,
        hypothesis_code="differentiate_inner_incorrectly",
        learning_intent="misconception_repair",
        repair_strategy="complete_inner_derivative",
        question_family_id="chain.exp-quartic.recompute-inner",
        question_text=(
            "For d/dx exp(x^4), first recompute the derivative of x^4, "
            "then give the complete derivative."
        ),
        math_expression="d/dx exp(x^4)",
        expected_answer="4*x^3*exp(x^4)",
        diagnostic_signature=(
            "composition:exp(x^4)|outer:exp(x^4)|inner:4*x^3|"
            "repair:recompute_inner"
        ),
        competing_hypothesis_codes=("omit_inner_derivative",),
    ),
    CognitiveQuestionBlueprint(
        blueprint_id="chain.inner-incorrect.cross-form-transfer.v1",
        topic_id=CHAIN_RULE_TOPIC_ID,
        hypothesis_code="differentiate_inner_incorrectly",
        learning_intent="transfer_check",
        repair_strategy="cross_form_transfer",
        question_family_id="chain.cos-cubic.cross-form-transfer",
        question_text="Differentiate cos(2*x^3 + 1).",
        math_expression="d/dx cos(2*x^3+1)",
        expected_answer="-6*x^2*sin(2*x^3+1)",
        diagnostic_signature=(
            "composition:cos(2*x^3+1)|outer:-sin(2*x^3+1)|"
            "inner:6*x^2|transfer:inner_derivative_accuracy"
        ),
        competing_hypothesis_codes=("omit_inner_derivative",),
    ),
    CognitiveQuestionBlueprint(
        blueprint_id="chain.product-confusion.classify-rule.v1",
        topic_id=CHAIN_RULE_TOPIC_ID,
        hypothesis_code="confuse_product_and_chain",
        learning_intent="misconception_probe",
        repair_strategy="structure_classification",
        question_family_id="chain.power-composition.classify-rule",
        question_text=(
            "Classify (x^2 + 1)^5 as a composition or a product, name the "
            "differentiation rule, and give its derivative."
        ),
        math_expression="d/dx (x^2+1)^5",
        expected_answer="composition; chain rule; 10*x*(x^2+1)^4",
        diagnostic_signature=(
            "composition:(x^2+1)^5|rule:chain|outer:5*(x^2+1)^4|"
            "inner:2*x|competition:product"
        ),
        competing_hypothesis_codes=("omit_inner_derivative",),
    ),
    CognitiveQuestionBlueprint(
        blueprint_id="chain.product-confusion.compare-structure.v1",
        topic_id=CHAIN_RULE_TOPIC_ID,
        hypothesis_code="confuse_product_and_chain",
        learning_intent="misconception_repair",
        repair_strategy="compare_steps",
        question_family_id="chain.exp-quadratic.compare-structure",
        question_text=(
            "Treat exp(x^2 + 1) as an outer function applied to an inner "
            "expression, then differentiate it."
        ),
        math_expression="d/dx exp(x^2+1)",
        expected_answer="2*x*exp(x^2+1)",
        diagnostic_signature=(
            "composition:exp(x^2+1)|outer:exp(x^2+1)|inner:2*x|"
            "repair:composition_not_product"
        ),
        competing_hypothesis_codes=("omit_inner_derivative",),
    ),
    CognitiveQuestionBlueprint(
        blueprint_id="chain.product-confusion.cross-form-transfer.v1",
        topic_id=CHAIN_RULE_TOPIC_ID,
        hypothesis_code="confuse_product_and_chain",
        learning_intent="transfer_check",
        repair_strategy="cross_form_transfer",
        question_family_id="chain.log-affine.cross-form-transfer",
        question_text="Differentiate ln(3*x + 2).",
        math_expression="d/dx ln(3*x+2)",
        expected_answer="3/(3*x+2)",
        diagnostic_signature=(
            "composition:ln(3*x+2)|outer:1/(3*x+2)|inner:3|"
            "transfer:composition_not_product"
        ),
        competing_hypothesis_codes=("omit_inner_derivative",),
    ),
)

COGNITIVE_CATALOG_V1 = CognitiveCatalog(
    CHAIN_RULE_HYPOTHESES,
    topic_aliases={COLLEGE_CHAIN_RULE_TOPIC_ID: CHAIN_RULE_TOPIC_ID},
    question_blueprints=CHAIN_RULE_QUESTION_BLUEPRINTS,
)

COGNITIVE_CATALOG_V2 = CognitiveCatalog(
    tuple(replace(item, availability="active") for item in CHAIN_RULE_HYPOTHESES),
    topic_aliases={COLLEGE_CHAIN_RULE_TOPIC_ID: CHAIN_RULE_TOPIC_ID},
    question_blueprints=(
        *CHAIN_RULE_QUESTION_BLUEPRINTS,
        *CHAIN_RULE_TEACHING_COVERAGE_BLUEPRINTS,
    ),
)


def cognitive_catalog_for_component_version(value: object) -> CognitiveCatalog:
    """Resolve the immutable reviewed catalog for one registered component id."""

    normalized = str(value or "").strip()
    if normalized in {
        "cognitive-v4-coverage-1",
        "cognitive-v4-coverage-2",
        "cognitive-catalog-v2",
        "cognitive-catalog-v3",
        "cognitive-question-validator-v4-coverage-1",
        "cognitive-question-validator-v4-coverage-2",
    }:
        return COGNITIVE_CATALOG_V2
    return COGNITIVE_CATALOG_V1


__all__ = [
    "CHAIN_RULE_HYPOTHESES",
    "CHAIN_RULE_QUESTION_BLUEPRINTS",
    "CHAIN_RULE_TEACHING_COVERAGE_BLUEPRINTS",
    "CHAIN_RULE_TOPIC_ID",
    "COLLEGE_CHAIN_RULE_TOPIC_ID",
    "COGNITIVE_CATALOG_V1",
    "COGNITIVE_CATALOG_V2",
    "ComprehensiveKnowledgeGraphCognitiveCatalog",
    "CognitiveCatalog",
    "CognitiveHypothesisSpec",
    "CognitiveQuestionBlueprint",
    "HypothesisAvailability",
    "KNOWLEDGE_GRAPH_HYPOTHESES",
    "KnowledgeGraphCognitiveCatalog",
    "build_knowledge_graph_cognitive_catalog",
    "cognitive_catalog_for_component_version",
]
