import {
  AdminPageHeader,
  AdminStatusPill,
  AdminTable,
  type AdminTableColumn,
  AdminSectionCard,
} from "@yesvus/helmdeck";
import { Cpu } from "lucide-react";
import { getPoolSnapshot, type DeviceInfo } from "@/lib/cindral";

export const dynamic = "force-dynamic";

export default async function DevicesPage() {
  const snapshot = await getPoolSnapshot();
  const devices = snapshot?.devices ?? [];

  const columns: AdminTableColumn<DeviceInfo>[] = [
    {
      key: "name",
      header: "Device Name",
      cell: (d) => (
        <div>
          <div className="font-semibold text-zinc-900 dark:text-zinc-100">{d.name}</div>
          <div className="text-xs text-zinc-500">Node Runner</div>
        </div>
      ),
    },
    {
      key: "status",
      header: "Status",
      cell: (d) => {
        const tone = d.status === "online" && d.healthy ? "success" : "error";
        return (
          <div className="flex items-center gap-2">
            <AdminStatusPill tone={tone} label={d.status} />
            {!d.healthy && <AdminStatusPill tone="warning" label="Degraded" />}
          </div>
        );
      },
    },
    {
      key: "labels",
      header: "Runner Labels",
      cell: (d) => (
        <div className="flex flex-wrap gap-1">
          {d.labels.map((lbl) => (
            <span
              key={lbl}
              className="inline-block rounded bg-zinc-100 dark:bg-zinc-800 px-2 py-0.5 text-xs text-zinc-700 dark:text-zinc-300 font-mono"
            >
              {lbl}
            </span>
          ))}
          {d.labels.length === 0 && <span className="text-xs text-zinc-400">None</span>}
        </div>
      ),
    },
    {
      key: "allocation",
      header: "Current Allocation",
      cell: (d) => {
        if (!d.current_lease) {
          return <AdminStatusPill tone="neutral" label="Idle" />;
        }
        const lease = d.current_lease;
        return (
          <div className="space-y-1">
            <div className="flex items-center gap-1.5">
              <AdminStatusPill tone="warning" label="Leased" />
              <span className="text-xs font-mono text-zinc-600 dark:text-zinc-300">{lease.repository}</span>
            </div>
            <div className="text-xs text-zinc-500 font-mono">
              Job: {lease.job_id.slice(0, 8)} ({Math.round(lease.remaining_seconds)}s remaining)
            </div>
          </div>
        );
      },
    },
  ];

  return (
    <div className="space-y-6">
      <AdminPageHeader title="Devices & Nodes" />

      <AdminSectionCard
        icon={Cpu}
        title="Runner Pool Fleet"
        description="Self-hosted execution nodes and their authoritative slot leases"
      >
        <AdminTable
          columns={columns}
          rows={devices}
          getKey={(d) => d.name}
          empty={<div className="p-8 text-center text-sm text-zinc-500">No runner devices registered</div>}
        />
      </AdminSectionCard>
    </div>
  );
}
