"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { headers } from "next/headers";
import {
  authConfigured,
  checkEmail,
  checkPassword,
  createSession,
  sessionCookie,
} from "@/lib/auth";
import {
  clearFailures,
  clientKey,
  recordFailure,
  tooManyAttempts,
} from "@/lib/throttle";

export async function signIn(
  _previous: string | undefined,
  formData: FormData,
): Promise<string | undefined> {
  // report misconfiguration before credentials, so an unset secret does not
  // read as a wrong password
  if (!authConfigured()) {
    return "Panel authentication is not configured.";
  }
  const key = clientKey(await headers());
  if (tooManyAttempts(key)) {
    return "Too many failed attempts. Try again later.";
  }
  const email = String(formData.get("email") ?? "");
  const password = String(formData.get("password") ?? "");
  // both checks always run, so a bad address and a bad password cost the same
  const emailOk = checkEmail(email);
  const passwordOk = checkPassword(password);
  if (!emailOk || !passwordOk) {
    recordFailure(key);
    return "Invalid credentials.";
  }
  clearFailures(key);
  const value = createSession();
  if (!value) {
    return "Panel authentication is not configured.";
  }
  const store = await cookies();
  store.set(sessionCookie.name, value, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
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
