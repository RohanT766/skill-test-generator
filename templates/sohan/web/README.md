# Web App

Next.js fullstack app for webclone projects.

## Commands

```bash
bun install
bun run dev
bun run lint
bun run typecheck
bun run test:run
bun run db:generate
bun run db:migrate
bun run db:seed
```

## DB contract

- Drizzle schema lives in `db/schema.ts`.
- SQL migrations are generated into `drizzle/` from that schema.
- Runtime applies those migrations via Drizzle migrator (`db/bootstrap.ts` + `db/client.ts`).
- Backend tests use pglite and apply the same migrations (`tests/helpers/pglite-db.ts`).
- Frontend tests use Vitest + jsdom + RTL (`tests/frontend/*.test.tsx`).

No raw SQL schema bootstrap path should be used.

## Runtime env

- `PGLITE_DATA_DIR` (optional; defaults to `/tmp/pglite-data/3000`)
- `ENABLE_DB_SEED` (`0` by default; set to `1` to seed on startup)
