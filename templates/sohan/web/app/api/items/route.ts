// TEMPLATE REFERENCE — this file is a scaffold showing the expected pattern for
// API route handlers.  Concrete imports are commented out because the db_schema
// agent rewrites the DB schema, which would break these imports.
// The backend_builder agent should replace this file with real implementations.
//
// import type { NextRequest } from "next/server";
// import { NextResponse } from "next/server";
// import { z } from "zod";
//
// import { getDb } from "@/db/client";
// import { createItem, listItems } from "@/lib/server/items-repo";
//
// export const runtime = "nodejs";
//
// const createItemSchema = z.object({
//   title: z.string().min(1),
//   priceCents: z.number().int().positive(),
//   imageUrl: z.string().url().optional(),
// });
//
// export async function GET(request: NextRequest) {
//   const query = request.nextUrl.searchParams.get("q") ?? "";
//   const db = await getDb();
//   const data = await listItems(db, query);
//   return NextResponse.json(data);
// }
//
// export async function POST(request: NextRequest) {
//   const rawBody = await request.json();
//   const parsed = createItemSchema.safeParse(rawBody);
//   if (!parsed.success) {
//     return NextResponse.json({ error: parsed.error.flatten() }, { status: 400 });
//   }
//   const db = await getDb();
//   const created = await createItem(db, parsed.data);
//   return NextResponse.json(created, { status: 201 });
// }

export {};
