import { AdminBanner, AdminPageHeader, AdminSectionCard } from "@yesvus/helmdeck";
import { Cpu } from "lucide-react";
import { loadPool } from "@/lib/load-pool";
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
          title={
            result.reason === "unauthorized"
              ? "Sign in required"
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
        <DeviceDetailTable devices={result.snapshot.devices} />
      </AdminSectionCard>
    </div>
  );
}
