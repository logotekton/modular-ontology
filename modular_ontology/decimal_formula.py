from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, localcontext


MAX_FORMULA_LENGTH = 256
MAX_FORMULA_TOKENS = 64
MAX_FORMULA_DEPTH = 8
# The complete formula is capped at 256 characters, so 512 significant digits
# safely cover every possible input literal plus additive carry digits.
FORMULA_DECIMAL_PRECISION = 512

_TOKEN_PATTERN = re.compile(r"\d+(?:\.\d+)?|[()+-]", re.ASCII)


class DecimalFormulaError(ValueError):
    """Raised when a fixed-profile decimal formula is unsafe or malformed."""


def evaluate_decimal_additive(formula: str) -> Decimal:
    """Evaluate the bounded additive grammar using ``Decimal`` only.

    Grammar::

        expression := factor (("+" | "-") factor)*
        factor     := ("+" | "-")* (number | "(" expression ")")

    Only ASCII digits, decimal points, plus/minus signs, parentheses, and
    ordinary spaces are accepted.  There is deliberately no general-purpose
    Python evaluation path.
    """

    if not isinstance(formula, str) or not formula:
        raise DecimalFormulaError("formula must be a non-empty string")
    if len(formula) > MAX_FORMULA_LENGTH:
        raise DecimalFormulaError(
            f"formula exceeds {MAX_FORMULA_LENGTH} characters"
        )

    tokens = _tokenize(formula)
    parser = _AdditiveParser(tokens)
    with localcontext() as context:
        context.prec = FORMULA_DECIMAL_PRECISION
        result = parser.parse()
    if not result.is_finite():
        raise DecimalFormulaError("formula result must be finite")
    return result


def _tokenize(formula: str) -> tuple[str, ...]:
    tokens: list[str] = []
    index = 0
    while index < len(formula):
        if formula[index] == " ":
            index += 1
            continue
        match = _TOKEN_PATTERN.match(formula, index)
        if match is None:
            raise DecimalFormulaError(
                f"formula contains an unsupported character at offset {index}"
            )
        tokens.append(match.group(0))
        if len(tokens) > MAX_FORMULA_TOKENS:
            raise DecimalFormulaError(
                f"formula exceeds {MAX_FORMULA_TOKENS} tokens"
            )
        index = match.end()
    if not tokens:
        raise DecimalFormulaError("formula contains no tokens")
    return tuple(tokens)


class _AdditiveParser:
    def __init__(self, tokens: tuple[str, ...]) -> None:
        self._tokens = tokens
        self._index = 0

    def parse(self) -> Decimal:
        result = self._expression(depth=0)
        if self._index != len(self._tokens):
            raise DecimalFormulaError(
                f"formula has an unexpected token {self._tokens[self._index]!r}"
            )
        return result

    def _expression(self, *, depth: int) -> Decimal:
        value = self._factor(depth=depth)
        while self._peek() in {"+", "-"}:
            operator = self._take()
            right = self._factor(depth=depth)
            value = value + right if operator == "+" else value - right
        return value

    def _factor(self, *, depth: int) -> Decimal:
        sign = Decimal(1)
        while self._peek() in {"+", "-"}:
            if self._take() == "-":
                sign = -sign

        token = self._peek()
        if token is None:
            raise DecimalFormulaError("formula ended before a value")
        if token == "(":
            if depth >= MAX_FORMULA_DEPTH:
                raise DecimalFormulaError(
                    f"formula exceeds parenthesis depth {MAX_FORMULA_DEPTH}"
                )
            self._take()
            value = self._expression(depth=depth + 1)
            if self._peek() != ")":
                raise DecimalFormulaError("formula has an unmatched '('")
            self._take()
            return sign * value
        if token == ")":
            raise DecimalFormulaError("formula has an unexpected ')'")

        self._take()
        try:
            value = Decimal(token)
        except InvalidOperation as exc:  # defensive: tokenizer already bounds numbers
            raise DecimalFormulaError(f"invalid decimal literal {token!r}") from exc
        if not value.is_finite():
            raise DecimalFormulaError("decimal literal must be finite")
        return sign * value

    def _peek(self) -> str | None:
        if self._index >= len(self._tokens):
            return None
        return self._tokens[self._index]

    def _take(self) -> str:
        token = self._peek()
        if token is None:
            raise DecimalFormulaError("formula ended unexpectedly")
        self._index += 1
        return token


__all__ = [
    "DecimalFormulaError",
    "FORMULA_DECIMAL_PRECISION",
    "MAX_FORMULA_DEPTH",
    "MAX_FORMULA_LENGTH",
    "MAX_FORMULA_TOKENS",
    "evaluate_decimal_additive",
]
