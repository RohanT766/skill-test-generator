import { PGlite } from "@electric-sql/pglite";
import { sql } from "drizzle-orm";
import { drizzle } from "drizzle-orm/pglite";
import { afterAll, afterEach, beforeAll } from "vitest";

import { ensureSchema } from "@/db/bootstrap";
import * as schema from "@/db/schema";

type AppDb = ReturnType<typeof drizzle<typeof schema>>;

type PGliteWithPrivateState = PGlite & {
  mod?: unknown;
  fs?: unknown;
};

/**
 * Wraps PGlite.close() to release WASM linear memory.
 *
 * WebAssembly memory can grow but never shrink. PGLite's close() shuts down
 * Postgres but keeps the Emscripten module referenced, so the WASM heap is
 * retained until the *entire* PGlite object is garbage-collected. In test
 * runners like vitest the test closure often outlives the test, pinning the
 * WASM memory for the whole suite (~200 MB per instance × 300 tests = OOM).
 *
 * Nulling `mod` (the Emscripten module) after shutdown lets the GC reclaim
 * the WASM linear memory immediately, even while the PGlite reference is
 * still reachable.
 */
function patchClose(client: PGlite): void {
  const originalClose = client.close.bind(client);
  const mutableClient = client as PGliteWithPrivateState;
  client.close = async () => {
    try {
      await originalClose();
    } finally {
      mutableClient.mod = undefined;
      mutableClient.fs = undefined;
    }
  };
}

/**
 * Create a standalone PGlite database instance.
 *
 * Prefer {@link sharedPglite} for test files — it shares one instance
 * across all tests in the file and cleans up between tests automatically.
 */
export async function createPgliteDb() {
  const client = new PGlite();
  patchClose(client);
  const db = drizzle(client, { schema });
  await ensureSchema(db);
  return { client, db };
}

/**
 * Shared PGlite instance for an entire test file.
 *
 * Creates one PGlite + schema in `beforeAll`, truncates all public tables
 * in `afterEach`, and closes in `afterAll`. This is **~15× faster** than
 * creating a new PGlite per test (avoids repeated WASM cold-start + DDL)
 * and uses **~50% less memory**.
 *
 * Usage:
 * ```ts
 * import { sharedPglite } from "@/tests/helpers/pglite-db";
 *
 * const { db } = sharedPglite();
 *
 * describe("myRepo", () => {
 *   it("creates a record", async () => {
 *     await createRecord(db(), { ... });
 *     // db is clean for the next test — no manual cleanup needed
 *   });
 * });
 * ```
 */
export function sharedPglite() {
  let client: PGlite;
  let db: AppDb;

  beforeAll(async () => {
    const result = await createPgliteDb();
    client = result.client;
    db = result.db;
  });

  afterAll(async () => {
    await client?.close();
  });

  afterEach(async () => {
    const res = await db.execute(
      sql`SELECT string_agg(quote_ident(tablename), ', ') AS tables
          FROM pg_tables WHERE schemaname = 'public'`,
    );
    const tables = (res as { rows: { tables: string | null }[] }).rows[0]
      ?.tables;
    if (tables) {
      await db.execute(sql.raw(`TRUNCATE ${tables} CASCADE`));
    }
  });

  return { db: () => db, client: () => client };
}
