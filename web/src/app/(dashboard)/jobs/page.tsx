import {
  AdminPageHeader,
  AdminStatusPill,
  AdminTable,
  type AdminTableColumn,
  AdminSectionCard,
} from "@yesvus/helmdeck";
import { Activity } from "lucide-react";
import { getPoolSnapshot, type JobInfo } from "@/lib/cindral";

export const dynamic = "force-dynamic";

export default async function JobsPage() {
  const snapshot = await getPoolSnapshot();
  const jobs = snapshot?.recent_jobs ?? [];

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
            <div className="text-[10px] text-amber-600 dark:text-amber-400 font-medium">
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
          <div className="font-medium text-zinc-900 dark:text-zinc-100">{j.repository}</div>
          <div className="text-xs text-zinc-500 font-mono">
            {j.ref} @ {j.sha.slice(0, 7)}
          </div>
        </div>
      ),
    },
    {
      key: "status",
      header: "Outcome",
      cell: (j) => {
        let tone: "success" | "warning" | "error" | "info" | "neutral" = "neutral";
        if (j.status === "success") tone = "success";
        else if (j.status === "failure") tone = "error";
        else if (j.status === "running") tone = "warning";
        else if (j.status === "pending") tone = "info";

        return (
          <div className="flex items-center gap-1.5">
            <AdminStatusPill tone={tone} label={j.status} />
            {j.exit_code !== null && (
              <span className="text-xs font-mono text-zinc-500">({j.exit_code})</span>
            )}
          </div>
        );
      },
    },
    {
      key: "device",
      header: "Assigned Device",
      cell: (j) => (
        <span className="text-sm font-medium text-zinc-700 dark:text-zinc-300">
          {j.device || <span className="text-zinc-400 italic">None</span>}
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
      cell: (j) => {
        if (!j.log) {
          return <span className="text-xs text-zinc-400">No output</span>;
        }
        return (
          <details className="text-xs group">
            <summary className="cursor-pointer text-amber-600 dark:text-amber-400 hover:underline">
              View Log Tail ({j.log.length} chars)
            </summary>
            <pre className="mt-2 max-h-48 max-w-lg overflow-auto rounded bg-zinc-950 p-2 font-mono text-[11px] text-zinc-100 whitespace-pre-wrap">
              {j.log}
            </pre>
          </details>
        );
      },
    },
  ];

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
          rows={jobs}
          getKey={(j) => j.id}
          empty={<div className="p-8 text-center text-sm text-zinc-500">No jobs currently recorded</div>}
        />
      </AdminSectionCard>
    </div>
  );
}
