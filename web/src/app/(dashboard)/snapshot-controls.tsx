"use client";

import { useEffect, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { AdminStatusPill, Button } from "@/components/admin";
import { RefreshCw } from "lucide-react";
import { formatRelative } from "@/lib/time";

// past this age the view no longer describes the pool, so it is marked rather
// than left for the operator to notice a count that stopped moving
const STALE_AFTER_SECONDS = 120;
const INTERVALS = [0, 15, 30, 60, 300];
const INTERVAL_STORAGE_KEY = "cindral-refresh-interval";

export function SnapshotControls({ fetchedAt }: { fetchedAt: number }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [now, setNow] = useState(fetchedAt);
  const [intervalSeconds, setIntervalSeconds] = useState(0);

  // the first render has to agree with the server render, so both start from
  // the snapshot instant and the effect moves the clock to the browser's
  useEffect(() => {
    setNow(Date.now() / 1000);
    const stored = Number(window.localStorage.getItem(INTERVAL_STORAGE_KEY) ?? 0);
    setIntervalSeconds(INTERVALS.includes(stored) ? stored : 0);
  }, []);

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    if (intervalSeconds === 0) {
      return;
    }
    const id = window.setInterval(
      () => startTransition(() => router.refresh()),
      intervalSeconds * 1000,
    );
    return () => window.clearInterval(id);
  }, [intervalSeconds, router]);

  function chooseInterval(seconds: number) {
    window.localStorage.setItem(INTERVAL_STORAGE_KEY, String(seconds));
    setIntervalSeconds(seconds);
  }

  const age = Math.max(0, now - fetchedAt);

  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      {age > STALE_AFTER_SECONDS && (
        <AdminStatusPill tone="warning" label="Stale" />
      )}
      <span className="text-xs text-admin-text-muted">
        Updated {formatRelative(fetchedAt, now)}
      </span>
      <label className="flex items-center gap-1.5 text-xs text-admin-text-muted">
        <span className="sr-only sm:not-sr-only">Auto</span>
        <select
          value={intervalSeconds}
          onChange={(event) => chooseInterval(Number(event.target.value))}
          className="rounded-md border border-admin-border bg-admin-surface px-2 py-1 text-xs text-admin-text-secondary"
        >
          {INTERVALS.map((seconds) => (
            <option key={seconds} value={seconds}>
              {seconds === 0 ? "Off" : `${seconds}s`}
            </option>
          ))}
        </select>
      </label>
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={pending}
        onClick={() => startTransition(() => router.refresh())}
      >
        <RefreshCw className={pending ? "animate-spin" : undefined} />
        Refresh
      </Button>
    </div>
  );
}
