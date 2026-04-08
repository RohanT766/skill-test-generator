# Webclone Next.js Template

VM-native template for webclone forks (no Docker runtime required).

## Stack

- Next.js App Router (single fullstack app)
- Drizzle ORM + PGlite (embedded PostgreSQL)
- Vitest + pglite backend tests
- shadcn-style UI primitives
- Shared Zod contracts + typed server actions
- TanStack Query + nuqs on the frontend
- Zustand for UI state

## Structure

- `web/app` - pages (and infra health route)
- `web/components` - UI and app components
- `web/db` - schema, runtime client, bootstrap helpers
- `web/lib/contracts` - shared request/response schemas and inferred TS types
- `web/lib/server` - typed server actions + backend repos
- `web/lib/query` - TanStack Query hooks calling typed server actions
- `web/lib/state` - local UI state
- `web/tests` - backend tests (pglite)

## Environment

Copy `.env.example` to `.env` and adjust values if needed.

Fixed host ports:

- Next.js app + API: `3000` (database is embedded via PGlite)

## Start / Stop

```bash
bash start.sh
bash stop.sh
```

## Validation

```bash
bash validate.sh
```
