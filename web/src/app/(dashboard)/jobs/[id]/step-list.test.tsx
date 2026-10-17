import { describe, expect, it } from "vitest";

import type { StepInfo } from "@/lib/cindral";
import {
  describe as describeSteps,
  emptyState,
  stoppingStep,
} from "@/app/(dashboard)/jobs/[id]/step-list";

/**
 * The panel's step view is three pure functions of the reported records and the
 * job status. They are tested directly rather than through a rendered tree,
 * because a renderer would add a dependency to check arithmetic that a reader
 * can verify from the signature.
 *
 * Not covered, deliberately and worth knowing: which rows a render opens. The
 * `open` flag is `step.index === stoppingStep(steps)`, and reverting it to
 * `!step.completed` leaves every test here green, because deciding which rows
 * expand needs a DOM. The pure half is pinned; the rendering half is not.
 */
function step(index: number, over: Partial<StepInfo> = {}): StepInfo {
  return { index, name: `run ${index}`, output: "", completed: true, ...over };
}

describe("stoppingStep", () => {
  it("is null when every step completed", () => {
    expect(stoppingStep([step(1), step(2)])).toBeNull();
    expect(stoppingStep([])).toBeNull();
  });

  it("names the last incomplete step, the one the commit status names", () => {
    // a report can leave more than one step incomplete, and the broker takes the
    // last, so taking the first would make the page and the commit disagree
    expect(stoppingStep([step(3, { completed: false }), step(5, { completed: false }), step(7)])).toBe(5);
    expect(stoppingStep([step(1), step(2, { completed: false })])).toBe(2);
  });
});

describe("describe", () => {
  it("counts a complete set once", () => {
    const steps = [step(1), step(2), step(3)];
    expect(describeSteps(steps, "success")).toBe("All 3 steps completed");
    expect(describeSteps(steps, "failure")).toBe("3 steps, all completed");
  });

  it("names the step a run stopped in", () => {
    expect(describeSteps([step(1), step(2, { completed: false })], "failure")).toBe(
      "Stopped in step 2 of 2",
    );
  });

  it("takes the last incomplete step, matching the commit status", () => {
    expect(
      describeSteps([step(3, { completed: false }), step(4, { completed: false }), step(5)], "failure"),
    ).toBe("Stopped in step 4 of 3");
  });

  it("adds back the steps a bounded record set dropped", () => {
    // a prefix presented as the whole run is the failure the count exists for
    expect(describeSteps([step(1), step(2), step(3), step(4, { dropped: 66 })], "success")).toBe(
      "All 70 steps completed",
    );
  });

  it("never reports a numerator above its denominator", () => {
    // a set bounded by both the count and the budget once reported fewer steps
    // in total than it held, and the page rendered "step 70 of 64"
    const summary = describeSteps([step(1), step(2, { completed: false, dropped: 68 })], "failure");
    const match = /Stopped in step (\d+) of (\d+)/.exec(summary);
    expect(match).not.toBeNull();
    expect(Number(match?.[1])).toBeLessThanOrEqual(Number(match?.[2]));
    expect(summary).toBe("Stopped in step 2 of 70");
  });

  it("does not claim every step completed when the dropped ones are the tail", () => {
    // `encode_steps` keeps a prefix once a set passes the count bound, so the
    // record that stopped the run can be one of the dropped records. Saying
    // "all completed" about a failed run from the surviving prefix is the one
    // thing this page must never do.
    expect(
      describeSteps([step(1), step(2), step(3, { dropped: 68 })], "failure"),
    ).toBe("71 steps ran, the last 68 not shown");
  });

  it("still claims completion for a lossy set on a run that succeeded", () => {
    // the same shape on a green run is true, because the end marker is only
    // printed on a zero exit
    expect(describeSteps([step(1), step(2, { dropped: 68 })], "success")).toBe(
      "All 70 steps completed",
    );
  });

  it("says nothing was announced when there are no steps", () => {
    expect(describeSteps([], "failure")).toBe("No step was announced");
  });
});

describe("emptyState", () => {
  it("says a run still going has not reported a step", () => {
    // a device reports steps once, at the end of a run, so an empty set on a
    // queued or running job is a run in progress, not one that died early
    for (const status of ["pending", "running"]) {
      expect(emptyState(status)).toContain("has not reported a step yet");
    }
  });

  it("blames the checkout only for a run that finished on its own terms", () => {
    for (const status of ["success", "failure"]) {
      expect(emptyState(status)).toContain("ended before the first one");
    }
  });

  it("does not blame the checkout for a run that lost its device", () => {
    // `dead` is terminal, and means the run lost its device past the attempt
    // cap rather than that a command failed
    expect(emptyState("dead")).not.toContain("checkout");
    expect(emptyState("dead")).toContain("did not end in a way that named one");
  });
});
