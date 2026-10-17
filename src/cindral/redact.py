"""Credentials removed from text copied out of a device run.

A commit status is built from the step's declared command and never from its
output, because a commit is public and a step's output is not. A check run panel
renders the log, so the same text has to be scrubbed on the way out. The
patterns cover the credential shapes that reach a CI log: a token a step
interpolated into its own command line, a private key a build wrote to the
workspace, a URL carrying a password.

A secret printed with no recognizable shape is not caught here, because a
pattern cannot be sound about arbitrary text. The log is therefore published
only for a run that did not pass, the removal is disclosed in the panel beside
the log it removed from, and the operator who needs the raw text reads it from
the job page, which is behind the broker's read token.
"""
from dataclasses import replace
import re

from .jobrecord import Job

REDACTED = "[redacted]"
# A setting name is a prefix chain followed by a word that names a credential:
# GITHUB_TOKEN, AWS_SECRET_ACCESS_KEY, and X-Auth-Token all parse as one.
_NAMED = (
    r"(?:[A-Za-z0-9]+[_.\-])*"
    r"(?:token|secret|password|passwd|credential"
    r"|api[_.\-]?key|access[_.\-]?key|auth[_.\-]?token|private[_.\-]?key|encryption[_.\-]?key)s?"
)

# The body of a PEM block is dropped between its markers rather than with them,
# so a reader can tell a key was here and see that none of it survived. A key
# split across the lines a log already holds is one key, so matching the body
# line by line would leave the pieces readable in order.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----).*?(-----END [A-Z ]*PRIVATE KEY-----)", re.DOTALL),
        r"\1 [REDACTED] \2",
    ),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"), REDACTED),
    (re.compile(r"\b(?:A3T[A-Z0-9]|A(?:BIA|CCA|GPA|IDA|IPA|KIA|NPA|NVA|ROA|SCA|SIA))[A-Z0-9]{16}\b"), REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"), REDACTED),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{8,}\b"), REDACTED),
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), REDACTED),
    (re.compile(r"\b[sr]k_live_[A-Za-z0-9]{12,}\b"), REDACTED),
    # An auth header keeps its name and loses its scheme with its value: a
    # surviving `Bearer` is a value that got half removed.
    (
        re.compile(r"(?i)\b(authorization|proxy-authorization)\b(\s*[:=]\s*)(?:bearer|token|basic|digest)?\s*\S+"),
        r"\1\2" + REDACTED,
    ),
    (re.compile(r"(?i)\b(bearer)\s+\S+"), r"\1 " + REDACTED),
    # A named setting keeps its name for the same reason. The name is matched as
    # a whole identifier rather than a bare word, because an environment
    # variable is the ordinary form: GITHUB_TOKEN, AWS_SECRET_ACCESS_KEY, and
    # ?access_token= are all the same setting with a prefix in front. A bare
    # `key` is not in the list: in a log it is far more often a sort key or a
    # map key than a credential, and a scrubber that cannot be sound should not
    # also be noisy.
    (re.compile(rf"(?i)\b({_NAMED})\b(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)"), r"\1\2" + REDACTED),
    # the userinfo of a URL, which is how a clone URL and a registry login
    # carry a credential
    (re.compile(r"(?<=://)[^\s/@]+:[^\s/@]+(?=@)"), REDACTED),
)


def redact(text: str) -> str:
    """The text with every credential it recognizes replaced by a marker."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scrubbed(text: str) -> bool:
    """Whether `redact` would change the text, so a reader can be told it did."""
    return any(pattern.search(text) for pattern, _ in _PATTERNS)


def redacted_job(job: Job) -> Job:
    """The record with every field a device writes text into scrubbed.

    The summary, the title, and the annotation are all built from the record, so
    one pass here covers them together rather than each rendering having to
    remember to scrub the loss it prints.
    """
    return replace(
        job,
        log=redact(job.log),
        last_error=redact(job.last_error) if job.last_error else job.last_error,
        steps=tuple({**step, "output": redact(str(step.get("output", "")))} for step in job.steps),
    )
