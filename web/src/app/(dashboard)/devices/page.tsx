import {
  AdminBanner,
  AdminPageHeader,
  AdminStatusPill,
  AdminTable,
  type AdminTableColumn,
  AdminSectionCard,
} from "@yesvus/helmdeck";
import { Cpu } from "lucide-react";
import type { DeviceInfo } from "@/lib/cindral";
import { loadPool } from "@/lib/load-pool";

export const dynamic = "force-dynamic";

const columns: AdminTableColumn<DeviceInfo>[] = [
  {
    key: "name",
    header: "Device Name",
    cell: (d) => (
      <div>
        <div className="font-semibold text-zinc-900 dark:text-zinc-100">
          {d.name}
        </div>
        <div className="text-xs text-zinc-500">Node Runner</div>
      </div>
    ),
  },
  {
    key: "status",
    header: "Status",
    cell: (d) => (
      <div className="flex items-center gap-2">
        <AdminStatusPill
          tone={d.status === "online" && d.healthy ? "success" : "error"}
          label={d.status}
        />
        {!d.healthy && <AdminStatusPill tone="warning" label="Degraded" />}
      </div>
    ),
  },
  {
    key: "labels",
    header: "Runner Labels",
    cell: (d) => (
      <div className="flex flex-wrap gap-1">
        {d.labels.map((label) => (
          <span
            key={label}
            className="inline-block rounded bg-zinc-100 px-2 py-0.5 font-mono text-xs text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
          >
            {label}
          </span>
        ))}
        {d.labels.length === 0 && (
          <span className="text-xs text-zinc-400">None</span>
        )}
      </div>
    ),
  },
  {
    key: "allocation",
    header: "Current Allocation",
    cell: (d) => {
      if (!d.current_lease) {
        // a device can be occupied by a GitHub runner without holding a
        // Cindral lease, so busy state is not the same as having a lease
        return (
          <AdminStatusPill
            tone={d.busy ? "warning" : "neutral"}
            label={d.busy ? "Busy (no Cindral lease)" : "Idle"}
          />
        );
      }
      const lease = d.current_lease;
      return (
        <div className="space-y-1">
          <div className="flex items-center gap-1.5">
            <AdminStatusPill tone="warning" label="Leased" />
            <span className="font-mono text-xs text-zinc-600 dark:text-zinc-300">
              {lease.repository}
            </span>
          </div>
          <div className="font-mono text-xs text-zinc-500">
            Job: {lease.job_id.slice(0, 8)} (
            {Math.round(lease.remaining_seconds)}s remaining)
          </div>
          {d.leases.length > 1 && (
            <div className="text-xs font-medium text-amber-600 dark:text-amber-400">
              {d.leases.length} concurrent leases on this device
            </div>
          )}
        </div>
      );
    },
  },
];

export default async function DevicesPage() {
  const result = await loadPool();

  if (!result.ok) {
    return (
      <div className="space-y-6">
        <AdminPageHeader title="Devices & Nodes" />
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
      <AdminPageHeader title="Devices & Nodes" />

      <AdminSectionCard
        icon={Cpu}
        title="Runner Pool Fleet"
        description="Self-hosted execution nodes and their authoritative slot leases"
      >
        <AdminTable
          columns={columns}
          rows={result.snapshot.devices}
          getKey={(d) => d.name}
          empty={
            <div className="p-8 text-center text-sm text-zinc-500">
              No runner devices registered
            </div>
          }
        />
      </AdminSectionCard>
    </div>
  );
}
