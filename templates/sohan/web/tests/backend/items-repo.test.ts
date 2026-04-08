import { eq } from "drizzle-orm";
import { describe, expect, it } from "vitest";

import { items } from "@/db/schema";
import { sharedPglite } from "@/tests/helpers/pglite-db";

const { db } = sharedPglite();

describe("items schema", () => {
  it("supports insert and select by id in pglite", async () => {
    await db().insert(items).values({
      id: "item_001",
      title: "Canvas Weekender Bag",
      priceCents: 8900,
      imageUrl: null,
    });

    const rows = await db()
      .select()
      .from(items)
      .where(eq(items.id, "item_001"))
      .limit(1);
    const row = rows[0];
    expect(row?.title).toBe("Canvas Weekender Bag");
    expect(row?.priceCents).toBe(8900);
    expect(row?.createdAt).toBeDefined();
  });

  it("supports title filtering via SQL", async () => {
    await db().insert(items).values([
      {
        id: "item_001",
        title: "Signal Desk Clock",
        priceCents: 4200,
        imageUrl: null,
      },
      {
        id: "item_002",
        title: "Studio Lamp",
        priceCents: 5900,
        imageUrl: null,
      },
    ]);

    const filtered = await db()
      .select()
      .from(items)
      .where(eq(items.title, "Signal Desk Clock"));
    expect(filtered.length).toBe(1);
    expect(filtered[0]?.id).toBe("item_001");
  });
});
