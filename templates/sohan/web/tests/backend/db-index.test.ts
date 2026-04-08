import { describe, expect, it } from "vitest";

import * as db from "@/db";

describe("db index exports", () => {
  it("re-exports runtime helpers and schema", () => {
    expect(typeof db.getDb).toBe("function");
    expect(typeof db.closeDb).toBe("function");
    expect(typeof db.ensureSchema).toBe("function");
    expect(db.schema.items).toBeDefined();
  });
});
