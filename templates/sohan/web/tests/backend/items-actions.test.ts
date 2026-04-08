import { describe, expect, it } from "vitest";

import {
  CreateItemInputSchema,
  GetItemByIdInputSchema,
  ItemDtoSchema,
  ListItemsInputSchema,
} from "@/lib/contracts/items";

describe("item contracts", () => {
  it("defaults list query to empty string", () => {
    expect(ListItemsInputSchema.parse({})).toEqual({ query: "" });
  });

  it("validates create item payload", () => {
    expect(
      CreateItemInputSchema.parse({
        title: "Signal Desk Clock",
        priceCents: 4200,
      }),
    ).toEqual({
      title: "Signal Desk Clock",
      priceCents: 4200,
    });
    expect(() =>
      CreateItemInputSchema.parse({
        title: "",
        priceCents: 0,
      }),
    ).toThrow();
  });

  it("validates item identifiers and dto shape", () => {
    expect(GetItemByIdInputSchema.parse({ id: "item_123" })).toEqual({ id: "item_123" });
    expect(() => GetItemByIdInputSchema.parse({ id: "" })).toThrow();

    expect(
      ItemDtoSchema.parse({
        id: "item_123",
        title: "Signal Desk Clock",
        priceCents: 4200,
        imageUrl: null,
        createdAt: "2026-01-01T00:00:00.000Z",
      }),
    ).toBeDefined();
  });
});
