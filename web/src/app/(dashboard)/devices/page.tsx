import { AdminBanner, AdminPageHeader, AdminSectionCard } from "@/components/admin";
import { Cpu } from "lucide-react";
import { loadPool, poolFailureTitle } from "@/lib/load-pool";
import { SnapshotControls } from "../snapshot-controls";
import { DeviceDetailTable } from "../tables";

export const dynamic = "force-dynamic";

export default async function DevicesPage() {
  const result = await loadPool();

  if (!result.ok) {
    return (
      <div className="space-y-6">
        <AdminPageHeader title="Devices & Nodes" />
        <AdminBanner
          tone={result.reason === "unauthorized" ? "error" : "warning"}
          title={poolFailureTitle(result.reason)}
          body={result.message}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <AdminPageHeader
        title="Devices & Nodes"
        action={<SnapshotControls fetchedAt={result.fetchedAt} />}
      />
      <AdminSectionCard
        icon={Cpu}
        title="Runner Pool Fleet"
        description="Self-hosted execution nodes and their authoritative slot leases"
      >
        <DeviceDetailTable devices={result.devices} now={result.fetchedAt} />
      </AdminSectionCard>
    </div>
  );
}
