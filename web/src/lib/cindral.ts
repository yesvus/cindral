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
  // A broker that predates the agent heartbeat reports none of these, so they
  // are optional and the columns stay hidden until the broker sends them.
  // available_slots is null while the agent is not connected, because a device
  // that stopped reporting takes no new work; max_concurrency is the limit it
  // was last configured with and survives a disconnect.
  agent_online?: boolean;
  // what the device says it is running. A device agent is a checkout on the
  // host, so this commit is the only place its age is visible, and build_known
  // false means the device did not say rather than that it said nothing
  build?: { version?: string; commit?: string; built_at?: string };
  build_known?: boolean;
  last_seen?: number | null;
  max_concurrency?: number | null;
  active_jobs?: number;
  available_slots?: number | null;
}

/** `dead` is terminal: the run lost its device past the attempt cap. */
export type JobStatus = "pending" | "running" | "success" | "failure" | "dead";

/**
 * Why a pending run older than five minutes has not moved, or null when the
 * queue cannot say: it holds no reason it can defend, and a wrong one sends the
 * reader after hardware the pool does not need.
 *
 * - `no_agents`: nothing is connected
 * - `idle_devices`: every connected device is at capacity
 * - `deferred`: nothing claimable is waiting, the queue is inside a retry backoff
 * - `no_eligible_device`: a claimable run needs labels no connected device has
 */
export type StallReason =
  | "no_agents"
  | "idle_devices"
  | "deferred"
  | "no_eligible_device";

/** What a reader should do about each reason the broker names. */
export const STALL_ADVICE: Record<StallReason, string> = {
  no_agents: "no device agent is connected. Start one on a device in the pool.",
  idle_devices:
    "every connected device is running its slots. Add a device, or wait for a run to finish.",
  deferred:
    "nothing claimable is waiting, the queue is inside a retry backoff. No action needed.",
  no_eligible_device:
    "no connected device advertises the labels a waiting run needs. Check device labels and job requirements.",
};

export interface StepInfo {
  index: number;
  name: string;
  output: string;
  /** false when the run stopped in this step or it never reported */
  completed: boolean;
  /** set when this step's name or output was bounded, not only its output */
  truncated?: boolean;
  /** steps that ran but are not in the set, carried on the last kept step */
  dropped?: number;
}

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
  // the broker sends this on every run, so it is declared rather than optional: a
  // key the broker sends and the panel does not name is refused by name on a write
  last_error: string | null;
  status_sha: string | null;
  log: string;
  log_truncated?: boolean;
  // per-step records the device reported. A broker predating them sends none,
  // and an empty list means the run announced no step, so the view must
  // distinguish "no records" from "nothing ran" rather than showing a blank
  steps?: StepInfo[];
  // typed optional and rendered only when present, so a broker that does not
  // report a dispatch time omits the column rather than filling it with blanks
  created_at?: number;
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
  stalled_reason?: StallReason | null;
  reclaim_count: number;
  /**
   * reclaims taken from a device that was still connected, which is a run the
   * queue restarted rather than lost. A broker predating the split sends none.
   */
  live_device_reclaim_count?: number;
  success_count: number;
  failure_count: number;
  recent_jobs: JobInfo[];
  // a broker predating build reporting sends none, so the panel omits the
  // version rather than showing a placeholder that could be mistaken for one
  build?: { version: string; commit: string; built_at: string };
}

// a trailing slash would produce "//v1/pool", which the broker does not match
const BROKER_URL = (process.env.CINDRAL_API_URL || "http://127.0.0.1:8095").replace(
  /\/+$/,
  ""
);
// read-only credential: the panel must not hold the token that can claim and
// report jobs on the device pool
const POOL_TOKEN = process.env.CINDRAL_POOL_TOKEN || "";
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
  if (POOL_TOKEN) {
    headers.Authorization = `Bearer ${POOL_TOKEN}`;
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
    throw new BrokerError(
      "unauthorized",
      "the broker rejected the pool credential; check CINDRAL_POOL_TOKEN",
    );
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
    throw new BrokerError(
      "unauthorized",
      "the broker rejected the pool credential; check CINDRAL_POOL_TOKEN",
    );
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
  // a run that lost its device past the attempt cap never got a verdict, and
  // an attempt cap reached is a red state rather than an informational one
  dead: "error",
  pending: "info",
};

export function jobStatusTone(status: JobStatus) {
  return STATUS_TONES[status] ?? "info";
}
