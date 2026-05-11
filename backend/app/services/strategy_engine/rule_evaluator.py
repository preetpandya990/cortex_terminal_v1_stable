"""
StrategyRuleEvaluator — Pure recursive condition-tree evaluator.

Evaluates a ConditionNode (ConditionLeaf or ConditionGroup) against a flat
dict derived from MarketContext.as_dict().  Zero I/O — fully deterministic,
pure-Python, and independently testable.

Supported operators (mirrors ConditionOperator enum):
    gt           — field > value
    gte          — field >= value
    lt           — field < value
    lte          — field <= value
    eq           — field == value  (numeric or string, case-insensitive for str)
    neq          — field != value
    between      — value is [low, high]; low <= field <= high
    crosses_above — field > value (current snapshot: "is above threshold/field")
    crosses_below — field < value (current snapshot: "is below threshold/field")

Note on crosses_above / crosses_below:
    A true crossover requires two sequential data points.  Since each
    MarketContext is a single-point snapshot (one signal), these operators
    are evaluated as "currently above / below" the reference value or field.
    When value is a string it is treated as a field path into the same ctx dict.

Short-circuit evaluation:
    AND  — stops on first False child
    OR   — stops on first True child
    NOT  — evaluates single child, inverts result
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.schemas.strategies import (
    ConditionGroup,
    ConditionLeaf,
    ConditionOperator,
    LogicalOperator,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class EvalResult:
    passed: bool
    reason: str


# ── Public entry point ────────────────────────────────────────────────────────

def evaluate_node(
    node: ConditionLeaf | ConditionGroup,
    ctx: dict[str, Any],
) -> EvalResult:
    """
    Recursively evaluate a condition node against the context dict.

    Args:
        node: A ConditionLeaf or ConditionGroup instance.
        ctx:  Flat dict from MarketContext.as_dict().

    Returns:
        EvalResult with passed=True/False and a human-readable reason.
    """
    if isinstance(node, ConditionLeaf):
        return _eval_leaf(node, ctx)
    if isinstance(node, ConditionGroup):
        return _eval_group(node, ctx)
    return EvalResult(passed=False, reason=f"Unknown node type: {type(node).__name__}")


# ── Leaf evaluation ───────────────────────────────────────────────────────────

def _resolve_value(raw: Any, ctx: dict[str, Any]) -> Any:
    """
    Resolve a condition value, supporting string field references.

    When `raw` is a string that matches a key in ctx, the field's current
    value is returned (e.g. "ema_50" → ctx["ema_50"]).  Otherwise the raw
    value is returned as-is, allowing literal string comparisons.
    """
    if isinstance(raw, str) and raw in ctx:
        return ctx[raw]
    return raw


def _eval_leaf(leaf: ConditionLeaf, ctx: dict[str, Any]) -> EvalResult:
    field = leaf.field
    op = leaf.op
    raw_expected = leaf.value

    if field not in ctx:
        return EvalResult(
            passed=False,
            reason=f"field '{field}' not found in MarketContext",
        )

    actual = ctx[field]

    if actual is None:
        return EvalResult(
            passed=False,
            reason=f"field '{field}' is None — cannot evaluate {op.value!r}",
        )

    expected = _resolve_value(raw_expected, ctx)

    try:
        passed, reason = _apply_operator(field, actual, op, expected)
    except (TypeError, ValueError) as exc:
        return EvalResult(
            passed=False,
            reason=f"operator error on '{field}': {exc}",
        )

    return EvalResult(passed=passed, reason=reason)


def _apply_operator(
    field: str,
    actual: Any,
    op: ConditionOperator,
    expected: Any,
) -> tuple[bool, str]:
    """Apply a single comparison.  Returns (passed, human-readable reason)."""

    # ── Numeric comparisons ───────────────────────────────────────────────────
    if op in (
        ConditionOperator.gt,
        ConditionOperator.gte,
        ConditionOperator.lt,
        ConditionOperator.lte,
    ):
        a, e = float(actual), float(expected)
        mapping = {
            ConditionOperator.gt:  (a > e,  f"{field} ({a}) > {e}"),
            ConditionOperator.gte: (a >= e, f"{field} ({a}) >= {e}"),
            ConditionOperator.lt:  (a < e,  f"{field} ({a}) < {e}"),
            ConditionOperator.lte: (a <= e, f"{field} ({a}) <= {e}"),
        }
        passed, label = mapping[op]
        return passed, f"{label} → {'pass' if passed else 'fail'}"

    # ── Equality ──────────────────────────────────────────────────────────────
    if op == ConditionOperator.eq:
        if isinstance(actual, str) or isinstance(expected, str):
            passed = str(actual).lower() == str(expected).lower()
        else:
            passed = float(actual) == float(expected)
        return passed, f"{field} == {expected!r} → {'pass' if passed else 'fail'}"

    if op == ConditionOperator.neq:
        if isinstance(actual, str) or isinstance(expected, str):
            passed = str(actual).lower() != str(expected).lower()
        else:
            passed = float(actual) != float(expected)
        return passed, f"{field} != {expected!r} → {'pass' if passed else 'fail'}"

    # ── Range ─────────────────────────────────────────────────────────────────
    if op == ConditionOperator.between:
        if not isinstance(expected, list) or len(expected) != 2:
            raise ValueError("'between' operator requires [low, high] list")
        low, high = float(expected[0]), float(expected[1])
        a = float(actual)
        passed = low <= a <= high
        return passed, f"{field} ({a}) ∈ [{low}, {high}] → {'pass' if passed else 'fail'}"

    # ── Crossover (snapshot approximation) ───────────────────────────────────
    # MarketContext is a single-point snapshot; "crosses_above/below" is
    # evaluated as "currently above/below" the reference value or field.
    if op == ConditionOperator.crosses_above:
        a, e = float(actual), float(expected)
        passed = a > e
        ref = str(expected) if not isinstance(expected, (int, float)) else str(e)
        return passed, f"{field} ({a}) above {ref} → {'pass' if passed else 'fail'}"

    if op == ConditionOperator.crosses_below:
        a, e = float(actual), float(expected)
        passed = a < e
        ref = str(expected) if not isinstance(expected, (int, float)) else str(e)
        return passed, f"{field} ({a}) below {ref} → {'pass' if passed else 'fail'}"

    raise ValueError(f"Unsupported operator: {op.value!r}")


# ── Group evaluation ──────────────────────────────────────────────────────────

def _eval_group(group: ConditionGroup, ctx: dict[str, Any]) -> EvalResult:
    op = group.operator  # LogicalOperator enum

    if op == LogicalOperator.NOT:
        child_result = evaluate_node(group.children[0], ctx)
        passed = not child_result.passed
        return EvalResult(
            passed=passed,
            reason=f"NOT ({child_result.reason}) → {'pass' if passed else 'fail'}",
        )

    if op == LogicalOperator.AND:
        for child in group.children:
            result = evaluate_node(child, ctx)
            if not result.passed:
                return EvalResult(
                    passed=False,
                    reason=f"AND short-circuit: {result.reason}",
                )
        return EvalResult(
            passed=True,
            reason=f"AND: all {len(group.children)} conditions passed",
        )

    if op == LogicalOperator.OR:
        reasons: list[str] = []
        for child in group.children:
            result = evaluate_node(child, ctx)
            if result.passed:
                return EvalResult(passed=True, reason=f"OR short-circuit: {result.reason}")
            reasons.append(result.reason)
        return EvalResult(
            passed=False,
            reason=f"OR: no condition matched — {'; '.join(reasons)}",
        )

    return EvalResult(passed=False, reason=f"Unknown logical operator: {op.value!r}")
