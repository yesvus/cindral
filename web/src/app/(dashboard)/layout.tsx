import type { Metadata } from "next";
import type { ReactNode } from "react";
import { AdminShell, type AdminNavGroup } from "@/components/admin";
import { readPool } from "@/lib/broker-store";
import { can, requirePageSession } from "@/lib/permissions";
import { readDevices, readJobs } from "@/lib/resources";
import { signOut } from "@/app/login/actions";
import { ThemeToggle } from "@/app/theme-toggle";

// Each item carries the permission it needs, so the nav a session is shown is
// the nav that session's rule allows. The decision is taken here rather than in
// the shell's client hook because the nav is built in a server layout, and it
// is the same `can` a served action asks, so the two cannot disagree.
const navigation: AdminNavGroup[] = [
  {
    label: "Runner Pool",
    items: [
      { href: "/", label: "Overview", icon: "overview", permission: readDevices },
      {
        href: "/devices",
        label: "Devices & Nodes",
        icon: "database",
        permission: readDevices,
      },
      { href: "/jobs", label: "Queue & History", icon: "activity", permission: readJobs },
    ],
  },
];

// The broker's own build, so an operator reading any page can tell which
// deployment they are looking at. It is the broker's value rather than the
// panel's, because those two are deployed separately and can differ. A broker
// that does not report it shows nothing rather than a placeholder.
async function BuildBadge() {
  let build: { version: string; commit: string } | undefined;
  try {
    // Through the guarded read rather than the raw snapshot fetch: broker-store's
    // contract is that every served answer passes resourceActions, and this was
    // the one that did not, so a session denied devices.read still learned the
    // broker's version and commit.
    const { snapshot } = await readPool();
    build = snapshot.build;
  } catch {
    build = undefined;
  }
  if (!build || build.version === "unknown") {
    return null;
  }
  return (
    <span
      className="whitespace-nowrap font-mono text-xs text-admin-text-muted"
      title={`broker build ${build.commit}`}
    >
      broker {build.version} ({build.commit.slice(0, 7)})
    </span>
  );
}

export const metadata: Metadata = { title: "Runner Pool" };
export const dynamic = "force-dynamic";

export default async function DashboardLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  const session = await requirePageSession();
  const groups = navigation
    .map((group) => ({
      label: group.label,
      items: group.items.filter((item) => can(session, item.permission ?? "")),
    }))
    .filter((group) => group.items.length > 0);

  return (
    <AdminShell
      nav={groups}
      homeHref="/"
      brand={{ label: "Cindral" }}
      session={{ email: session.email, role: session.role }}
      onLogout={signOut}
      // the panel has no profile page and no separate public site, so the
      // defaults would render two links that leave the pool view or 404
      profileHref=""
      viewSiteHref=""
      showTopbar
      topbarExtra={
        <>
          <BuildBadge />
          <ThemeToggle />
        </>
      }
    >
      {children}
    </AdminShell>
  );
}
