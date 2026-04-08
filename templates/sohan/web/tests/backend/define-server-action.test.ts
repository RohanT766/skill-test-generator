import { describe, expect, it } from "vitest";
import { z } from "zod";

import { defineServerAction } from "@/lib/server/actions/define-server-action";

describe("defineServerAction", () => {
  it("parses input and output through zod schemas", async () => {
    const action = defineServerAction({
      inputSchema: z.object({
        value: z.string().min(1),
      }),
      outputSchema: z.object({
        normalized: z.string(),
      }),
      handler: async ({ value }) => ({
        normalized: value.trim().toLowerCase(),
      }),
    });

    await expect(action({ value: "  HELLO  " })).resolves.toEqual({
      normalized: "hello",
    });
  });

  it("throws when input does not match schema", async () => {
    const action = defineServerAction({
      inputSchema: z.object({
        value: z.string().min(1),
      }),
      outputSchema: z.object({
        ok: z.literal(true),
      }),
      handler: async () => ({ ok: true as const }),
    });

    await expect(
      action({
        value: "",
      }),
    ).rejects.toThrow();
  });

  it("throws when handler output does not match output schema", async () => {
    const action = defineServerAction({
      inputSchema: z.object({
        value: z.string(),
      }),
      outputSchema: z.object({
        id: z.string().uuid(),
      }),
      handler: async () => ({
        id: "not-a-uuid",
      }),
    });

    await expect(action({ value: "x" })).rejects.toThrow();
  });
});
