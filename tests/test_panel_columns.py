"""The panel's declared run columns, against the payload the broker actually sends.

The panel and the broker are one repository and two deployables, and a run's shape
crosses between them: the broker decides which keys a job payload carries, and the
panel declares which of them it will show and which it would accept on a write. A
key the broker sends and the panel does not declare is refused **by name** on a
write, so the panel's column list has to name everything the broker sends.

This check lives here rather than in the panel's own test suite for a concrete
reason: the panel's CI step copies `web/` into a container and nothing else, so a
test in `web/` cannot read `src/cindral/jobrecord.py`. A guard written where the
file is not present is not a guard, and it passes locally while failing in CI,
which is worse than having none.

What the panel's own suite does check is that its columns and its own TypeScript
interface agree. That is a real check and it is not this one: it cannot see the
broker, and it was green while `last_error` sat in every payload undeclared.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _panel_columns() -> set[str]:
    """The run keys `web/src/lib/resources.ts` declares."""
    source = (ROOT / "web" / "src" / "lib" / "resources.ts").read_text()
    body = source[source.index("JOB_COLUMNS"):]
    body = body[: body.index("\n];")]
    return set(re.findall(r'key:\s*"([a-z_]+)"', body))


def _broker_job_keys() -> set[str]:
    """Every key a run's payload carries, from both places the broker sets one.

    `Job.as_dict()` is the record. `pool.py` adds to it on the snapshot path, which
    is where `log_truncated` comes from, so reading only the record would be blind
    to exactly the kind of key that gets added later without ceremony.
    """
    record = (ROOT / "src" / "cindral" / "jobrecord.py").read_text()
    body = record[record.index("def as_dict"):]
    keys = set(re.findall(r'"([a-z_]+)":', body))
    pool = (ROOT / "src" / "cindral" / "pool.py").read_text()
    keys |= set(re.findall(r'job\["([a-z_]+)"\]\s*=', pool))
    return keys


class PanelColumnDriftTest(unittest.TestCase):
    def test_the_panel_declares_every_key_a_run_payload_carries(self) -> None:
        # `id` is the one key a record may carry that no definition names: the
        # boundary decides it itself, writable on a create and refused on an update
        sent = _broker_job_keys() - {"id"}
        declared = _panel_columns()
        self.assertEqual(
            sorted(sent - declared),
            [],
            "the broker sends run keys the panel does not declare, and a write "
            "naming one is refused by name",
        )
        self.assertEqual(
            sorted(declared - sent),
            [],
            "the panel declares run columns the broker never sends, so every row "
            "renders them empty",
        )

    def test_the_read_is_anchored_to_the_broker_rather_than_the_panel(self) -> None:
        # A list typed into the test would be the list the panel already had, and
        # comparing against it is the comparison that already passed while the
        # two disagreed. This asserts the guard reads the broker's own files.
        source = (ROOT / "tests" / "test_panel_columns.py").read_text()
        self.assertIn("jobrecord.py", source)
        self.assertIn("pool.py", source)