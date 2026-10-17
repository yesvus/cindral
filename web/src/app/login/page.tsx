import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { currentAdminSession } from "@/lib/auth";
import { LoginForm } from "./login-form";

export const metadata: Metadata = { title: "Sign in" };
export const dynamic = "force-dynamic";

export default async function LoginPage() {
  if (await currentAdminSession()) {
    redirect("/");
  }
  // the operator address stays out of the prefill: this page is public, and a
  // rendered address hands an unauthenticated visitor a target to phish.
  return <LoginForm />;
}
