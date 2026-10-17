"use server";

import { redirect } from "next/navigation";
import { signInAttempt, signOutAttempt } from "@/lib/auth";

export async function signIn(
  _previous: string | undefined,
  formData: FormData,
): Promise<string | undefined> {
  const result = await signInAttempt({
    email: String(formData.get("email") ?? ""),
    password: String(formData.get("password") ?? ""),
  });
  if (!result.ok) {
    return result.message;
  }
  redirect("/");
}

export async function signOut(): Promise<void> {
  await signOutAttempt();
  redirect("/login");
}
