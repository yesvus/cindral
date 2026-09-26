export interface LeaseInfo {
  job_id: string;
  repository: string;
  sha: string;
  ref: string;
  lease_expires: number;
  remaining_seconds: number;
}

export interface DeviceInfo {
  name: string;
  status: string;
  healthy: boolean;
  busy: boolean;
  labels: string[];
  current_lease: LeaseInfo | null;
}

export interface JobInfo {
  id: string;
  repository: string;
  sha: string;
  ref: string;
  command: string[];
  labels: string[];
  timeout: number;
  status: "pending" | "running" | "success" | "failure";
  device: string | null;
  exit_code: number | null;
  attempts: number;
  status_sha: string | null;
  log: string;
}

export interface PoolSnapshot {
  devices: DeviceInfo[];
  queue_depth: {
    pending: number;
    running: number;
    success: number;
    failure: number;
  };
  oldest_pending_age_seconds: number;
  reclaim_count: number;
  success_count: number;
  failure_count: number;
  recent_jobs: JobInfo[];
}

const CINDRAL_API_URL = process.env.CINDRAL_API_URL || "http://127.0.0.1:8095";
const CINDRAL_AGENT_TOKEN = process.env.CINDRAL_AGENT_TOKEN || "";

export async function getPoolSnapshot(): Promise<PoolSnapshot | null> {
  try {
    const headers: Record<string, string> = {
      Accept: "application/json",
    };
    if (CINDRAL_AGENT_TOKEN) {
      headers.Authorization = `Bearer ${CINDRAL_AGENT_TOKEN}`;
    }

    const res = await fetch(`${CINDRAL_API_URL}/v1/pool`, {
      headers,
      cache: "no-store",
    });

    if (!res.ok) {
      return null;
    }

    return (await res.json()) as PoolSnapshot;
  } catch {
    return null;
  }
}

export async function getJob(id: string): Promise<JobInfo | null> {
  try {
    const headers: Record<string, string> = {
      Accept: "application/json",
    };
    if (CINDRAL_AGENT_TOKEN) {
      headers.Authorization = `Bearer ${CINDRAL_AGENT_TOKEN}`;
    }

    const res = await fetch(`${CINDRAL_API_URL}/v1/jobs/${id}`, {
      headers,
      cache: "no-store",
    });

    if (!res.ok) {
      return null;
    }

    const data = await res.json();
    return (data.job || data) as JobInfo;
  } catch {
    return null;
  }
}
