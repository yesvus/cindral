import type { Metadata } from "next";
import type { ReactNode } from "react";
import { AdminI18nProvider } from "@/components/admin";
import "./globals.css";

// the package dictionary defaults to Turkish, so the shell has to be told which
// locale the panel copy is written in. the html lang attribute reads the same
// constant so the two cannot drift apart
const INTERFACE_LOCALE = "en";

export const metadata: Metadata = {
  title: {
    default: "Cindral Control Panel",
    template: "%s | Cindral",
  },
  description: "Cindral runner pool, device status, and job queue control panel.",
};

// the shell themes off this attribute rather than prefers-color-scheme, so it
// has to be set before the first paint or a dark page flashes light
const THEME_BOOTSTRAP = `try{var s=localStorage.getItem("cindral-theme");var t=s==="dark"||s==="light"?s:matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";document.documentElement.dataset.adminTheme=t}catch(e){}`;

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang={INTERFACE_LOCALE} data-admin-theme="light">
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP }} />
      </head>
      <body className="min-h-screen bg-admin-surface-subtle text-admin-text-primary antialiased">
        <AdminI18nProvider locale={INTERFACE_LOCALE}>{children}</AdminI18nProvider>
      </body>
    </html>
  );
}
