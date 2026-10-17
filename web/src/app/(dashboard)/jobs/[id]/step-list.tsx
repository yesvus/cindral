import { AdminSectionCard, AdminStatusPill } from "@/components/admin";
import { ChevronRight, ListChecks } from "lucide-react";
import type { JobInfo, StepInfo } from "@/lib/cindral";

/**
 * The run's steps, in order, with the one that stopped the run expanded.
 *
 * A step that never reported is shown as unfinished rather than failed: the
 * difference is whether a command ran and returned non-zero or the run was cut
 * short mid-step, and a panel that blurs them sends the reader to the wrong
 * place. Output is the step's own, not the run's tail, because the tail is
 * exactly the part that hides the step that failed first.
 */
export function StepList({ job }: { job: JobInfo }) {
  const steps = job.steps ?? [];
  return (
    <AdminSectionCard
      icon={ListChecks}
      title="Steps"
      description={describe(steps, job.status)}
    >
      {steps.length === 0 ? (
        <p className="text-sm text-admin-text-muted">{emptyState(job.status)}</p>
      ) : (
        <ol className="space-y-2">
          {steps.map((step) => (
            <StepRow
              key={step.index}
              step={step}
              open={step.index === stoppingStep(steps)}
            />
          ))}
        </ol>
      )}
    </AdminSectionCard>
  );
}

export function emptyState(status: string): string {
  // A device reports its steps once, at the end of a run, so a run that has not
  // finished has none to show and saying otherwise blames the checkout. `dead`
  // is terminal but means the run lost its device past the attempt cap, not
  // that a command failed, so it does not get the checkout diagnosis either.
  if (status === "pending" || status === "running") {
    return "This run has not reported a step yet. Steps arrive when it finishes.";
  }
  if (status === "success" || status === "failure") {
    return "This run reported no step. It ended before the first one, in the checkout or the image pull, so the output above is all there is.";
  }
  return "This run reported no step, and it did not end in a way that named one.";
}

/**
 * The step a run stopped in: the last one that did not complete.
 *
 * The broker names the last incomplete step in the commit status, and a
 * report can leave more than one incomplete, so taking the first here would
 * make the page and the commit disagree about which step failed.
 */
export function stoppingStep(steps: StepInfo[]): number | null {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    if (!steps[index].completed) return steps[index].index;
  }
  return null;
}

/**
 * The step counts, including steps a bounded record set no longer holds.
 *
 * A bounded set carries how many it dropped on its last record, so the totals
 * add it back rather than presenting a prefix as the whole run.
 *
 * `encode_steps` keeps a prefix when a set passes the count bound or the byte
 * budget, and in that shape the dropped records are the tail, so the step that
 * stopped the run can be among them. Claiming every step completed from the
 * records that survive is the one wrong thing this page must not say, so a
 * lossy set on a run that did not succeed says the later steps are not shown.
 */
export function describe(steps: StepInfo[], status: string): string {
  if (steps.length === 0) return "No step was announced";
  const lost = steps[steps.length - 1]?.dropped ?? 0;
  const total = steps.length + lost;
  const stopping = stoppingStep(steps);
  if (stopping !== null) {
    return `Stopped in step ${stopping} of ${total}`;
  }
  if (status === "success") {
    return `All ${total} steps completed`;
  }
  return lost > 0
    ? `${total} steps ran, the last ${lost} not shown`
    : `${total} steps, all completed`;
}

function StepRow({ step, open }: { step: StepInfo; open: boolean }) {
  return (
    <li
      className={
        open
          ? "rounded border border-admin-warning-border bg-admin-warning-surface"
          : "rounded border border-admin-border"
      }
    >
      <details open={open} className="group">
        <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-sm">
          <ChevronRight
            aria-hidden
            className="size-4 shrink-0 text-admin-text-muted transition-transform group-open:rotate-90"
          />
          <AdminStatusPill
            tone={step.completed ? "success" : "warning"}
            label={step.completed ? "passed" : "unfinished"}
          />
          <span className="font-mono text-xs text-admin-text-muted">
            Step {step.index}
          </span>
          <span className="min-w-0 flex-1 truncate font-mono text-xs text-admin-text-primary">
            {step.name}
          </span>
        </summary>
        {step.output ? (
          <pre className="max-h-[50vh] overflow-auto whitespace-pre-wrap break-words border-t border-admin-border px-3 py-2 font-mono text-xs text-admin-text-primary">
            {step.output}
          </pre>
        ) : (
          <p className="border-t border-admin-border px-3 py-2 text-xs text-admin-text-muted">
            This step produced no output.
          </p>
        )}
        {step.truncated ? (
          <p className="border-t border-admin-border px-3 py-2 text-xs text-admin-text-muted">
            This step&apos;s name or output was too long to keep whole.
          </p>
        ) : null}
        {step.dropped ? (
          <p className="border-t border-admin-border px-3 py-2 text-xs text-admin-text-muted">
            {step.dropped} earlier {step.dropped === 1 ? "step" : "steps"} ran and{" "}
            {step.dropped === 1 ? "is" : "are"} not shown here.
          </p>
        ) : null}
      </details>
    </li>
  );
}
