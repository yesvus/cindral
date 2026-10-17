"""Reconcile direct-execution webhooks for policy-owned and discovered repositories."""
from argparse import Namespace
from collections.abc import Callable, Iterable, Sequence
import sys
from urllib.parse import urlsplit

from .github import DiscoveredRepository, GitHubAPIError, GitHubClient, is_repository_slug
from .dispatch_targets import INVALID_REF_FRAGMENTS
from .policy import Policy


DEFAULT_OPS_REPOSITORY: str | None = None


def validate_webhook_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("webhook URL must be an HTTPS URL without embedded credentials")


def device_repositories(policy: Policy) -> tuple[str, ...]:
    return tuple(target.repository for target in policy.dispatch_targets if target.executes_on_device)


def reconcile_webhooks(
    repositories: tuple[str, ...],
    register: Callable[[str], str],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    changed: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    for repository in repositories:
        try:
            changed.append((repository, register(repository)))
        except (ValueError, RuntimeError) as exc:
            failed.append((repository, str(exc)))
    return changed, failed


def slug_set(raw: str | Iterable[str]) -> frozenset[str]:
    """Parse a comma-separated or iterable list of owner/name slugs."""
    parts = raw.split(",") if isinstance(raw, str) else raw
    return frozenset(part.strip().lower() for part in parts if part.strip())


def trusted_default_branch(branch: str) -> bool:
    """Whether a default branch name can be a trusted dispatch ref.

    The broker dispatches only a signed push to the repository's default
    branch, and the policy accepts ref names that carry no ambiguous fragment,
    so a default branch failing the same shape is never a trusted event source.
    """
    return bool(branch) and not any(fragment in branch for fragment in INVALID_REF_FRAGMENTS)


def select_adoption(
    discovered: Iterable[DiscoveredRepository],
    allowlist: frozenset[str] = frozenset(),
    all_owned: bool = False,
    exclusions: frozenset[str] = frozenset(),
    ops_repository: str | None = DEFAULT_OPS_REPOSITORY,
) -> tuple[tuple[DiscoveredRepository, ...], tuple[tuple[str, str], ...], int]:
    """Choose what one adoption pass touches.

    Adoption is never implicit: without an explicit allowlist or ``all_owned``
    nothing is selected, whatever the inventory holds. Returns the eligible
    selection in slug order, the selected repositories that must be skipped
    with a reason, and how many owned repositories the selection does not
    touch. A named repository outside the owned inventory is reported, never
    silently dropped.
    """
    by_slug = {item.repository.lower(): item for item in discovered}
    candidates = by_slug if all_owned else {slug: by_slug[slug] for slug in allowlist if slug in by_slug}
    eligible: list[DiscoveredRepository] = []
    skips: list[tuple[str, str]] = []
    if not all_owned:
        for slug in sorted(allowlist):
            if slug not in by_slug:
                skips.append((slug, "credential does not own this repository"))
    for slug in sorted(candidates):
        reason = _skip_reason(candidates[slug], exclusions, ops_repository)
        if reason is None:
            eligible.append(candidates[slug])
        else:
            skips.append((slug, reason))
    skips.sort()
    return tuple(eligible), tuple(skips), len(discovered) - len(candidates)


def _skip_reason(
    item: DiscoveredRepository, exclusions: frozenset[str], ops_repository: str | None
) -> str | None:
    repository = item.repository.lower()
    if ops_repository and repository == ops_repository.lower():
        return "repository owns the deployment policy"
    if repository in exclusions:
        return "excluded by CINDRAL_ADOPT_EXCLUDE"
    if not item.private:
        return "public repositories route to hosted runners"
    if item.archived:
        return "archived repositories accept no pushes"
    if not item.default_branch:
        return "repository has no default branch"
    if not trusted_default_branch(item.default_branch):
        return "default branch is not a trusted branch name"
    return None


def definition_note(item: DiscoveredRepository) -> str:
    """What the default branch offers direct execution, as far as listing shows."""
    if item.has_contract and item.has_workflows:
        return "committed contract and workflow files"
    if item.has_contract:
        return "committed contract"
    if item.has_workflows:
        return "workflow files"
    return "no committed CI definition; the device infers one from the tree"


def adoption_allowlist(
    named: Sequence[str] | None, from_env: str, error: Callable[[str], None]
) -> frozenset[str]:
    """The repositories the operator named, from one source or the other.

    Naming a repository and setting the environment variable are separate
    opt-ins, like ``--all-owned``. Unioning them let an ambient variable adopt
    repositories nobody typed, which is why this refuses rather than merges.

    ``error`` reports a fatal configuration problem and must not return.
    """
    typed = tuple(named or ())
    ambient = slug_set(from_env)
    if typed and ambient:
        error("--repository and CINDRAL_ADOPT_REPOSITORIES are separate opt-ins; set one")
    return frozenset(ambient or slug_set(",".join(typed)))


def run_adoption(
    args: Namespace,
    github: GitHubClient,
    secret: str | None,
    exclusions: frozenset[str],
    allowlist: frozenset[str],
    error: Callable[[str], None],
) -> bool:
    """Run one adoption pass and report it. False means the run must exit nonzero.

    ``error`` reports a fatal configuration problem and must not return. The
    default pass adopts nothing: applying requires ``all_owned`` or a named
    allowlist, and a named repository that cannot be adopted fails the apply.
    """
    if args.ops_repository is None or not is_repository_slug(args.ops_repository):
        error("set --ops-repository or CINDRAL_OPS_REPOSITORY to the deployment-policy repository")
    if args.all_owned and allowlist:
        error("--all-owned and a named allowlist are separate opt-ins; pass one")
    if args.apply and not allowlist and not args.all_owned:
        error("name what to adopt: repeat --repository OWNER/NAME, set CINDRAL_ADOPT_REPOSITORIES, or pass --all-owned")
    if args.apply:
        if not args.webhook_url or not secret:
            error("set CINDRAL_WEBHOOK_URL and the webhook secret environment variable before applying")
        try:
            validate_webhook_url(args.webhook_url)
        except ValueError as exc:
            error(str(exc))
    try:
        discovered = github.list_repositories(probe=not args.apply)
    except GitHubAPIError as exc:
        print(f"Repository discovery failed: {exc}", file=sys.stderr)
        return False
    adoptable = sum(_skip_reason(item, exclusions, args.ops_repository) is None for item in discovered)
    print(f"Credential owns {len(discovered)} repositories: {adoptable} adoptable, {len(discovered) - adoptable} outside adoption")
    if not allowlist and not args.all_owned:
        print("Nothing is selected for adoption; pass --repository OWNER/NAME for each repository, set CINDRAL_ADOPT_REPOSITORIES, or pass --all-owned")
        for item in sorted(discovered, key=lambda entry: entry.repository.lower()):
            reason = _skip_reason(item, exclusions, args.ops_repository)
            if reason is None:
                print(f"  {item.repository}: not selected ({definition_note(item)})")
            else:
                print(f"  {item.repository}: skipped: {reason}")
        return True
    eligible, skips, unselected = select_adoption(discovered, allowlist, args.all_owned, exclusions, args.ops_repository)
    if args.all_owned:
        print("Selection: every owned private repository, opted in with --all-owned")
    else:
        named = "with --repository" if args.repository else "with CINDRAL_ADOPT_REPOSITORIES"
        print(f"Selection: {len(allowlist)} named {named}")
    for repository, reason in skips:
        print(f"  {repository}: skipped: {reason}")
    if unselected:
        noun = "repository" if unselected == 1 else "repositories"
        verb = "was" if unselected == 1 else "were"
        print(f"{unselected} owned {noun} outside the selection {verb} not touched")
    if not args.apply:
        webhook_url = args.webhook_url or "(set CINDRAL_WEBHOOK_URL)"
        for item in eligible:
            print(f"  {item.repository}: would reconcile {webhook_url} ({definition_note(item)})")
        return True
    changed, failed = reconcile_webhooks(
        tuple(item.repository for item in eligible),
        lambda repository: github.ensure_push_webhook(repository, args.webhook_url, secret),
    )
    for repository, result in changed:
        print(f"  {repository}: {result} webhook")
    for repository, failure in failed:
        print(f"  {repository}: {failure}", file=sys.stderr)
    return not failed and not (skips and not args.all_owned)
