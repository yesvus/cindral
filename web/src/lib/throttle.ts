/** Bounded failed-login attempts, so the single operator password cannot be
 * brute-forced from the network.
 *
 * State is per-process and deliberately lost on restart: this throttles a
 * casual online guess, it is not a distributed rate limiter.
 */
const WINDOW_MS = 15 * 60 * 1000;
const MAX_ATTEMPTS = 8;

const failures = new Map<string, { count: number; firstAt: number }>();

export function clientKey(requestHeaders: Headers): string {
  const forwarded = requestHeaders.get("x-forwarded-for");
  if (forwarded) {
    return forwarded.split(",")[0].trim();
  }
  return requestHeaders.get("x-real-ip")?.trim() || "unknown";
}

export function tooManyAttempts(key: string, now: number = Date.now()): boolean {
  const entry = failures.get(key);
  if (!entry) {
    return false;
  }
  if (now - entry.firstAt > WINDOW_MS) {
    failures.delete(key);
    return false;
  }
  return entry.count >= MAX_ATTEMPTS;
}

export function recordFailure(key: string, now: number = Date.now()): void {
  const entry = failures.get(key);
  if (!entry || now - entry.firstAt > WINDOW_MS) {
    failures.set(key, { count: 1, firstAt: now });
    return;
  }
  entry.count += 1;
}

export function clearFailures(key: string): void {
  failures.delete(key);
}
