import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";

import type { PGlite } from "@electric-sql/pglite";
import type { AppDb } from "@/db/types";

function migrationsFolder(): string {
  return path.join(process.cwd(), "drizzle");
}

export async function ensureSchema(db: AppDb): Promise<void> {
  const folder = migrationsFolder();
  const client = (db as unknown as { $client: PGlite }).$client;

  const sqlFiles = readdirSync(folder)
    .filter((f) => f.endsWith(".sql"))
    .sort();

  for (const file of sqlFiles) {
    const sql = readFileSync(path.join(folder, file), "utf-8");
    const statements = sql
      .split("--> statement-breakpoint")
      .map((s) => s.trim())
      .filter(Boolean);

    for (const stmt of statements) {
      await client.exec(stmt);
    }
  }
}
