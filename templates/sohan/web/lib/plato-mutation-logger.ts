/**
 * Plato mutation logger — reports DB writes to the Plato scoring system.
 *
 * Job ID discovery (in order):
 *   1. PLATO_JOB_ID / JOB_ID env var
 *   2. Extracted from the request Host header ({job_id}--{port}.sims.plato.so)
 *
 * After each successful INSERT/UPDATE/DELETE the API route should call
 * `logMutation(...)`. If no job ID can be determined (local dev), this
 * is a silent no-op.
 */

import { headers } from "next/headers";

const PLATO_API_URL = (
  process.env.PLATO_API_URL || process.env.PLATO_BASE_URL || "https://plato.so"
).replace(/\/+$/, "").replace(/\/api$/, "");

let _cachedJobId: string | undefined;

async function getJobId(): Promise<string | null> {
  const envId = process.env.PLATO_JOB_ID || process.env.JOB_ID;
  if (envId) return envId;

  if (_cachedJobId) return _cachedJobId;

  try {
    const h = await headers();
    const host = h.get("host") || "";
    const m = host.match(/^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:--\d+)?\.sims\.plato\.so$/);
    if (m) {
      _cachedJobId = m[1];
      return m[1];
    }
  } catch {
    // headers() unavailable outside request context
  }
  return null;
}

export type MutationAction = "insert" | "update" | "delete";

interface MutationPayload {
  tablename: string;
  action: MutationAction;
  row_filter: Record<string, unknown>;
  values?: Record<string, unknown>;
}

/**
 * Fire-and-forget: POST mutation to Plato's job log endpoint.
 * No auth required — the endpoint is a VM callback.
 */
export async function logMutation(
  tablename: string,
  action: MutationAction,
  rowFilter: Record<string, unknown>,
  values?: Record<string, unknown>,
): Promise<void> {
  const jobId = await getJobId();
  if (!jobId) return;

  const mutation: MutationPayload = {
    tablename,
    action,
    row_filter: rowFilter,
    ...(values !== undefined && { values }),
  };

  const body = {
    source: "simulator",
    type: "state_mutation",
    timestamp: new Date().toISOString(),
    mutation,
  };

  try {
    await fetch(`${PLATO_API_URL}/api/v2/jobs/${jobId}/log`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    // Best-effort — never block the API response
  }
}
