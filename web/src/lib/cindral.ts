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
  leases: LeaseInfo[];
}

export type JobStatus = "pending" | "running" | "success" | "failure";

export interface JobInfo {
  id: string;
  repository: string;
  sha: string;
  ref: string;
  command: string[];
  labels: string[];
  timeout: number;
  status: JobStatus;
  device: string | null;
  exit_code: number | null;
  attempts: number;
  status_sha: string | null;
  log: string;
  log_truncated?: boolean;
}

export interface PoolSnapshot {
  devices: DeviceInfo[];
  queue_depth: {
    pending: number;
    running: number;
    success: number;
    failure: number;
  };
  expired_lease_count: number;
  oldest_pending_age_seconds: number;
  reclaim_count: number;
  success_count: number;
  failure_count: number;
  recent_jobs: JobInfo[];
}

// a trailing slash would produce "//v1/pool", which the broker does not match
const BROKER_URL = (process.env.CINDRAL_API_URL || "http://127.0.0.1:8095").replace(
  /\/+$/,
  ""
);
const AGENT_TOKEN = process.env.CINDRAL_AGENT_TOKEN || "";
// fail fast instead of pinning a request worker when the broker stops responding
const REQUEST_TIMEOUT_MS = Number(process.env.CINDRAL_API_TIMEOUT_MS || 5000);

export type BrokerFailure = "unauthorized" | "unavailable";

export class BrokerError extends Error {
  readonly reason: BrokerFailure;

  constructor(reason: BrokerFailure, message: string) {
    super(message);
    this.reason = reason;
  }
}

async function brokerFetch(path: string): Promise<Response> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (AGENT_TOKEN) {
    headers.Authorization = `Bearer ${AGENT_TOKEN}`;
  }
  return fetch(`${BROKER_URL}${path}`, {
    headers,
    cache: "no-store",
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
}

export async function getPoolSnapshot(): Promise<PoolSnapshot> {
  let res: Response;
  try {
    res = await brokerFetch("/v1/pool");
  } catch {
    throw new BrokerError("unavailable", "the Cindral broker did not respond");
  }
  if (res.status === 401 || res.status === 403) {
    throw new BrokerError("unauthorized", "the broker rejected the agent token");
  }
  if (!res.ok) {
    throw new BrokerError("unavailable", `the broker returned ${res.status}`);
  }
  return (await res.json()) as PoolSnapshot;
}

export async function getJob(id: string): Promise<JobInfo> {
  let res: Response;
  try {
    res = await brokerFetch(`/v1/jobs/${encodeURIComponent(id)}`);
  } catch {
    throw new BrokerError("unavailable", "the Cindral broker did not respond");
  }
  if (res.status === 401 || res.status === 403) {
    throw new BrokerError("unauthorized", "the broker rejected the agent token");
  }
  if (res.status === 404) {
    throw new BrokerError("unavailable", "job not found");
  }
  if (!res.ok) {
    throw new BrokerError("unavailable", `the broker returned ${res.status}`);
  }
  const payload = await res.json();
  return (payload.job ?? payload) as JobInfo;
}

const STATUS_TONES: Record<JobStatus, "success" | "warning" | "error" | "info"> = {
  success: "success",
  running: "warning",
  failure: "error",
  pending: "info",
};

export function jobStatusTone(status: JobStatus) {
  return STATUS_TONES[status] ?? "info";
}
