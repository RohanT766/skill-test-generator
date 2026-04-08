import { integer, pgTable, text, timestamp } from "drizzle-orm/pg-core";

export const items = pgTable("test_table", {
  id: text("id").primaryKey(),
  title: text("title").notNull(),
  priceCents: integer("price_cents").notNull(),
  imageUrl: text("image_url"),
  createdAt: timestamp("created_at", { withTimezone: true }).defaultNow().notNull(),
});
