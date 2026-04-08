import { NextResponse } from "next/server";

import { getDb } from "@/db/client";

export const runtime = "nodejs";

export async function GET() {
  const db = await getDb();
  // Simple connectivity check — avoid importing app schema/repo modules
  // since agents may modify those during build stages.
  await db.execute("SELECT 1");
  return NextResponse.json({ status: "ok" });
}
