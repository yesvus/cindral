"use client";

import { useState, type ButtonHTMLAttributes, type FormEvent, type ReactNode } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity, Cpu, LayoutDashboard, LogOut, Server, type LucideIcon } from "lucide-react";
import type { AdminLoginCredentials, AdminSession } from "@/lib/admin-core";

type AdminNavItem = {
  href: string;
  label: string;
  icon?: string;
  permission?: string;
};

export type AdminNavGroup = { label: string; items: AdminNavItem[] };

const iconByName = {
  overview: LayoutDashboard,
  database: Server,
  activity: Activity,
} as const;

export function AdminI18nProvider({ children }: { locale: string; children: ReactNode }) {
  return children;
}

export function AdminShell({
  nav,
  session,
  homeHref = "/",
  brand = { label: "Admin" },
  onLogout,
  showTopbar = true,
  topbarExtra,
  children,
}: {
  nav: AdminNavGroup[];
  session?: AdminSession | null;
  homeHref?: string;
  brand?: { label: string };
  onLogout?: () => void | Promise<void>;
  showTopbar?: boolean;
  topbarExtra?: ReactNode;
  profileHref?: string;
  viewSiteHref?: string;
  children: ReactNode;
}) {
  const pathname = usePathname();
  return (
    <div className="min-h-screen bg-admin-surface-subtle md:grid md:grid-cols-[15rem_1fr]">
      <aside className="border-b border-admin-border bg-admin-surface p-4 md:min-h-screen md:border-b-0 md:border-r">
        <Link className="block rounded-lg px-3 py-4 text-lg font-semibold text-admin-text-primary" href={homeHref}>
          {brand.label}
        </Link>
        <nav aria-label="Main navigation" className="space-y-5">
          {nav.map((group) => (
            <section key={group.label}>
              <h2 className="px-3 pb-2 text-xs font-semibold uppercase tracking-wide text-admin-text-muted">{group.label}</h2>
              <ul className="space-y-1">
                {group.items.map((item) => {
                  const Icon = item.icon && item.icon in iconByName
                    ? iconByName[item.icon as keyof typeof iconByName]
                    : Activity;
                  const active = pathname === item.href || (item.href !== "/" && pathname.startsWith(`${item.href}/`));
                  return (
                    <li key={item.href}>
                      <Link
                        aria-current={active ? "page" : undefined}
                        className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm ${active ? "bg-admin-surface-muted font-medium text-admin-text-primary" : "text-admin-text-secondary hover:bg-admin-surface-muted"}`}
                        href={item.href}
                      >
                        <Icon aria-hidden="true" className="size-4" />
                        {item.label}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </section>
          ))}
        </nav>
      </aside>
      <div className="min-w-0">
        {showTopbar && (
          <header className="flex min-h-14 items-center justify-between border-b border-admin-border bg-admin-surface px-5">
            <span className="truncate text-sm text-admin-text-secondary">{session?.email}</span>
            <div className="flex items-center gap-3">{topbarExtra}</div>
            {onLogout && (
              <form action={onLogout}>
                <button className="inline-flex items-center gap-2 rounded-md px-3 py-2 text-sm text-admin-text-secondary hover:bg-admin-surface-muted" type="submit">
                  <LogOut aria-hidden="true" className="size-4" />
                  Sign out
                </button>
              </form>
            )}
          </header>
        )}
        <main className="mx-auto w-full max-w-screen-2xl space-y-6 p-4 sm:p-6">{children}</main>
      </div>
    </div>
  );
}

export function AdminLoginScreen({
  brandLabel = "Admin",
  homeHref = "/",
  errorMessage,
  message,
  busy = false,
  onSubmit,
}: {
  brandLabel?: string;
  homeHref?: string;
  errorMessage?: string;
  message?: string;
  busy?: boolean;
  onSubmit?: (credentials: AdminLoginCredentials) => void | Promise<void>;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void onSubmit?.({ email, password });
  };
  return (
    <main className="flex min-h-screen items-center justify-center bg-admin-surface-subtle p-4">
      <section className="w-full max-w-md rounded-xl border border-admin-border bg-admin-surface p-8 shadow-sm">
        <Link href={homeHref} className="text-xl font-semibold text-admin-text-primary">{brandLabel}</Link>
        <h1 className="mt-6 text-2xl font-semibold text-admin-text-primary">Sign in</h1>
        {message && <p className="mt-2 text-sm text-admin-text-secondary">{message}</p>}
        {errorMessage && <p role="alert" className="mt-4 rounded-lg border border-admin-warning-border bg-admin-warning-surface p-3 text-sm text-admin-warning-text">{errorMessage}</p>}
        <form className="mt-6 space-y-4" onSubmit={submit}>
          <label className="block space-y-1.5 text-sm font-medium text-admin-text-secondary">
            Email
            <input autoComplete="username" className="w-full rounded-md border border-admin-border bg-admin-surface px-3 py-2 text-admin-text-primary" name="email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} required />
          </label>
          <label className="block space-y-1.5 text-sm font-medium text-admin-text-secondary">
            Password
            <input autoComplete="current-password" className="w-full rounded-md border border-admin-border bg-admin-surface px-3 py-2 text-admin-text-primary" name="password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} required />
          </label>
          <Button className="w-full" type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</Button>
        </form>
      </section>
    </main>
  );
}

export function AdminPageHeader({ title, action }: { title: string; action?: ReactNode }) {
  return <header className="flex min-h-10 items-center justify-between gap-4"><h1 className="text-2xl font-semibold tracking-tight text-admin-text-primary">{title}</h1>{action}</header>;
}

export function AdminBanner({ tone, title, body }: { tone: "error" | "warning" | "info" | "success"; title: string; body: string }) {
  const colors = tone === "error" ? "border-red-300 bg-red-50 text-red-900" : tone === "warning" ? "border-amber-300 bg-amber-50 text-amber-900" : tone === "success" ? "border-emerald-300 bg-emerald-50 text-emerald-900" : "border-blue-300 bg-blue-50 text-blue-900";
  return <section role="status" className={`rounded-lg border p-4 ${colors}`}><h2 className="font-semibold">{title}</h2><p className="mt-1 text-sm">{body}</p></section>;
}

export function AdminSectionCard({ icon: Icon, title, description, action, children, className = "" }: {
  icon?: typeof Cpu;
  title: string;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`min-w-0 rounded-xl border border-admin-border bg-admin-surface shadow-sm ${className}`}>
      <header className="flex items-start justify-between gap-4 border-b border-admin-border px-5 py-4">
        <div className="flex min-w-0 items-start gap-3">{Icon && <Icon aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-admin-text-muted" />}<div><h2 className="font-semibold text-admin-text-primary">{title}</h2>{description && <p className="mt-1 text-sm text-admin-text-muted">{description}</p>}</div></div>
        {action}
      </header>
      <div className="p-5">{children}</div>
    </section>
  );
}

export function AdminStatusPill({ tone = "neutral", label, className = "" }: { tone?: string; label: string; className?: string }) {
  const colors: Record<string, string> = {
    neutral: "border-zinc-300 bg-zinc-100 text-zinc-700",
    info: "border-blue-300 bg-blue-50 text-blue-800",
    success: "border-emerald-300 bg-emerald-50 text-emerald-800",
    warning: "border-amber-300 bg-amber-50 text-amber-900",
    danger: "border-red-300 bg-red-50 text-red-800",
    error: "border-red-300 bg-red-50 text-red-800",
  };
  return <span className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium ${colors[tone] ?? colors.neutral} ${className}`}>{label}</span>;
}

export type AdminStatusTone = "neutral" | "info" | "success" | "warning" | "danger" | "error";

export type AdminTableColumn<T> = {
  key: string;
  header: ReactNode;
  width?: string;
  align?: "left" | "right" | "center";
  className?: string;
  headerClassName?: string;
  cell: (row: T) => ReactNode;
};

export function AdminTable<T>({
  caption,
  columns,
  rows,
  getKey,
  empty,
}: {
  caption: string;
  columns: AdminTableColumn<T>[];
  rows: T[];
  getKey: (row: T) => string;
  empty?: ReactNode;
}) {
  return (
    <div className="w-full overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-admin-border text-xs font-medium uppercase tracking-wide text-admin-text-muted">
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={`px-3 py-2 ${column.align === "right" ? "text-right" : column.align === "center" ? "text-center" : "text-left"} ${column.headerClassName ?? ""}`}
                style={column.width ? { width: column.width } : undefined}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-admin-border">
          {rows.map((row) => (
            <tr key={getKey(row)} className="align-top">
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={`px-3 py-3 ${column.align === "right" ? "text-right" : column.align === "center" ? "text-center" : "text-left"} ${column.className ?? ""}`}
                >
                  {column.cell(row)}
                </td>
              ))}
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={Math.max(columns.length, 1)} className="px-3 py-8 text-center">
                {empty}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export function AdminStatCard({
  icon: Icon,
  label,
  value,
  detail,
  tone = "neutral",
}: {
  icon: LucideIcon;
  label: string;
  value: string;
  detail?: string;
  tone?: AdminStatusTone;
}) {
  const toneClass = tone === "warning" ? "text-admin-warning-text" : tone === "danger" || tone === "error" ? "text-admin-danger-text" : tone === "success" ? "text-emerald-700" : tone === "info" ? "text-blue-700" : "text-admin-text-primary";
  return (
    <section className="rounded-xl border border-admin-border bg-admin-surface p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm text-admin-text-muted">{label}</p>
          <p className={`mt-2 text-2xl font-semibold tabular-nums ${toneClass}`}>{value}</p>
        </div>
        <Icon aria-hidden="true" className="size-5 shrink-0 text-admin-text-muted" />
      </div>
      {detail && <p className="mt-3 text-xs text-admin-text-secondary">{detail}</p>}
    </section>
  );
}

export function Button({ className = "", variant = "default", size = "default", type = "button", ...props }: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "default" | "destructive" | "success" | "outline" | "secondary" | "ghost" | "link" | "white";
  size?: "default" | "sm" | "lg" | "icon";
}) {
  const variants: Record<string, string> = {
    default: "bg-zinc-900 text-white hover:bg-zinc-800",
    destructive: "bg-red-600 text-white hover:bg-red-700",
    success: "bg-emerald-600 text-white hover:bg-emerald-700",
    outline: "border border-admin-border bg-admin-surface text-admin-text-primary hover:bg-admin-surface-muted",
    secondary: "bg-admin-surface-muted text-admin-text-primary hover:opacity-80",
    ghost: "text-admin-text-secondary hover:bg-admin-surface-muted",
    link: "text-admin-text-primary underline underline-offset-4",
    white: "bg-white text-zinc-900 hover:bg-zinc-100",
  };
  const sizes: Record<string, string> = {
    default: "h-10 px-4 py-2",
    sm: "h-8 rounded-md px-3 text-xs",
    lg: "h-11 rounded-md px-8",
    icon: "size-9",
  };
  return <button type={type} className={`inline-flex items-center justify-center gap-2 rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:pointer-events-none disabled:opacity-50 ${variants[variant]} ${sizes[size]} ${className}`} {...props} />;
}
