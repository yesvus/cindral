import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The cookie store, stubbed.
 *
 * The server reads the session cookie through `next/headers`, which only has a store inside a
 * request. A stub is what lets a sign-in be exercised end to end here, and it is also the only way
 * to make a session lapse without waiting twelve hours.
 */
const jar = new Map<string, string>();

vi.mock("next/headers.js", () => ({
  cookies: async () => ({
    get: (name: string) =>
      jar.has(name) ? { name, value: jar.get(name) } : undefined,
    set: (name: string, value: string, options: { maxAge?: number }) => {
      jar.set(name, value);
      void options;
    },
    delete: (input: string | { name: string }) => {
      const name = typeof input === "string" ? input : input.name;
      jar.delete(name);
    },
  }),
  headers: async () => ({ get: () => null }),
}));

const SECRET = "a-session-secret-long-enough-to-sign";
const { authConfigured, currentAdminSession, signInAttempt, signOutAttempt } =
  await import("@/lib/auth");

async function signIn(email: string, password: string) {
  return signInAttempt({ email, password });
}

beforeEach(() => {
  jar.clear();
  vi.unstubAllEnvs();
  vi.stubEnv("CINDRAL_PANEL_EMAIL", "operator@example.com");
  vi.stubEnv("CINDRAL_SESSION_SECRET", SECRET);
  vi.stubEnv("CINDRAL_PANEL_PASSWORD", "correct horse battery staple");
  vi.stubEnv("NODE_ENV", "production");
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("authConfigured", () => {
  it("needs an address, a password and a secret long enough to sign with", () => {
    // the signer refuses a short secret by throwing, so a weak one has to read as
    // unconfigured rather than sign cookies a guess gets through
    expect(authConfigured()).toBe(true);

    vi.stubEnv("CINDRAL_SESSION_SECRET", "fifteen-chars");
    expect(SECRET.length).toBeGreaterThanOrEqual(16);
    expect(authConfigured()).toBe(false);

    vi.stubEnv("CINDRAL_SESSION_SECRET", SECRET);
    vi.stubEnv("CINDRAL_PANEL_EMAIL", "");
    expect(authConfigured()).toBe(false);

    vi.stubEnv("CINDRAL_PANEL_EMAIL", "operator@example.com");
    vi.stubEnv("CINDRAL_PANEL_PASSWORD", "");
    expect(authConfigured()).toBe(false);
  });

  it("refuses every login rather than serving the panel open when unconfigured", async () => {
    // the failure this guards is an unset secret reading as an empty password
    vi.stubEnv("CINDRAL_PANEL_PASSWORD", "");
    const result = await signIn("operator@example.com", "");
    expect(result.ok).toBe(false);
    expect(result.ok === false && result.message).toBe(
      "Panel authentication is not configured.",
    );
    expect(await currentAdminSession()).toBeNull();
  });
});

describe("signing in", () => {
  it("accepts the configured address and password, and reads back a session", async () => {
    const result = await signIn("operator@example.com", "correct horse battery staple");
    expect(result.ok).toBe(true);
    expect(await currentAdminSession()).toEqual({
      email: "operator@example.com",
      role: "operator",
    });
  });

  it("reads a session back from a cookie that carries a signature", async () => {
    // Pins the cookie contract readSession depends on. The cookie is `id.signature`,
    // and readSession does Number(sessionId), so the cookie parser has to hand it the part
    // before the signature: Number("id.signature") is NaN and every session would read
    // as signed out. Assert the cookie boundary directly so a format change remains visible.
    const result = await signIn("operator@example.com", "correct horse battery staple");
    expect(result.ok).toBe(true);
    const cookie = jar.get("cindral_session")!;
    expect(cookie).toContain(".");
    expect(cookie.split(".")[0]).toMatch(/^\d+$/);
    expect(await currentAdminSession()).toEqual({
      email: "operator@example.com",
      role: "operator",
    });
  });

  it("folds the address the way a person types it", async () => {
    // case is not significant in an address and surrounding space is a paste
    expect((await signIn("  Operator@Example.COM ", "correct horse battery staple")).ok).toBe(
      true,
    );
  });

  it("refuses a wrong password and a wrong address alike", async () => {
    for (const credentials of [
      { email: "operator@example.com", password: "wrong" },
      { email: "someone.else@example.com", password: "correct horse battery staple" },
      { email: "", password: "" },
      // a prefix of the real password, which the old length-checked comparison refused
      // early enough to be distinguishable by how long it took
      { email: "operator@example.com", password: "correct horse" },
      { email: "operator@example.com", password: "correct horse battery stapl" },
    ]) {
      const result = await signIn(credentials.email, credentials.password);
      expect(result.ok, JSON.stringify(credentials)).toBe(false);
      expect(await currentAdminSession()).toBeNull();
    }
  });

  it("sets one cookie, and it is the panel's own name", async () => {
    await signIn("operator@example.com", "correct horse battery staple");
    expect([...jar.keys()]).toEqual(["cindral_session"]);
  });

  it("signs in against a scrypt hash in the environment", async () => {
    // the deployment that stops holding the plaintext: the hash is what the comparison runs
    // against, and the plaintext variable is then not read at all. The module is reloaded
    // because the plaintext is hashed once per process, which is the point of caching it and
    // also why a changed environment needs a restart.
    const { hashPassword } = await import("@/lib/auth");
    vi.stubEnv("CINDRAL_PANEL_PASSWORD_HASH", await hashPassword("a-hashed-password"));
    vi.stubEnv("CINDRAL_PANEL_PASSWORD", "no-longer-the-password");
    vi.resetModules();
    const fresh = await import("@/lib/auth");

    expect(fresh.authConfigured()).toBe(true);
    const accepted = await fresh.signInAttempt({
      email: "operator@example.com",
      password: "a-hashed-password",
    });
    expect(accepted.ok).toBe(true);
    const refused = await fresh.signInAttempt({
      email: "operator@example.com",
      password: "no-longer-the-password",
    });
    expect(refused.ok).toBe(false);
  });
});

describe("the session", () => {
  it("is gone after signing out", async () => {
    await signIn("operator@example.com", "correct horse battery staple");
    await signOutAttempt();
    expect(await currentAdminSession()).toBeNull();
    expect(jar.size).toBe(0);
  });

  it("is refused for a cookie the panel did not sign", async () => {
    await signIn("operator@example.com", "correct horse battery staple");
    const forged = `${jar.get("cindral_session")!.split(".")[0]}.not-a-signature`;
    jar.set("cindral_session", forged);
    expect(await currentAdminSession()).toBeNull();
  });

  it("is refused for a cookie signed with a different secret", async () => {
    // a second instance configured differently must not accept this one's sessions
    await signIn("operator@example.com", "correct horse battery staple");
    const before = jar.get("cindral_session")!;
    jar.set("cindral_session", before);
    vi.resetModules();
    vi.stubEnv("CINDRAL_SESSION_SECRET", "a-different-secret-of-sufficient-length");
    const other = await import("@/lib/auth");
    expect(await other.currentAdminSession()).toBeNull();
  });

  it("lapses on the server, not only in the browser holding it", async () => {
    // the cookie's own max-age is the browser's promise. A cookie kept past it is refused
    // here, so the expiry is a decision of the panel rather than of whoever kept the value.
    await signIn("operator@example.com", "correct horse battery staple");
    expect(await currentAdminSession()).not.toBeNull();

    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date(Date.now() + 13 * 60 * 60 * 1000));
      expect(await currentAdminSession()).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
