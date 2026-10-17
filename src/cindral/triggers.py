"""Which events a workflow declares, and which refs each one fires on.

Kept apart from the translation in `actions` because a repository's `on:` block
is the one thing a reader checks before anything runs: it decides whether a job
exists for the event at all, which is a different question from how that job
translates.
"""
import fnmatch
from typing import Any

__all__ = ["event_declares", "event_matches"]


def _patterns(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return None


def event_matches(
    on_spec: Any,
    event_name: str,
    branch_or_tag: str,
    base_ref: str | None = None,
) -> bool:
    if isinstance(on_spec, str):
        return on_spec == event_name
    if isinstance(on_spec, list):
        return any(event_matches(item, event_name, branch_or_tag, base_ref) for item in on_spec)
    if isinstance(on_spec, dict):
        if event_name not in on_spec:
            return False
        config = on_spec.get(event_name)
        if config is None:
            return True
        if isinstance(config, dict):
            is_tag = branch_or_tag.startswith("refs/tags/")
            clean_ref = (
                branch_or_tag.removeprefix("refs/tags/")
                if is_tag
                else branch_or_tag.removeprefix("refs/heads/")
            )
            has_tag_filter = "tags" in config or "tags-ignore" in config
            has_branch_filter = "branches" in config or "branches-ignore" in config
            if is_tag:
                if has_branch_filter and not has_tag_filter:
                    return False
                tags = _patterns(config.get("tags"))
                tags_ignore = _patterns(config.get("tags-ignore"))
                if tags is not None and not any(fnmatch.fnmatch(clean_ref, p) for p in tags):
                    return False
                if tags_ignore is not None and any(fnmatch.fnmatch(clean_ref, p) for p in tags_ignore):
                    return False
                return True
            if has_tag_filter and not has_branch_filter:
                return False
            if event_name == "pull_request":
                is_pr_ref = branch_or_tag.startswith("refs/pull/") or branch_or_tag.startswith("pull/")
                if is_pr_ref and base_ref is None:
                    return True
                target = base_ref if base_ref is not None else clean_ref
                branches = _patterns(config.get("branches"))
                branches_ignore = _patterns(config.get("branches-ignore"))
                if branches is not None and not any(fnmatch.fnmatch(target, p) for p in branches):
                    return False
                if branches_ignore is not None and any(fnmatch.fnmatch(target, p) for p in branches_ignore):
                    return False
                return True
            branches = _patterns(config.get("branches"))
            branches_ignore = _patterns(config.get("branches-ignore"))
            if branches is not None and not any(fnmatch.fnmatch(clean_ref, p) for p in branches):
                return False
            if branches_ignore is not None and any(fnmatch.fnmatch(clean_ref, p) for p in branches_ignore):
                return False
        return True
    return False


def event_declares(on_spec: Any, event_name: str) -> bool:
    """Whether a workflow's `on:` names this event, ignoring any ref filter.

    `event_matches` answers whether the event fires here, which is false both
    when the workflow never handles the event and when it handles it on other
    refs. Only the first is a repository with no CI for this event.
    """
    if isinstance(on_spec, str):
        return on_spec == event_name
    if isinstance(on_spec, list):
        return any(event_declares(item, event_name) for item in on_spec)
    if isinstance(on_spec, dict):
        return event_name in on_spec
    return False
