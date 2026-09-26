import type { Metadata } from "next";
import type { ReactNode } from "react";
import { redirect } from "next/navigation";
import { AdminShell, type AdminNavGroup } from "@yesvus/helmdeck";
import { currentSessionEmail } from "@/lib/auth";
import { signOut } from "@/app/login/actions";

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

export const metadata: Metadata = { title: "Runner Pool" };
export const dynamic = "force-dynamic";

export default async function DashboardLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  const email = await currentSessionEmail();
  if (!email) {
    redirect("/login");
  }
  return (
    <AdminShell
      nav={navigation}
      homeHref="/"
      brand={{ label: "Cindral" }}
      session={{ email, role: "operator" }}
      onLogout={signOut}
      showTopbar
    >
      <div className="p-6">{children}</div>
    </AdminShell>
  );
}
