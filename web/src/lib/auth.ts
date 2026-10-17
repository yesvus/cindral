import { createHmac, randomBytes, scrypt as scryptCallback, timingSafeEqual } from "node:crypto";
import { cookies, headers } from "next/headers.js";
import type { AdminLoginCredentials, AdminLoginResult, AdminSession } from "@/lib/admin-core";

const SESSION_COOKIE = "cindral_session";
const SESSION_TTL_SECONDS = 60 * 60 * 12;
const NOT_CONFIGURED = "Panel authentication is not configured.";
const INVALID_CREDENTIALS = "Invalid credentials.";
const THROTTLED = "Too many failed sign-in attempts. Try again later.";
const THROTTLE_WINDOW_MS = 15 * 60 * 1000;
const THROTTLE_LIMIT = 12;
const attempts = new Map<string, { count: number; expiresAt: number }>();

let storedHash: Promise<string> | null = null;

function scrypt(password: string, salt: Buffer, length: number): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    scryptCallback(password, salt, length, (error, result) => {
      if (error) reject(error);
      else resolve(result);
    });
  });
}

export async function hashPassword(password: string): Promise<string> {
  const salt = randomBytes(16);
  const key = await scrypt(password, salt, 64);
  return `scrypt$${salt.toString("base64")}$${key.toString("base64")}`;
}

export async function verifyPassword(password: string, stored: string): Promise<boolean> {
  const [scheme, saltPart, keyPart] = stored.split("$");
  if (scheme !== "scrypt" || !saltPart || !keyPart) return false;
  const salt = Buffer.from(saltPart, "base64");
  const expected = Buffer.from(keyPart, "base64");
  if (!salt.length || !expected.length) return false;
  const actual = await scrypt(password, salt, expected.length);
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

function normalizeEmail(email: string): string {
  return email.trim().toLowerCase();
}

function passwordHash(): Promise<string> {
  storedHash ??= (async () => {
    const declared = process.env.CINDRAL_PANEL_PASSWORD_HASH?.trim();
    if (declared) return declared;
    const plain = process.env.CINDRAL_PANEL_PASSWORD || "";
    return plain ? hashPassword(plain) : "";
  })();
  return storedHash;
}

function operatorEmail(): string {
  return process.env.CINDRAL_PANEL_EMAIL || "";
}

function sessionSecret(): string {
  return process.env.CINDRAL_SESSION_SECRET || "";
}

export function authConfigured(): boolean {
  return Boolean(
    operatorEmail() &&
      sessionSecret().length >= 16 &&
      (process.env.CINDRAL_PANEL_PASSWORD_HASH?.trim() || process.env.CINDRAL_PANEL_PASSWORD),
  );
}

function signature(sessionId: string, secret = sessionSecret()): string {
  return createHmac("sha256", secret).update(sessionId).digest("base64url");
}

function readSession(sessionId: string): AdminSession | null {
  const expiresAt = Number(sessionId);
  if (!Number.isFinite(expiresAt) || expiresAt * 1000 <= Date.now()) return null;
  const email = normalizeEmail(operatorEmail());
  return email ? { email, role: "operator" } : null;
}

async function sessionFromCookie(): Promise<AdminSession | null> {
  if (!authConfigured()) return null;
  const store = await cookies();
  const value = store.get(SESSION_COOKIE)?.value;
  if (!value) return null;
  const [sessionId, supplied, extra] = value.split(".");
  if (!sessionId || !supplied || extra !== undefined) return null;
  const expected = signature(sessionId);
  const receivedBytes = Buffer.from(supplied);
  const expectedBytes = Buffer.from(expected);
  if (receivedBytes.length !== expectedBytes.length || !timingSafeEqual(receivedBytes, expectedBytes)) {
    return null;
  }
  return readSession(sessionId);
}

async function throttleKey(): Promise<string> {
  const requestHeaders = await headers();
  return requestHeaders.get("x-forwarded-for")?.split(",")[0]?.trim() || "unknown";
}

function throttleState(key: string): { count: number; expiresAt: number } {
  const current = attempts.get(key);
  if (current && current.expiresAt > Date.now()) return current;
  const fresh = { count: 0, expiresAt: Date.now() + THROTTLE_WINDOW_MS };
  attempts.set(key, fresh);
  return fresh;
}

export async function currentAdminSession(): Promise<AdminSession | null> {
  try {
    return await sessionFromCookie();
  } catch {
    return null;
  }
}

export async function signInAttempt(credentials: AdminLoginCredentials): Promise<AdminLoginResult> {
  if (!authConfigured()) return { ok: false, message: NOT_CONFIGURED };
  const key = await throttleKey();
  const state = throttleState(key);
  if (state.count >= THROTTLE_LIMIT) return { ok: false, message: THROTTLED };

  const emailOk = normalizeEmail(credentials.email) === normalizeEmail(operatorEmail());
  const passwordOk = await verifyPassword(credentials.password, await passwordHash());
  if (!emailOk || !passwordOk) {
    state.count += 1;
    return { ok: false, message: INVALID_CREDENTIALS };
  }

  attempts.delete(key);
  const sessionId = String(Math.floor(Date.now() / 1000) + SESSION_TTL_SECONDS);
  const store = await cookies();
  await store.set(
    SESSION_COOKIE,
    `${sessionId}.${signature(sessionId)}`,
    {
      maxAge: SESSION_TTL_SECONDS,
      path: "/",
      sameSite: "lax",
      secure: process.env.NODE_ENV === "production",
      httpOnly: true,
    },
  );
  return { ok: true, session: { email: normalizeEmail(operatorEmail()), role: "operator" } };
}

export async function signOutAttempt(): Promise<void> {
  const store = await cookies();
  store.delete(SESSION_COOKIE);
}
