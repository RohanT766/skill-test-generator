import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  const pgliteInstances: Array<{ close: ReturnType<typeof vi.fn> }> = [];
  const PGlite = vi.fn(
    class MockPGlite {
      close = vi.fn().mockResolvedValue(undefined);

      constructor() {
        pgliteInstances.push(this);
      }
    },
  );
  return {
    PGlite,
    drizzle: vi.fn(),
    ensureSchema: vi.fn().mockResolvedValue(undefined),
    pgliteInstances,
  };
});

vi.mock("@electric-sql/pglite", () => ({
  PGlite: mocks.PGlite,
}));

vi.mock("drizzle-orm/pglite", () => ({
  drizzle: mocks.drizzle,
}));

vi.mock("@/db/bootstrap", () => ({
  ensureSchema: mocks.ensureSchema,
}));

describe("db client", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    mocks.pgliteInstances.length = 0;
    mocks.drizzle.mockImplementation(() => ({ tag: "db" }));
    delete process.env.PGLITE_DATA_DIR;
  });

  it("uses default data dir when PGLITE_DATA_DIR is not set", async () => {
    const client = await import("@/db/client");
    await client.getDb();

    expect(mocks.PGlite).toHaveBeenCalledWith("/tmp/pglite-data/3000");
  });

  it("uses PGLITE_DATA_DIR when set", async () => {
    process.env.PGLITE_DATA_DIR = "/tmp/custom-pglite";
    const client = await import("@/db/client");
    await client.getDb();

    expect(mocks.PGlite).toHaveBeenCalledWith("/tmp/custom-pglite");
  });

  it("initializes once, ensures schema once, and resets on close", async () => {
    const client = await import("@/db/client");

    const db1 = await client.getDb();
    const db2 = await client.getDb();

    expect(db1).toBe(db2);
    expect(mocks.PGlite).toHaveBeenCalledTimes(1);
    expect(mocks.drizzle).toHaveBeenCalledTimes(1);
    expect(mocks.ensureSchema).toHaveBeenCalledTimes(1);

    await client.closeDb();
    expect(mocks.pgliteInstances[0]?.close).toHaveBeenCalledTimes(1);

    await client.getDb();
    expect(mocks.PGlite).toHaveBeenCalledTimes(2);
    expect(mocks.drizzle).toHaveBeenCalledTimes(2);
    expect(mocks.ensureSchema).toHaveBeenCalledTimes(2);
  });

  it("concurrent getDb calls create only one PGlite instance", async () => {
    const client = await import("@/db/client");

    const [db1, db2, db3] = await Promise.all([
      client.getDb(),
      client.getDb(),
      client.getDb(),
    ]);

    expect(db1).toBe(db2);
    expect(db2).toBe(db3);
    expect(mocks.PGlite).toHaveBeenCalledTimes(1);
    expect(mocks.ensureSchema).toHaveBeenCalledTimes(1);
  });

  it("retries after init failure", async () => {
    mocks.ensureSchema.mockRejectedValueOnce(new Error("migration failed"));
    const client = await import("@/db/client");

    await expect(client.getDb()).rejects.toThrow("migration failed");

    // Second call should retry (not return cached rejected promise)
    mocks.ensureSchema.mockResolvedValueOnce(undefined);
    const db = await client.getDb();
    expect(db).toEqual({ tag: "db" });
    expect(mocks.PGlite).toHaveBeenCalledTimes(2);
    expect(mocks.ensureSchema).toHaveBeenCalledTimes(2);
  });

  it("does not expose db before ensureSchema completes", async () => {
    let schemaResolved = false;
    mocks.ensureSchema.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          setTimeout(() => {
            schemaResolved = true;
            resolve();
          }, 50);
        }),
    );

    const client = await import("@/db/client");
    const db = await client.getDb();

    // By the time getDb() resolves, schema must have been applied
    expect(schemaResolved).toBe(true);
    expect(db).toEqual({ tag: "db" });
  });
});
