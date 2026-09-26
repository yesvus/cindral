import { createHmac, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";

const PASSWORD = process.env.CINDRAL_PANEL_PASSWORD || "";
const EMAIL = process.env.CINDRAL_PANEL_EMAIL || "operator@yesvus.com";
const SESSION_SECRET = process.env.CINDRAL_SESSION_SECRET || "";
const SESSION_COOKIE = "cindral_session";
const SESSION_TTL_SECONDS = 60 * 60 * 12;

function configured(): boolean {
  return Boolean(PASSWORD && SESSION_SECRET);
}

export function safeEqual(presented: string, expected: string): boolean {
  const a = Buffer.from(presented);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

function sign(payload: string): string {
  return createHmac("sha256", SESSION_SECRET).update(payload).digest("base64url");
}

/** Mint a session cookie value. Returns null when auth is not configured. */
export function createSession(): string | null {
  if (!configured()) {
    return null;
  }
  const expires = Math.floor(Date.now() / 1000) + SESSION_TTL_SECONDS;
  const payload = Buffer.from(JSON.stringify({ email: EMAIL, expires })).toString(
    "base64url",
  );
  return `${payload}.${sign(payload)}`;
}

export function verifySession(value: string | undefined): boolean {
  if (!configured() || !value) {
    return false;
  }
  const [payload, signature] = value.split(".");
  if (!payload || !signature || !safeEqual(signature, sign(payload))) {
    return false;
  }
  try {
    const decoded = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    return typeof decoded.expires === "number" && decoded.expires > Date.now() / 1000;
  } catch {
    return false;
  }
}

export function checkPassword(candidate: string): boolean {
  // refuse rather than fall through when unconfigured, so a missing secret
  // cannot leave the panel open to an empty password
  return configured() && safeEqual(candidate, PASSWORD);
}

export function checkEmail(candidate: string): boolean {
  return configured() && safeEqual(candidate.trim().toLowerCase(), EMAIL.toLowerCase());
}

export async function currentSessionEmail(): Promise<string | null> {
  const store = await cookies();
  const value = store.get(SESSION_COOKIE)?.value;
  return verifySession(value) ? EMAIL : null;
}

export const sessionCookie = {
  name: SESSION_COOKIE,
  maxAge: SESSION_TTL_SECONDS,
};
