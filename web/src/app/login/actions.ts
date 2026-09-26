"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { checkEmail, checkPassword, createSession, sessionCookie } from "@/lib/auth";

export async function signIn(
  _previous: string | undefined,
  formData: FormData,
): Promise<string | undefined> {
  if (!checkEmail(String(formData.get("email") ?? ""))) {
    return "Unknown operator.";
  }
  if (!checkPassword(String(formData.get("password") ?? ""))) {
    return "Invalid password.";
  }
  const value = createSession();
  if (!value) {
    return "Panel authentication is not configured.";
  }
  const store = await cookies();
  store.set(sessionCookie.name, value, {
    httpOnly: true,
    sameSite: "lax",
    secure: true,
    path: "/",
    maxAge: sessionCookie.maxAge,
  });
  redirect("/");
}

export async function signOut(): Promise<void> {
  const store = await cookies();
  store.delete(sessionCookie.name);
  redirect("/login");
}
