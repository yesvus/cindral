import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { currentSessionEmail, panelEmail } from "@/lib/auth";
import { LoginForm } from "./login-form";

export const metadata: Metadata = { title: "Sign in" };
export const dynamic = "force-dynamic";

export default async function LoginPage() {
  if (await currentSessionEmail()) {
    redirect("/");
  }
  return <LoginForm defaultEmail={panelEmail} />;
}
