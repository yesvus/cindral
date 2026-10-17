const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  if (total < MINUTE) {
    return `${total}s`;
  }
  if (total < HOUR) {
    const rest = total % MINUTE;
    const minutes = Math.floor(total / MINUTE);
    return rest === 0 ? `${minutes}m` : `${minutes}m ${rest}s`;
  }
  if (total < DAY) {
    const rest = Math.floor((total % HOUR) / MINUTE);
    const hours = Math.floor(total / HOUR);
    return rest === 0 ? `${hours}h` : `${hours}h ${rest}m`;
  }
  const rest = Math.floor((total % DAY) / HOUR);
  const days = Math.floor(total / DAY);
  return rest === 0 ? `${days}d` : `${days}d ${rest}h`;
}

export function formatAge(seconds: number): string {
  return `${formatDuration(seconds)} ago`;
}

export function formatCountdown(seconds: number): string {
  return `in ${formatDuration(seconds)}`;
}

export function formatRelative(at: number, now: number): string {
  const delta = now - at;
  if (Math.abs(delta) < 5) {
    return "just now";
  }
  return delta > 0 ? formatAge(delta) : formatCountdown(-delta);
}

// toLocaleString renders differently on the server and in the browser, which
// would break hydration, so both agree on one fixed UTC format
export function absoluteTime(at: number): { dateTime: string; label: string } {
  const iso = new Date(at * 1000).toISOString();
  return {
    dateTime: iso,
    label: `${iso.slice(0, 10)} ${iso.slice(11, 19)} UTC`,
  };
}
