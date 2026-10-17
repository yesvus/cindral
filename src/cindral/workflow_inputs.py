"""Where one workflow's declared inputs meet another's use of them.

A reusable workflow reads `inputs.` for values its caller supplies in `with:`, and
a caller's own `if:` reads the inputs the caller declares. Both are resolved
here, because the two are the same question asked from opposite ends: what did
the file in hand declare, and what is being asked of it.

Substitution is by the *declared default* where the value is not in the file. A
`with:` value is static and exact; a condition's value arrives with the dispatch,
so the translation, which happens before the run, can only see the shape of the
guard. An input that is not declared at all is refused by name rather than left
in the expression.
"""
import re
from typing import Any

from .workflow_errors import WorkflowUnsupported

# GitHub accepts a hyphen in an input name, which is how most repositories spell
# one, and writes it both ways: `inputs.run-ci` and `inputs['run-ci']`. A pattern
# that allowed only letters and underscores left the first form untouched, so it
# was neither substituted nor refused and the expression reached the condition
# parser intact.
_INPUT = re.compile(
    r"\$\{\{\s*(?:inputs\.([A-Za-z_][A-Za-z0-9_-]*)|inputs\[\s*['\"]([A-Za-z_][A-Za-z0-9_-]*)['\"]\s*\])\s*\}\}"
)


def _caller_inputs(on_spec: Any) -> dict[str, str]:
    """The inputs a calling workflow declares, at their declared defaults.

    The `workflow_dispatch` and `workflow_call` declarations are both read, since
    a caller's own condition reads whichever applies to it.

    Only the `${{ }}` form is substituted. GitHub also accepts a bare
    `inputs.name` in an `if:`, which is an expression without delimiters, and this
    does not resolve that: reading a bare word as a value would need the whole
    expression language rather than one pattern, and guessing at it would put a
    resolved value in a condition that was not one.
    """
    if not isinstance(on_spec, dict):
        return {}
    declared: dict[str, str] = {}
    for trigger in ("workflow_dispatch", "workflow_call"):
        spec = on_spec.get(trigger)
        inputs = spec.get("inputs") if isinstance(spec, dict) else None
        if not isinstance(inputs, dict):
            continue
        for name, value in inputs.items():
            if not isinstance(value, dict) or "default" not in value:
                continue
            declared.setdefault(str(name), _as_expression(value.get("type"), value["default"]))
    return declared


def _as_expression(declared_type: Any, default: Any) -> str:
    """A default in the form a condition reads it, rather than the form YAML held it.

    A boolean default is the bare literal `true` or `false`, which is what a
    repository writes and what the condition parser reads as a verdict.

    Everything else is quoted. A `type: string` input whose default is the text
    `false` would otherwise become the bare literal `false` and skip a job
    GitHub runs, because a non-empty string is true and this would read as a
    boolean. The two are the same characters and not the same value, so the
    quoting is what keeps them apart.
    """
    if isinstance(default, bool) or declared_type == "boolean":
        return "true" if default in (True, "true", "True") else "false"
    if declared_type == "number":
        # A number reaches the guard as a number, and zero is false there.
        # Quoted it would be the non-empty string '0', which is true, so an
        # input counting to zero ran a job GitHub skips.
        try:
            zero = float(default or 0) == 0.0
        except (TypeError, ValueError) as exc:
            # `float` raising here would be a bare ValueError, and a ValueError
            # is not a WorkflowUnsupported, so it would skip the committed
            # contract that is supposed to answer a default this cannot read.
            raise WorkflowUnsupported(
                f"input default {default!r} is declared a number and is not one"
            ) from exc
        return "0" if zero else str(default)
    text = "" if default is None else str(default)
    return "'" + text.replace("'", "''") + "'"


def _call_inputs(on_spec: Any) -> dict[str, tuple[Any, str]]:
    """The inputs a called workflow declares, with their type and default.

    The type is kept because one value is used in two places that need it in two
    forms. `runs-on: ${{ inputs.runner }}` wants the bare text, and a guard wants
    the form its type reads: the text `false` is a non-empty string and therefore
    true, where the boolean `false` is a verdict. Substituting one form for both
    makes a job run or a job skip that GitHub decides the other way.
    """
    if not isinstance(on_spec, dict):
        return {}
    call = on_spec.get("workflow_call")
    inputs = call.get("inputs") if isinstance(call, dict) else None
    if not isinstance(inputs, dict):
        return {}
    return {
        str(name): (spec.get("type"), str(spec["default"]))
        for name, spec in inputs.items()
        if isinstance(spec, dict) and "default" in spec
    }


def _guard_inputs(declared: dict[str, tuple[Any, str]]) -> dict[str, str]:
    """The declared defaults in the form a called job's condition reads them."""
    return {
        name: _as_expression(kind, value) for name, (kind, value) in declared.items()
    }


def _plain_inputs(declared: dict[str, tuple[Any, str]]) -> dict[str, str]:
    """The declared defaults in the form a `runs-on` or a step input reads them."""
    return {name: value for name, (_, value) in declared.items()}


def _substitute_inputs(value: Any, inputs: dict[str, str], context: str) -> Any:
    """Put the caller's `with:` values where the called workflow reads `inputs.`."""
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1) or match.group(2)
            if name not in inputs:
                raise WorkflowUnsupported(
                    f"{context} reads input '{name}', which the caller does not pass"
                )
            return inputs[name]

        return _INPUT.sub(replace, value)
    if isinstance(value, dict):
        return {key: _substitute_inputs(item, inputs, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute_inputs(item, inputs, context) for item in value]
    return value
