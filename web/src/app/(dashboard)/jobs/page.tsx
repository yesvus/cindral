import {
  AdminBanner,
  AdminPageHeader,
  AdminStatusPill,
  AdminTable,
  type AdminTableColumn,
  AdminSectionCard,
} from "@yesvus/helmdeck";
import { Activity } from "lucide-react";
import { jobStatusTone, type JobInfo } from "@/lib/cindral";
import { loadPool } from "@/lib/load-pool";

export const dynamic = "force-dynamic";

const columns: AdminTableColumn<JobInfo>[] = [
  {
    key: "id",
    header: "Job ID",
    cell: (j) => (
      <div>
        <span className="font-mono text-xs font-semibold text-zinc-900 dark:text-zinc-100">
          {j.id.slice(0, 8)}
        </span>
        {j.attempts > 1 && (
          <div className="text-[10px] font-medium text-amber-600 dark:text-amber-400">
            Attempt #{j.attempts}
          </div>
        )}
      </div>
    ),
  },
  {
    key: "repository",
    header: "Repository & Target",
    cell: (j) => (
      <div className="space-y-0.5">
        <div className="font-medium text-zinc-900 dark:text-zinc-100">
          {j.repository}
        </div>
        <div className="font-mono text-xs text-zinc-500">
          {j.ref} @ {j.sha.slice(0, 7)}
        </div>
      </div>
    ),
  },
  {
    key: "status",
    header: "Outcome",
    cell: (j) => (
      <div className="flex items-center gap-1.5">
        <AdminStatusPill tone={jobStatusTone(j.status)} label={j.status} />
        {j.exit_code !== null && (
          <span className="font-mono text-xs text-zinc-500">({j.exit_code})</span>
        )}
      </div>
    ),
  },
  {
    key: "device",
    header: "Assigned Device",
    cell: (j) => (
      <span className="text-sm font-medium text-zinc-700 dark:text-zinc-300">
        {j.device || <span className="italic text-zinc-400">None</span>}
      </span>
    ),
  },
  {
    key: "command",
    header: "Contract Command",
    cell: (j) => (
      <div className="max-w-xs truncate font-mono text-xs text-zinc-600 dark:text-zinc-400">
        {j.command.length > 0 ? j.command.join(" ") : "default ci"}
      </div>
    ),
  },
  {
    key: "log",
    header: "Log Output",
    cell: (j) =>
      j.log ? (
        <details className="group text-xs">
          <summary className="cursor-pointer text-amber-600 hover:underline dark:text-amber-400">
            View log tail
            {j.log_truncated ? " (truncated)" : ""}
          </summary>
          <pre className="mt-2 max-h-48 max-w-lg overflow-auto whitespace-pre-wrap rounded bg-zinc-950 p-2 font-mono text-[11px] text-zinc-100">
            {j.log}
          </pre>
        </details>
      ) : (
        <span className="text-xs text-zinc-400">No output</span>
      ),
  },
];

export default async function JobsPage() {
  const result = await loadPool();

  if (!result.ok) {
    return (
      <div className="space-y-6">
        <AdminPageHeader title="Queue & Run History" />
        <AdminBanner
          tone={result.reason === "unauthorized" ? "error" : "warning"}
          title={
            result.reason === "unauthorized"
              ? "Operator token required"
              : "Broker unavailable"
          }
          body={result.message}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <AdminPageHeader title="Queue & Run History" />

      <AdminSectionCard
        icon={Activity}
        title="Direct Execution Queue"
        description="Active leases, queued commits, and completed device runs"
      >
        <AdminTable
          columns={columns}
          rows={result.snapshot.recent_jobs}
          getKey={(j) => j.id}
          empty={
            <div className="p-8 text-center text-sm text-zinc-500">
              No jobs currently recorded
            </div>
          }
        />
      </AdminSectionCard>
    </div>
  );
}
