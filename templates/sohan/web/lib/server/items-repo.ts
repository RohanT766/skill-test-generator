// TEMPLATE REFERENCE — this file is a scaffold showing the expected pattern for
// repository modules.  The db_schema agent will rewrite db/schema.ts, so concrete
// imports/exports are commented out to avoid build failures.  The backend_builder
// agent should replace this file with real implementations.
//
// import { desc, eq, ilike } from "drizzle-orm";
//
// import type { AppDb } from "@/db/types";
// import { items } from "@/db/schema";
//
// export type ItemRecord = {
//   id: string;
//   title: string;
//   priceCents: number;
//   imageUrl: string | null;
//   createdAt: string;
// };
//
// export type CreateItemInput = {
//   title: string;
//   priceCents: number;
//   imageUrl?: string;
// };
//
// export async function listItems(db: AppDb, query: string): Promise<ItemRecord[]> {
//   const normalized = query.trim();
//   const rows = normalized
//     ? await db.select().from(items).where(ilike(items.title, `%${normalized}%`)).orderBy(desc(items.createdAt))
//     : await db.select().from(items).orderBy(desc(items.createdAt));
//
//   return rows.map((row) => ({
//     id: row.id,
//     title: row.title,
//     priceCents: row.priceCents,
//     imageUrl: row.imageUrl,
//     createdAt: row.createdAt.toISOString(),
//   }));
// }
//
// export async function getItemById(db: AppDb, id: string): Promise<ItemRecord | null> {
//   const rows = await db.select().from(items).where(eq(items.id, id)).limit(1);
//   const row = rows[0];
//   if (!row) return null;
//   return {
//     id: row.id,
//     title: row.title,
//     priceCents: row.priceCents,
//     imageUrl: row.imageUrl,
//     createdAt: row.createdAt.toISOString(),
//   };
// }
//
// export async function createItem(db: AppDb, input: CreateItemInput): Promise<ItemRecord> {
//   const row = {
//     id: crypto.randomUUID(),
//     title: input.title,
//     priceCents: input.priceCents,
//     imageUrl: input.imageUrl ?? null,
//   };
//   await db.insert(items).values(row);
//   const created = await getItemById(db, row.id);
//   if (!created) throw new Error("Created item could not be loaded");
//   return created;
// }
