import Link from "next/link";
import { AdminBanner, AdminPageHeader, AdminSectionCard, AdminStatusPill } from "@/components/admin";
import { Activity } from "lucide-react";
import { BrokerError, jobStatusTone, type JobInfo } from "@/lib/cindral";
import { resourceActions } from "@/lib/broker-store";
import { JOB_RESOURCE } from "@/lib/resources";
import { StepList } from "./step-list";

export const dynamic = "force-dynamic";

export default async function JobPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let job: JobInfo | null;
  try {
    job = await resourceActions.read<JobInfo>(JOB_RESOURCE, id);
  } catch (error) {
    const message = error instanceof BrokerError ? error.message : "job lookup failed";
    return (
      <div className="space-y-6">
        <AdminPageHeader title="Run details" />
        <AdminBanner tone="error" title="Run unavailable" body={message} />
        <Link href="/jobs" className="text-sm text-admin-text-primary underline">Back to queue</Link>
      </div>
    );
  }

  if (!job) {
    return (
      <div className="space-y-6">
        <AdminPageHeader title="Run details" />
        <AdminBanner
          tone="warning"
          title="No such run"
          body="The broker holds no run with that id, so there is nothing to show for it."
        />
        <Link href="/jobs" className="text-sm text-admin-text-primary underline">Back to queue</Link>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <AdminPageHeader title={`Run ${job.id.slice(0, 8)}`} />
      <Link href="/jobs" className="text-sm text-admin-text-primary underline">Back to queue</Link>
      <AdminSectionCard icon={Activity} title="Run summary" description={`${job.repository} · ${job.ref} · ${job.sha.slice(0, 7)}`}>
        <div className="grid gap-4 text-sm sm:grid-cols-3">
          <div><span className="text-admin-text-muted">Status</span><div className="mt-1"><AdminStatusPill tone={jobStatusTone(job.status)} label={job.status} /></div></div>
          <div><span className="text-admin-text-muted">Device</span><div>{job.device ?? "Waiting for a device"}</div></div>
          <div><span className="text-admin-text-muted">Exit code</span><div>{job.exit_code ?? "Pending"}</div></div>
        </div>
        <p className="mt-4 font-mono text-xs text-admin-text-secondary">{job.repository}@{job.sha}</p>
      </AdminSectionCard>
      <StepList job={job} />
      <AdminSectionCard icon={Activity} title="Output" description={job.log_truncated ? "The retained output is truncated." : "Output retained by the broker."}>
        {job.log ? (
          <pre className="max-h-[65vh] overflow-auto whitespace-pre-wrap break-words rounded border border-admin-border bg-admin-surface-muted p-4 font-mono text-xs text-admin-text-primary">{job.log}</pre>
        ) : (
          <p className="text-sm text-admin-text-muted">No output reported yet.</p>
        )}
      </AdminSectionCard>
    </div>
  );
}
