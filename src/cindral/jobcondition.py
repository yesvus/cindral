"""Whether a job's own `if:` holds for the event this run was queued for.

A job-level `if` is a decision the repository already made, so Cindral either
honours it or says it cannot. Silently running a job whose `if` would have
skipped it reports a green build for work the repository declined to do, which
is worse than refusing. The answer is therefore three-valued: a context path the
queue record does not carry leaves the condition undecided rather than false.
"""
import re
from dataclasses import dataclass, field

from .workflow_context import RunContext

__all__ = ["Condition", "ConditionError", "evaluate_condition"]

# The operators a job condition is written in. Anything outside this set is a
# decision Cindral does not make, and is refused by name rather than guessed at.
TOKEN = re.compile(
    r"""\s*(?:
        (?P<operator>\|\||&&|==|!=|!|\(|\))
      | (?P<string>'(?:[^']|'')*'|"(?:[^"]|"")*")
      | (?P<path>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)*)
      | (?P<number>-?[0-9]+(?:\.[0-9]+)?)
      | (?P<other>\S)
    )""",
    re.VERBOSE,
)
LITERAL: dict[str, str] = {"true": "true", "false": "", "null": "", "none": ""}
# A guard is written by a person, and a repository nests a handful deep. Beyond
# this it is refused by name, because reading it costs stack in four mutually
# recursive functions and a RecursionError is not a refusal anything catches.
MAX_DEPTH = 64


class ConditionError(ValueError):
    """Raised for an `if` this evaluator does not read."""


@dataclass(frozen=True)
class Condition:
    """A condition's verdict, and the context it could not resolve to get there."""

    value: bool | None
    unread: tuple[str, ...] = field(default_factory=tuple)

    @property
    def decided(self) -> bool:
        return self.value is not None


def evaluate_condition(expression: str, context: RunContext) -> Condition:
    """Read a job condition for the event `context` carries, or report it unread.

    A path the run does not carry is undecided rather than false, because the
    two lead to different verdicts and only the repository can say which it
    meant. `a || b` still settles on a true `a` and `a && b` on a false `a`, so
    a condition whose deciding side is readable runs without needing the other.
    """
    tokens = _tokenize(expression)
    if not tokens:
        raise ConditionError("the condition is empty")
    unread: set[str] = set()
    value, rest = _or(tokens, context, unread)
    if rest:
        raise ConditionError(f"unreadable text {rest[0][1]!r}")
    # a path an operator never reached did not hold the verdict up, so it is
    # not reported as unread: the condition is decided without it
    return Condition(value, () if value is not None else tuple(sorted(unread)))


def _tokenize(expression: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(expression):
        if expression[position].isspace():
            position += 1
            continue
        match = TOKEN.match(expression, position)
        if match is None:
            raise ConditionError(f"unreadable text {expression[position:].strip()!r}")
        position = match.end()
        kind = match.lastgroup
        assert kind is not None
        if kind == "other":
            raise ConditionError(f"unreadable text {match.group()!r}")
        tokens.append((kind, match.group()))
    return tokens


def _is(tokens: list[tuple[str, str]], operator: str) -> bool:
    return bool(tokens) and tokens[0] == ("operator", operator)


def _or(
    tokens: list[tuple[str, str]], context: RunContext, unread: set[str], depth: int = 0
) -> tuple[bool | None, list[tuple[str, str]]]:
    if depth > MAX_DEPTH:
        raise ConditionError(
            f"the condition nests deeper than {MAX_DEPTH} parentheses"
        )
    value, rest = _and(tokens, context, unread, depth)
    while _is(rest, "||"):
        right, rest = _and(rest[1:], context, unread, depth)
        value = _either(value, right, decisive=True)
    return value, rest


def _and(
    tokens: list[tuple[str, str]], context: RunContext, unread: set[str], depth: int = 0
) -> tuple[bool | None, list[tuple[str, str]]]:
    value, rest = _not(tokens, context, unread, depth)
    while _is(rest, "&&"):
        right, rest = _not(rest[1:], context, unread, depth)
        value = _either(value, right, decisive=False)
    return value, rest


def _either(left: bool | None, right: bool | None, decisive: bool) -> bool | None:
    """Combine two operands, short-circuiting on the one that settles the pair.

    `true || unread` is true and `false && unread` is false, because neither
    reaches the other side. Everything else needs both, so an unread operand
    leaves the pair undecided.
    """
    if (left is not None and left == decisive) or (right is not None and right == decisive):
        return decisive
    if left is None or right is None:
        return None
    return not decisive


def _not(
    tokens: list[tuple[str, str]], context: RunContext, unread: set[str], depth: int = 0
) -> tuple[bool | None, list[tuple[str, str]]]:
    # A `!` chain recurses here and reaches `_operand` only once every `!` is
    # consumed, so a bound on the other two descents fires at whatever depth the
    # interpreter happens to survive to, not at the bound.
    if depth > MAX_DEPTH:
        raise ConditionError(f"the condition nests deeper than {MAX_DEPTH} terms")
    if _is(tokens, "!"):
        value, rest = _not(tokens[1:], context, unread, depth + 1)
        return (None if value is None else not value), rest
    return _comparison(tokens, context, unread, depth)


def _unquote(text: str) -> str:
    """A string literal's text, with GitHub's doubled quote as one quote.

    A default substituted into a guard can contain one, and the tokenizer takes
    the doubling as the escape it is. Undoing it here is what keeps
    `default: "it's here"` readable rather than refused as two literals.
    """
    return text[1:-1].replace("''", "'").replace('""', '"')


def _as_text(value: bool | None) -> str | None:
    """A group's verdict as the text a comparison compares against.

    GitHub casts a boolean to `true`/`false` when it is on the right of a
    comparison, so this is the only faithful rendering available here.
    """
    if value is None:
        return None
    return "true" if value else ""


def _comparison(
    tokens: list[tuple[str, str]], context: RunContext, unread: set[str], depth: int = 0
) -> tuple[bool | None, list[tuple[str, str]]]:
    left, left_kind, rest = _operand(tokens, context, unread, depth)
    if not (_is(rest, "==") or _is(rest, "!=")):
        # An operand with no comparison after it *is* the value of this part of
        # the expression, so it becomes a real bool here. Carrying the raw text
        # instead made `true` the string "true", which is truthy, and `false` the
        # empty string, which is falsy: right answers to the wrong question, and
        # `true || unread` compared a string against a bool, so it never
        # short-circuited and a condition that settles on its own side was
        # reported as undecided.
        #
        # A number is false only at zero, and its text is non-empty at zero, so
        # truthiness of the text is the wrong question for a bare number.
        if left_kind == "number":
            return (None if left is None else float(left) != 0.0), rest
        return (None if left is None else bool(left)), rest
    operator = rest[0][1]
    right, right_kind, rest = _operand(rest[1:], context, unread, depth)
    if left is None or right is None:
        return None, rest
    equal = _equal(left, left_kind, right, right_kind)
    return (equal if operator == "==" else not equal), rest


def _equal(left: str, left_kind: str, right: str, right_kind: str) -> bool:
    """Two values as GitHub compares them, which is not how Python compares them.

    GitHub compares as the type when both sides are the same type, and coerces
    both sides to a number when they are not. The order matters: comparing two
    strings numerically made `'1' == '1.0'` true, where GitHub compares them as
    the strings they are and answers false.

    A side that will not coerce is not equal to anything, which is what makes
    `(true) == 'true'` false: a boolean coerces to one, and a string that does not
    read as a number does not.

    Plain text equality would answer most of these the other way, and each one is
    a job GitHub runs reported as skipped or the reverse.
    """
    if left_kind == right_kind:
        if left_kind == "string":
            # GitHub ignores case when comparing strings.
            return left.casefold() == right.casefold()
        if left_kind == "boolean":
            return left == right
        return _as_number(left) == _as_number(right)
    return _coerce(left, left_kind) == _coerce(right, right_kind)


def _coerce(value: str, kind: str) -> float:
    """A value as a number, or NaN when it does not read as one.

    NaN rather than a sentinel, because NaN compares unequal to everything
    including itself, which is the answer GitHub gives a value that cannot be
    coerced on either side of a mixed comparison.
    """
    if kind == "boolean":
        return 1.0 if value == "true" else 0.0
    number = _as_number(value)
    return float("nan") if number is None else number


def _as_number(value: str) -> float | None:
    """The number a value reads as, or None when it does not read as one."""
    text = value.strip()
    if not text:
        # GitHub coerces an empty or whitespace-only string to zero, which is
        # what makes `0 == ''` hold.
        return 0.0
    try:
        return float(text)
    except ValueError:
        return None


def _operand(
    tokens: list[tuple[str, str]], context: RunContext, unread: set[str], depth: int = 0
) -> tuple[str | None, str, list[tuple[str, str]]]:
    """One side of a comparison, as a string. `None` is a path the run lacks.

    A value is carried as text, so `github.event_name != 'pull_request'` reads
    the way the repository wrote it, and an empty value is falsy the way the
    engine treats one. The caller turns it into a bool when no comparison
    follows, so the text never escapes as a value.
    """
    if depth > MAX_DEPTH:
        # The value-unwrap path recurses through here rather than through `_or`,
        # so a bound only on the expression descent does not bound the stack.
        raise ConditionError(f"the condition nests deeper than {MAX_DEPTH} parentheses")
    if not tokens:
        raise ConditionError("the condition ends after an operator")
    kind, text = tokens[0]
    rest = tokens[1:]
    if kind == "operator" and text == "(":
        # A group holding one operand is that operand's *value*, not a verdict.
        # Read as a condition, `(github.ref)` yields a bool, and a bool compared
        # against a string is never equal, so the comparison below would answer
        # false for every parenthesised value. A longer group is a sub-condition,
        # whose boolean is cast to text.
        #
        # Whether GitHub reads a one-operand group as its value or as a boolean
        # is not confirmed. Reading it as a
        # value is what a person writing `(a) == b` means, and the other reading
        # makes such a guard always false, which is the worse error: a job GitHub
        # runs, reported as skipped.
        #
        # The scan skips repeated opening parentheses because redundant ones are
        # that operand, and `((x))` is two of them. A check on the next two
        # tokens answered the outer one wrongly: `((0))` became the string
        # "true" and ran a job GitHub skips.
        index = 0
        while index < len(rest) and rest[index] == ("operator", "("):
            index += 1
        if (
            index + 1 < len(rest)
            and rest[index][0] in {"path", "number", "string"}
            and rest[index + 1] == ("operator", ")")
        ):
            # The rest of the tokens go in with the operand, not just the
            # operand: the recursion has to see what follows the group so that
            # what it returns is the real remainder. A truncated tail leaves the
            # outer `)` in place and the comparison after it is never seen.
            value, operand_kind, after = _operand(
                [rest[index], *rest[index + 1:]], context, unread, depth + index + 1
            )
            closes = 0
            while closes < len(after) and after[closes] == ("operator", ")"):
                closes += 1
            return value, operand_kind, after[closes:]
        value, rest = _or(rest, context, unread, depth + 1)
        if not _is(rest, ")"):
            raise ConditionError("unbalanced parenthesis")
        return _as_text(value), "boolean", rest[1:]
    if kind == "string":
        return _unquote(text), kind, rest
    if kind == "number":
        return text, kind, rest
    if kind != "path":
        raise ConditionError(f"unreadable text {text!r}")
    name = text.lower()
    if name in LITERAL:
        return LITERAL[name], "boolean", rest
    value = context.value(text)
    if value is None:
        unread.add(text)
        return None, kind, rest
    return value, "string", rest
