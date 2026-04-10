/**
 * Plato mutation logger — reports DB writes to the Plato scoring system.
 *
 * When running on a Plato sim VM, env vars PLATO_JOB_ID and PLATO_API_URL
 * are set. After each successful INSERT/UPDATE/DELETE the API route should
 * call `logMutation(...)`. If the env vars are missing (local dev), this
 * is a silent no-op.
 */

const PLATO_JOB_ID = process.env.PLATO_JOB_ID || process.env.JOB_ID;
const PLATO_API_URL = (
  process.env.PLATO_API_URL || process.env.PLATO_BASE_URL || "https://plato.so"
).replace(/\/+$/, "").replace(/\/api$/, "");

const enabled = Boolean(PLATO_JOB_ID);

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
  if (!enabled) return;

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
    await fetch(`${PLATO_API_URL}/api/v2/jobs/${PLATO_JOB_ID}/log`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    // Best-effort — never block the API response
  }
}
