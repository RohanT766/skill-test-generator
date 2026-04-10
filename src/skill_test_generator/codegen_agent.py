"""Build instructions for the claude-code agent that finishes/fixes pre-generated code."""

from __future__ import annotations

import json


_PROTECTED_FILES = (
    "package.json, tsconfig.json, next.config.ts, next.config.js, "
    "postcss.config.mjs, tailwind.config.ts, "
    "db/client.ts, db/bootstrap.ts, db/types.ts, db/index.ts, "
    "lib/plato-mutation-logger.ts, "
    "components/theme-provider.tsx, app/providers.tsx, app/globals.css"
)


def build_codegen_instruction(
    spec: dict,
    slug: str,
    variant_dir: str,
    verify_port: int = 4000,
    files_written: list[str] | None = None,
    validation_errors: list[str] | None = None,
    deps_installed: bool = False,
    check_results: list[dict] | None = None,
) -> str:
    """Produce the natural-language instruction for a claude-code agent session.

    Only called when automated verification failed. ``check_results`` contains
    the pass/fail status of each check so the agent can jump straight to fixing.
    """

    api_routes = spec.get("api_routes", [])
    verify_routes: list[str] = []
    for r in api_routes:
        route = r.get("route", "")
        if route and "[" not in route and "/health" not in route:
            verify_routes.append(route)

    verify_cmds = "\n".join(
        f"   curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{verify_port}{r}"
        for r in verify_routes
    )

    # --- Build check-results section (from world VM verification) ---
    checks_section = ""
    if check_results:
        passed = [c for c in check_results if c.get("pass")]
        failed = [c for c in check_results if not c.get("pass")]
        checks_section = "## Automated verification results\n\n"
        checks_section += (
            f"The world VM already tried running this app. "
            f"**{len(passed)} checks passed, {len(failed)} failed.**\n\n"
        )
        if failed:
            checks_section += "### FAILED checks (fix these):\n"
            for c in failed:
                checks_section += f"- **{c['name']}**: {c.get('error', 'unknown')}\n"
            checks_section += "\n"
        if passed:
            checks_section += "### Passed checks:\n"
            for c in passed:
                checks_section += f"- {c['name']}: OK\n"
            checks_section += "\n"

    # --- Build file manifest section ---
    files_section = ""
    if files_written:
        files_section = "## Pre-generated files\n\nThese files were written by the code generator:\n"
        for f in files_written:
            files_section += f"- {f}\n"
        files_section += "\nYou do NOT need to read every file. Focus on fixing the failed checks.\n\n"

    # --- Build known-issues section ---
    issues_section = ""
    if validation_errors:
        issues_section = "## Known issues (fix these first)\n\nAutomated validation found these problems:\n"
        for err in validation_errors:
            issues_section += f"- {err}\n"
        issues_section += "\nFix these before running the dev server.\n\n"

    return f"""\
You are fixing a pre-generated Next.js web application that failed automated
verification. Code was written by a one-shot generator. The world VM already
attempted to run and verify it but some checks failed (see below).

Your job: fix the failures, get everything working, verify, and report success.

## CRITICAL: Work from local disk, NOT NFS

The source code lives on NFS at {variant_dir}/web/ — but NFS is too slow
for webpack and node_modules. You MUST copy source to local disk first:

```bash
cp -r {variant_dir}/web /tmp/work-{slug}
cd /tmp/work-{slug}
mkdir -p /tmp/pglite-data/{verify_port}
bun install
```

Do ALL your work in /tmp/work-{slug}. Edit files there, run the server
from there. NEVER try to move, symlink, or install node_modules on NFS.
When done, copy changed files back to the NFS location.

## Source and spec

NFS source: {variant_dir}/web/
Spec: {variant_dir}/spec.json
Your working copy: /tmp/work-{slug}/

{checks_section}{files_section}{issues_section}## Step 1: Build and start the production server

```bash
cd /tmp/work-{slug}
NODE_ENV=production NEXT_DIST_DIR=.next node ./node_modules/next/dist/bin/next build
PGLITE_DATA_DIR=/tmp/pglite-data/{verify_port} NODE_ENV=production NEXT_DIST_DIR=.next PORT={verify_port} APP_PORT={verify_port} node ./node_modules/next/dist/bin/next start --hostname 0.0.0.0 -p {verify_port} &
```

Wait for http://localhost:{verify_port}/api/health to return 200.

## Step 2: Fix failing checks

Check ALL API routes return 200 with actual data:
{verify_cmds}

If anything fails — 500 errors, empty data, missing routes — kill the server,
fix the code in /tmp/work-{slug}/, rebuild with `next build`, and restart.

Common issues:
- db/schema.ts columns don't match drizzle/0000_zippy_changeling.sql
- db/seed.ts references non-existent schema columns
- API routes import tables not exported by db/schema.ts
- Missing --> statement-breakpoint between CREATE TABLE statements
- Missing lazy seedDatabase() call in GET API routes
- Using bare `db` import instead of `const db = await getDb()`

DO NOT modify these protected template files:
  {_PROTECTED_FILES}

## Technical reference

DATABASE: import {{ getDb }} from '@/db/client'; const db = await getDb();
SCHEMA: pgTable from drizzle-orm/pg-core in db/schema.ts
SEED: import getDb from './client' (NOT './db'), check existing data before insert
LAYOUT: <body suppressHydrationWarning>, <Providers> wrapper
IMPORTS: shadcn from @/components/ui/<name>, schema from @/db/schema
MUTATION LOGGING: Every write route (PUT/PATCH/POST/DELETE) that modifies
the DB must call `logMutation` from `@/lib/plato-mutation-logger` after
the Drizzle write succeeds:
  import {{ logMutation }} from "@/lib/plato-mutation-logger";
  await logMutation("table_name", "update", {{ id: numId }}, newValues);
The row_filter must use the numeric primary key. This is required for
scoring — if it's missing, mutation tests will always fail.

## Step 3: Copy fixes back and finalize

Once ALL routes return 200 with data:
1. Stop the dev server
2. Copy your changed files back to NFS:
   ```bash
   rsync -a --exclude node_modules --exclude .next /tmp/work-{slug}/ {variant_dir}/web/
   ```
3. Write {variant_dir}/codegen_result.json:
```json
{{
  "status": "success",
  "files_written": [...list of files you modified or created...],
  "api_routes_verified": {json.dumps(verify_routes)}
}}
```

If you cannot fix everything, write the result with "status": "partial"
and an "errors" array explaining what's still broken.
"""
