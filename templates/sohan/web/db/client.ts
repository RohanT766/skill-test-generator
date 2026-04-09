import fs from "node:fs";
import { PGlite } from "@electric-sql/pglite";
import { drizzle } from "drizzle-orm/pglite";

import { ensureSchema } from "@/db/bootstrap";
import * as schema from "@/db/schema";
import type { AppDb } from "@/db/types";

let client: PGlite | null = null;
let db: AppDb | null = null;
let initPromise: Promise<void> | null = null;
let autoSeedStarted = false;

function getDataDir(): string {
  const port = process.env.PORT ?? process.env.APP_PORT ?? "3000";
  return process.env.PGLITE_DATA_DIR ?? `/tmp/pglite-data/${port}`;
}

export async function getDb(): Promise<AppDb> {
  if (!initPromise) {
    initPromise = (async () => {
      const dataDir = getDataDir();
      fs.mkdirSync(dataDir, { recursive: true });
      const c = new PGlite(dataDir);
      const d = drizzle(c, { schema });
      await ensureSchema(d);
      client = c;
      db = d;
    })().catch((err) => {
      initPromise = null;
      throw err;
    });
  }
  await initPromise;

  if (!autoSeedStarted) {
    autoSeedStarted = true;
    try {
      const seedMod = await import("@/db/seed");
      if (typeof seedMod.seedDatabase === "function") {
        await seedMod.seedDatabase();
      }
    } catch (e) {
      console.warn("auto-seed:", e instanceof Error ? e.message : e);
    }
  }

  return db!;
}

export async function closeDb(): Promise<void> {
  if (client) {
    await client.close();
  }
  client = null;
  db = null;
  initPromise = null;
}
