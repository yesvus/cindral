"""Tests for reconstructing per-step records from a run's log."""
import json
import unittest

from cindral.steps import (
    MAX_STEP_CHARS,
    MAX_STEP_NAME_CHARS,
    MAX_STEPS,
    Step,
    decode_steps,
    encode_steps,
    failed_step_index,
    parse_steps,
)

NONCE = "a" * 32
OTHER_NONCE = "f" * 32


def start(index: int, name: str, nonce: str = NONCE) -> str:
    return f"cindral: step {index} {nonce}: {name}\n"


def end(index: int, nonce: str = NONCE) -> str:
    return f"cindral: step {index} {nonce} end\n"


def steps_of(log: str) -> list[Step]:
    return parse_steps(log, NONCE)


# a run that got through install and failed the ci step, with the end markers
# absent on the step that never returned
TWO_STEPS = (
    "cindral: job 1 example-org/app@abc\n"
    + start(1, "pnpm install")
    + "added 412 packages\n"
    + end(1)
    + start(2, "pnpm run ci")
    + "assertion failed\n"
)


class ParseStepsTest(unittest.TestCase):
    def test_each_step_carries_its_own_output_and_command(self) -> None:
        steps = steps_of(TWO_STEPS)
        self.assertEqual([(s.index, s.name) for s in steps], [(1, "pnpm install"), (2, "pnpm run ci")])
        self.assertIn("added 412 packages", steps[0].output)
        self.assertNotIn("assertion failed", steps[0].output)
        self.assertIn("assertion failed", steps[1].output)

    def test_an_end_marker_marks_a_step_complete(self) -> None:
        self.assertEqual([s.completed for s in steps_of(TWO_STEPS)], [True, False])

    def test_a_run_with_no_marker_announced_no_step(self) -> None:
        self.assertEqual(steps_of("cindral: checkout failed\n"), [])

    def test_the_last_step_of_a_failed_run_is_the_one_that_failed(self) -> None:
        self.assertEqual(failed_step_index(TWO_STEPS, steps_of(TWO_STEPS)), 2)

    def test_a_fully_completed_run_has_no_failed_step(self) -> None:
        log = "".join(start(i, f"run {i}") + f"ok {i}\n" + end(i) for i in range(1, 4))
        self.assertIsNone(failed_step_index(log, steps_of(log)))

    def test_output_is_bounded_at_the_tail_that_names_the_failure(self) -> None:
        log = start(1, "build") + ("x" * (MAX_STEP_CHARS * 2)) + "\nboom\n"
        step = steps_of(log)[0]
        self.assertTrue(step.truncated)
        self.assertLessEqual(len(step.output), MAX_STEP_CHARS)
        self.assertIn("boom", step.output)

    def test_step_count_is_bounded(self) -> None:
        log = "".join(start(i, "run") for i in range(MAX_STEPS + 20))
        self.assertEqual(len(steps_of(log)), MAX_STEPS)

    def test_a_marker_from_another_run_is_not_a_boundary(self) -> None:
        # a step that prints a marker-shaped line cannot invent a step, because
        # it cannot know the nonce this run's markers carry
        log = start(1, "echo") + start(2, "forged", OTHER_NONCE) + "spoofed\n"
        steps = steps_of(log)
        self.assertEqual(len(steps), 1)
        self.assertIn("spoofed", steps[0].output)
        self.assertIn("forged", steps[0].output)

    def test_a_marker_naming_the_next_step_number_is_still_output(self) -> None:
        # the sequence alone is not enough, which is what the nonce buys
        log = start(1, "echo") + start(2, "forged") + "spoofed\n"
        self.assertEqual(len(steps_of(log)), 2)
        self.assertIn("spoofed", steps_of(log)[1].output)

    def test_a_declared_sequence_still_splits_at_every_step(self) -> None:
        log = "".join(start(i, f"run {i}") + f"out {i}\n" + end(i) for i in range(1, 6))
        self.assertEqual(
            [(s.index, s.output.strip()) for s in steps_of(log)],
            [(1, "out 1"), (2, "out 2"), (3, "out 3"), (4, "out 4"), (5, "out 5")],
        )


class BudgetTest(unittest.TestCase):
    """A chatty run must still produce a report the broker will accept."""

    def chatty(self, count: int, last: str = "") -> str:
        log = "".join(
            start(i, f"run {i}") + ("x" * 20000) + f"\nout {i}\n" + end(i) for i in range(1, count)
        )
        return log + start(count, last or "the failing one") + "boom\n"

    def test_the_record_set_fits_the_broker_body_limit(self) -> None:
        steps = steps_of(self.chatty(20))
        body = json.dumps({"log": "x" * (64 * 1024), "steps": [s.as_dict() for s in steps]})
        self.assertLess(len(body), 1 << 20)

    def test_a_bounded_set_keeps_the_step_the_run_stopped_in(self) -> None:
        steps = steps_of(self.chatty(20))
        self.assertEqual(steps[-1].index, 20)
        self.assertEqual(steps[-1].name, "the failing one")
        self.assertFalse(steps[-1].completed)

    def test_a_bounded_set_says_it_is_bounded(self) -> None:
        # otherwise it is shape-identical to a short run that really had four steps
        steps = steps_of(self.chatty(20))
        self.assertGreater(steps[-1].dropped, 0)
        self.assertEqual(encode_steps([s.as_dict() for s in steps])[-1]["dropped"], steps[-1].dropped)

    def test_the_count_bound_says_it_dropped(self) -> None:
        # the count bound loses steps too, and a set that lost them while
        # reporting zero dropped is what finding the output bound existed to kill
        steps = steps_of("".join(
            start(i, f"run {i}") + f"out {i}\n" + end(i) for i in range(1, 101)
        ))
        self.assertEqual(len(steps), MAX_STEPS)
        self.assertEqual(steps[-1].index, 100)
        self.assertEqual(steps[-1].dropped, 100 - MAX_STEPS)
        self.assertEqual(encode_steps([s.as_dict() for s in steps])[-1]["dropped"], 100 - MAX_STEPS)

    def test_a_step_name_longer_than_a_command_line_still_fits(self) -> None:
        # a step's name is repository-controlled and can be as long as a command
        # line; bounding it only at validation was too late, because the body
        # had already been built and was refused with the verdict still on it
        steps = steps_of(start(1, "x" * 1_500_000) + "out\n")
        self.assertEqual(len(steps), 1)
        self.assertEqual(len(steps[0].name), MAX_STEP_NAME_CHARS)
        self.assertTrue(steps[0].truncated)
        body = json.dumps({"log": "x" * 65536, "steps": encode_steps([s.as_dict() for s in steps])})
        self.assertLess(len(body), 1 << 20)

    def test_both_bounds_contribute_to_one_dropped_count(self) -> None:
        # a set bounded by the count and by the budget reported only the second
        # bound's count, so a reader was told a total smaller than the run had
        # steps, and a numerator could exceed its denominator
        log = "".join(
            start(i, f"run {i}") + ("x" * 1200) + f"\nout {i}\n" + end(i) for i in range(1, 70)
        ) + start(70, "the failing one") + "boom\n"
        steps = steps_of(log)
        self.assertGreater(len(steps), 1)
        self.assertEqual(steps[-1].index, 70)
        self.assertEqual(len(steps) + steps[-1].dropped, 70)

    def test_a_short_run_is_not_marked_truncated(self) -> None:
        steps = steps_of(self.chatty(3))
        self.assertEqual([s.dropped for s in steps], [0, 0, 0])


class EncodeStepsTest(unittest.TestCase):
    @staticmethod
    def report(count, body="y" * 4000, **over):
        records = [
            {"index": index, "name": f"s{index}", "output": body, "completed": True,
             "truncated": False, "dropped": 0}
            for index in range(1, count + 1)
        ]
        for position, fields in over.items():
            records[int(position) - 1].update(fields)
        return records

    def total_of(self, records) -> int:
        stored = decode_steps(json.dumps(encode_steps(records)))
        return len(stored) + stored[-1]["dropped"] if stored else 0

    def test_a_report_longer_than_the_step_cap_says_so(self) -> None:
        # the cap discarded these with no count, so a 100-step run stored 64
        # records and read as a complete one
        self.assertEqual(self.total_of(self.report(100, body="ok")), 100)

    def test_a_count_on_a_non_final_record_is_not_counted_twice(self) -> None:
        # the report claims 30 steps across 20 records, and the record holding
        # the claim is itself past the budget, so adding its own count to the
        # reported total reported 40 for a 30-step run
        records = self.report(20, **{"13": {"dropped": 10}, "14": {"output": "z" * 40000}})
        self.assertEqual(self.total_of(records), 30)

    def test_a_count_on_the_last_record_agrees_with_one_on_an_earlier_record(self) -> None:
        last = self.report(20, **{"20": {"dropped": 10}, "14": {"output": "z" * 40000}})
        self.assertEqual(self.total_of(last), 30)

    def test_an_over_cap_report_that_states_a_count_keeps_it(self) -> None:
        # the cap fold replaced the stated count with slice arithmetic, so a
        # 65-record report claiming 10 more reported 65 steps ran rather than 75
        self.assertEqual(self.total_of(self.report(65, body="ok", **{"65": {"dropped": 10}})), 75)

    def test_an_over_cap_report_that_states_a_count_keeps_it_when_both_bounds_fire(self) -> None:
        # the budget fold computed the right total and the cap fold then
        # overwrote it with the slice count
        records = self.report(100, body="y" * 2000, **{"100": {"dropped": 10}})
        self.assertEqual(self.total_of(records), 110)

    def test_entries_the_validator_rejects_are_not_counted_as_run(self) -> None:
        # a report carrying entries the loop skips describes fewer steps than it
        # has lines, and the fold counted the lines
        records = ["nope", {"index": True}, {"name": "no index"}]
        records += self.report(70, body="")
        self.assertEqual(self.total_of(records), 70)

    def test_a_negative_reported_count_is_not_stored(self) -> None:
        for stated, expected in ((-5, 0), (True, 0), ("7", 0), (7, 7)):
            with self.subTest(stated=stated):
                stored = encode_steps([
                    {"index": 2, "name": "s", "output": "", "completed": False, "dropped": stated}
                ])
                self.assertEqual(stored[-1]["dropped"], expected)

    def test_the_budget_is_charged_the_bounded_name(self) -> None:
        # the charge used the unbounded name and the record stored a bounded
        # one, so records were dropped for a budget the stored set did not
        # reach: 64 names of 1000 characters charge 76_800 and store 44_800,
        # which is under the budget
        records = self.report(64, body="z" * 200, **{
            str(index): {"name": "n" * 1000} for index in range(1, 65)
        })
        stored = decode_steps(json.dumps(encode_steps(records)))
        self.assertEqual(len(stored), 64)
        self.assertEqual(stored[-1]["dropped"], 0)

    def test_a_reported_dropped_count_survives_the_report_budget(self) -> None:
        # the record carrying the count is the one the budget discards, so a
        # bounded set lost its count entirely and read as a complete run
        reported = [
            {"index": index, "name": f"run {index}", "output": "y" * 4000,
             "completed": True, "truncated": False, "dropped": 0}
            for index in range(1, 21)
        ]
        reported[-1].update(dropped=30, completed=False, name="the failing one", output="boom")
        stored = encode_steps(reported)
        self.assertLess(len(stored), len(reported))
        self.assertEqual(len(stored) + stored[-1]["dropped"], 50)


    def test_a_reported_step_is_stored_with_its_fields(self) -> None:
        stored = encode_steps([{"index": 2, "name": "pnpm run ci", "output": "boom", "completed": False}])
        self.assertEqual(
            stored,
            [{
                "index": 2, "name": "pnpm run ci", "output": "boom",
                "completed": False, "truncated": False, "dropped": 0,
            }],
        )

    def test_a_malformed_report_is_dropped_rather_than_stored(self) -> None:
        self.assertEqual(encode_steps("not a list"), [])
        self.assertEqual(encode_steps([{"name": "no index", "output": ""}]), [])
        self.assertEqual(encode_steps([{"index": True, "name": "bool index"}]), [])
        self.assertEqual(encode_steps(["a string"]), [])

    def test_a_reported_step_is_bounded_like_a_parsed_one(self) -> None:
        stored = encode_steps([
            {"index": 1, "name": "n" * 5000, "output": "y" * (MAX_STEP_CHARS * 2), "completed": True}
        ])
        self.assertLessEqual(len(stored[0]["name"]), 500)
        self.assertLessEqual(len(stored[0]["output"]), MAX_STEP_CHARS)
        self.assertTrue(stored[0]["truncated"])

    def test_a_round_trip_through_storage_returns_the_records(self) -> None:
        stored = encode_steps([{"index": 1, "name": "build", "output": "ok", "completed": True, "dropped": 0}])
        self.assertEqual(decode_steps(json.dumps(stored)), stored)

    def test_absent_or_corrupt_stored_steps_read_as_no_steps(self) -> None:
        self.assertEqual(decode_steps(None), [])
        self.assertEqual(decode_steps(""), [])
        self.assertEqual(decode_steps("{not json"), [])
