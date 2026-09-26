import type { ReactNode } from "react";
import { AdminShell, type AdminNavGroup } from "@yesvus/helmdeck";

const navigation: AdminNavGroup[] = [
  {
    label: "Runner Pool",
    items: [
      { href: "/", label: "Overview", icon: "overview" },
      { href: "/devices", label: "Devices & Nodes", icon: "database" },
      { href: "/jobs", label: "Queue & History", icon: "activity" },
    ],
  },
];

export default function DashboardLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <AdminShell
      nav={navigation}
      homeHref="/"
      brand={{ label: "Cindral" }}
      showTopbar
    >
      <div className="p-6">{children}</div>
    </AdminShell>
  );
}
