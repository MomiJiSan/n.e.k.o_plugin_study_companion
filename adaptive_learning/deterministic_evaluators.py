"""Conservative, dependency-free deterministic assessment evaluators.

These evaluators only return a decision when they can establish a result from
server-owned private question data.  Any malformed, ambiguous, unsupported or
non-matching input returns ``None`` so the caller can retain the LLM-rubric
path.  In particular, this module never executes learner text as Python.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from .assessment import AssessmentDecision, AssessmentRequest


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _feature_enabled(feature_flags: Mapping[str, Any], name: str) -> bool:
    """Read only explicit boolean flags, including a TOML-style subsection."""

    if feature_flags.get(name) is True:
        return True
    return _mapping(feature_flags.get("assessment")).get(name) is True


def _question_type(request: AssessmentRequest) -> str:
    return str(request.context.get("question_type") or "").strip().lower()


def _normalized_text(value: object) -> str:
    """Normalize presentation differences without treating different words equal."""

    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _normalized_math_text(value: object) -> str:
    """Normalize harmless math typography for an exact declared-answer match."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = text.replace("$", "").replace("π", r"\pi")
    text = text.replace(r"\left", "").replace(r"\right", "")
    return "".join(text.split())


def _closed_world(request: AssessmentRequest) -> bool:
    answer_spec = _mapping(request.context.get("answer_spec"))
    return (
        request.context.get("closed_world") is True
        or answer_spec.get("closed_world") is True
    )


def _accepted_answers(request: AssessmentRequest) -> tuple[str, ...]:
    raw = request.context.get("accepted_answers")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raw = ()
    values = [request.expected_answer, *raw]
    normalizer = (
        _normalized_math_text
        if _question_type(request) == "math_exact"
        else _normalized_text
    )
    return tuple(
        normalized
        for value in values
        if (normalized := normalizer(value))
    )


def _correct_decision(evaluator_type: str, evaluator_version: str) -> AssessmentDecision:
    return AssessmentDecision(
        payload={
            "verdict": "correct",
            "score": 100,
            "final_answer_correct": True,
            # The UI renders its localized verdict message from these facts.
            "feedback": "",
        },
        evaluator_type=evaluator_type,
        evaluator_version=evaluator_version,
        confidence=1.0,
        fallback_reason="",
    )


def _wrong_decision(evaluator_type: str, evaluator_version: str) -> AssessmentDecision:
    return AssessmentDecision(
        payload={
            "verdict": "wrong",
            "score": 0,
            "final_answer_correct": False,
            # Avoid leaking an English-only fallback into localized clients.
            "feedback": "",
        },
        evaluator_type=evaluator_type,
        evaluator_version=evaluator_version,
        confidence=1.0,
        fallback_reason="",
    )


class ExactShortAnswerEvaluator:
    """Confirm server-declared exact answers without deciding open-world misses."""

    evaluator_type = "exact_short_answer"
    evaluator_version = "exact-short-answer-v1"
    feature_flag = "exact_short_answer_enabled"

    async def try_assess(
        self,
        request: AssessmentRequest,
        *,
        feature_flags: Mapping[str, Any],
    ) -> AssessmentDecision | None:
        if not _feature_enabled(feature_flags, self.feature_flag):
            return None
        question_type = _question_type(request)
        if question_type not in {"short_answer", "math_exact"}:
            return None
        answer = (
            _normalized_math_text(request.answer)
            if question_type == "math_exact"
            else _normalized_text(request.answer)
        )
        expected_answers = _accepted_answers(request)
        if not answer or not expected_answers:
            return None
        if answer in expected_answers:
            return _correct_decision(self.evaluator_type, self.evaluator_version)
        # An accepted-answer list need not exhaust valid semantic expressions.
        if _closed_world(request):
            return _wrong_decision(self.evaluator_type, self.evaluator_version)
        return None


_DECIMAL_LITERAL = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)


def _parse_decimal(value: object) -> Decimal | None:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text or not _DECIMAL_LITERAL.fullmatch(text):
        return None
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _numeric_tolerance(request: AssessmentRequest) -> Decimal | None:
    """Read tolerance only from a private, server-supplied answer spec."""

    answer_spec = _mapping(request.context.get("answer_spec"))
    if "numeric_tolerance" not in answer_spec:
        return Decimal(0)
    tolerance = _parse_decimal(answer_spec.get("numeric_tolerance"))
    if tolerance is None or tolerance < 0:
        return None
    return tolerance


class NumericToleranceEvaluator:
    """Evaluate unambiguous scalar numerical answers with ``Decimal`` only."""

    evaluator_type = "numeric_tolerance"
    evaluator_version = "numeric-tolerance-v1"
    feature_flag = "numeric_tolerance_enabled"

    async def try_assess(
        self,
        request: AssessmentRequest,
        *,
        feature_flags: Mapping[str, Any],
    ) -> AssessmentDecision | None:
        if not _feature_enabled(feature_flags, self.feature_flag):
            return None
        if _question_type(request) != "math_exact":
            return None
        answer = _parse_decimal(request.answer)
        expected = _parse_decimal(request.expected_answer)
        tolerance = _numeric_tolerance(request)
        if answer is None or expected is None or tolerance is None:
            return None
        if abs(answer - expected) <= tolerance:
            return _correct_decision(self.evaluator_type, self.evaluator_version)
        # Keep an unlisted-but-valid alternative answer on the LLM path unless
        # the question author has explicitly declared a closed answer world.
        if _closed_world(request):
            return _wrong_decision(self.evaluator_type, self.evaluator_version)
        return None


_EXPRESSION_TOKEN = re.compile(
    r"\s*(?:(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)|"
    r"(?P<identifier>[A-Za-z][A-Za-z0-9_]*)|(?P<operator>\*\*|[+\-*/^()]))"
)


class _ExpressionParseError(ValueError):
    pass


class _SafeExpressionParser:
    """A bounded parser for a deliberately tiny algebraic grammar.

    It recognizes literals, declared variables, parentheses, and elementary
    arithmetic.  It does *not* support Python attributes, calls, indexing,
    assignments, functions, matrices, piecewise notation, or implicit
    multiplication.  Expressions outside that grammar are not deterministically
    assessed.
    """

    def __init__(self, text: str, variables: frozenset[str]) -> None:
        text = unicodedata.normalize("NFKC", str(text or "")).strip()
        if not text or len(text) > 512:
            raise _ExpressionParseError("expression length is unsupported")
        self._variables = variables
        self._tokens = self._tokenize(text)
        self._index = 0
        self._depth = 0

    @staticmethod
    def _tokenize(text: str) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        position = 0
        while position < len(text):
            match = _EXPRESSION_TOKEN.match(text, position)
            if match is None:
                raise _ExpressionParseError("unsupported expression syntax")
            position = match.end()
            group = match.lastgroup
            value = match.group(group) if group else ""
            tokens.append((group or "", value))
            if len(tokens) > 256:
                raise _ExpressionParseError("too many expression tokens")
        return tokens

    def parse(self) -> tuple[Any, ...]:
        expression = self._sum()
        if self._peek() is not None:
            raise _ExpressionParseError("unexpected trailing token")
        return expression

    def _peek(self) -> tuple[str, str] | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _take_operator(self, *operators: str) -> str | None:
        token = self._peek()
        if token is not None and token[0] == "operator" and token[1] in operators:
            self._index += 1
            return token[1]
        return None

    def _sum(self) -> tuple[Any, ...]:
        left = self._product()
        while (operator := self._take_operator("+", "-")) is not None:
            right = self._product()
            left = _add(left, right if operator == "+" else _negate(right))
        return left

    def _product(self) -> tuple[Any, ...]:
        left = self._power()
        while (operator := self._take_operator("*", "/")) is not None:
            right = self._power()
            left = _multiply(left, right) if operator == "*" else ("divide", left, right)
        return left

    def _power(self) -> tuple[Any, ...]:
        left = self._unary()
        if self._take_operator("^", "**") is not None:
            # Right-associative powers retain their grouping.
            return ("power", left, self._power())
        return left

    def _unary(self) -> tuple[Any, ...]:
        if self._take_operator("+") is not None:
            return self._unary()
        if self._take_operator("-") is not None:
            return _negate(self._unary())
        return self._atom()

    def _atom(self) -> tuple[Any, ...]:
        self._depth += 1
        try:
            if self._depth > 32:
                raise _ExpressionParseError("expression nesting is unsupported")
            token = self._peek()
            if token is None:
                raise _ExpressionParseError("missing expression atom")
            self._index += 1
            if token[0] == "number":
                number = _parse_decimal(token[1])
                if number is None:
                    raise _ExpressionParseError("invalid numeric literal")
                return ("number", _canonical_decimal(number))
            if token[0] == "identifier":
                if token[1] not in self._variables:
                    raise _ExpressionParseError("undeclared variable")
                return ("variable", token[1])
            if token == ("operator", "("):
                expression = self._sum()
                if self._take_operator(")") is None:
                    raise _ExpressionParseError("missing closing parenthesis")
                return expression
            raise _ExpressionParseError("unsupported expression atom")
        finally:
            self._depth -= 1


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return str(value.normalize())


def _sort_key(value: tuple[Any, ...]) -> str:
    return repr(value)


def _add(*values: tuple[Any, ...]) -> tuple[Any, ...]:
    flattened: list[tuple[Any, ...]] = []
    for value in values:
        if value[0] == "add":
            flattened.extend(value[1:])
        else:
            flattened.append(value)
    return ("add", *sorted(flattened, key=_sort_key))


def _multiply(*values: tuple[Any, ...]) -> tuple[Any, ...]:
    flattened: list[tuple[Any, ...]] = []
    for value in values:
        if value[0] == "multiply":
            flattened.extend(value[1:])
        else:
            flattened.append(value)
    return ("multiply", *sorted(flattened, key=_sort_key))


def _negate(value: tuple[Any, ...]) -> tuple[Any, ...]:
    if value[0] == "negate":
        return value[1]
    return ("negate", value)


def _expression_engine(request: AssessmentRequest) -> Mapping[str, Any]:
    return _mapping(request.context.get("math_equivalence_engine"))


def _declared_variables(engine: Mapping[str, Any]) -> frozenset[str] | None:
    raw = engine.get("variables")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        return None
    variables = frozenset(str(value or "").strip() for value in raw)
    if not variables or "" in variables:
        return None
    return variables


class MathExpressionEvaluator:
    """Recognize a small, explicitly declared subset of algebraic equivalence."""

    evaluator_type = "math_expression"
    evaluator_version = "math-expression-v1"
    feature_flag = "math_expression_enabled"

    async def try_assess(
        self,
        request: AssessmentRequest,
        *,
        feature_flags: Mapping[str, Any],
    ) -> AssessmentDecision | None:
        if not _feature_enabled(feature_flags, self.feature_flag):
            return None
        if _question_type(request) != "math_exact":
            return None
        engine = _expression_engine(request)
        if engine.get("enabled") is not True:
            return None
        # A domain is mandatory: equivalence over reals and integers can differ.
        if str(engine.get("domain") or "").strip().lower() not in {"real", "integer"}:
            return None
        variables = _declared_variables(engine)
        if variables is None:
            return None
        try:
            expected = _SafeExpressionParser(request.expected_answer, variables).parse()
            learner = _SafeExpressionParser(request.answer, variables).parse()
        except _ExpressionParseError:
            return None
        if learner == expected:
            return _correct_decision(self.evaluator_type, self.evaluator_version)
        # The restricted normalizer cannot prove non-equivalence safely.  Keep
        # every miss on the LLM path, including closed-world questions.
        return None
