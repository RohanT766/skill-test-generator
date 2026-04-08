import type { PgliteDatabase } from "drizzle-orm/pglite";
import type * as schema from "@/db/schema";

export type AppDb = PgliteDatabase<typeof schema>;
