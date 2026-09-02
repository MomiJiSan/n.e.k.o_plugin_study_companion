"""Closed-world misconception and intervention catalog.

The catalog is the reviewed source of truth for V2 teaching mechanics.  A
model may paraphrase a prompt, but it cannot add hypotheses, select a repair
strategy, or change the protected mathematical and diagnostic signatures.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

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
)

COGNITIVE_CATALOG_V1 = CognitiveCatalog(
    CHAIN_RULE_HYPOTHESES,
    topic_aliases={COLLEGE_CHAIN_RULE_TOPIC_ID: CHAIN_RULE_TOPIC_ID},
    question_blueprints=CHAIN_RULE_QUESTION_BLUEPRINTS,
)


__all__ = [
    "CHAIN_RULE_HYPOTHESES",
    "CHAIN_RULE_QUESTION_BLUEPRINTS",
    "CHAIN_RULE_TOPIC_ID",
    "COLLEGE_CHAIN_RULE_TOPIC_ID",
    "COGNITIVE_CATALOG_V1",
    "CognitiveCatalog",
    "CognitiveHypothesisSpec",
    "CognitiveQuestionBlueprint",
    "HypothesisAvailability",
]
