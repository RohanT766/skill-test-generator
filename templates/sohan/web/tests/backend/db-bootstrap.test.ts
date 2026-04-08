import path from "node:path";

import { describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  migrate: vi.fn(),
}));

vi.mock("drizzle-orm/pglite/migrator", () => ({
  migrate: mocks.migrate,
}));

import { ensureSchema } from "@/db/bootstrap";

describe("ensureSchema", () => {
  it("runs drizzle pglite migrations from the project drizzle folder", async () => {
    const db = {} as never;
    await ensureSchema(db);

    expect(mocks.migrate).toHaveBeenCalledWith(db, {
      migrationsFolder: path.join(process.cwd(), "drizzle"),
    });
  });
});
