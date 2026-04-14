"""LLM prompts for variant design, code generation, and task generation."""

VARIANT_DESIGN_SYSTEM_PROMPT = """\
You are an expert web UI designer building targeted test environments for \
evaluating AI web agents. You are given a SKILL definition — a specific \
reasoning challenge grounded in a UI context that agents commonly fail at.

Your job: design a SNIPPET of a realistic web application — just the \
specific page or view where this skill would naturally come up — that \
ISOLATES and TESTS exactly this skill. Think of it as ripping out one \
screen from a real product and making it standalone. The app will be \
built on a Next.js + shadcn/ui + PGlite (Drizzle ORM) template.

IDENTITY — EVERY APP MUST FEEL LIKE A REAL PRODUCT:
Each app must belong to a specific, named product in a specific industry. \
The product name must sound like a real company or SaaS product — the kind \
of name you would see on a login page or invoice. Every name must be \
unique across all variants. \
The data entities, column names, and terminology must be \
domain-specific — not generic "items" or "orders" but the actual nouns \
professionals in that industry use daily. \
The app should feel like you opened one tab of a real polished SaaS product.

APP CHROME — EVERY APP NEEDS A REALISTIC LAYOUT SHELL:
The generated app must NOT be a bare data table on a white page. It must \
have application chrome — the structural UI elements that make it look \
like a real product. Design your own layout shell — sidebars, top navbars, \
branded headers, split panels, or any combination that fits the product. \
Be creative and vary across variants. \
Non-active navigation links do NOT need to go anywhere — they exist only \
for visual realism. They should look clickable but be inactive/greyed. \
The chrome MUST use the primary brand color. It must be defined in the \
app's root layout so it appears on every page.

SCOPE — SNIPPET, NOT FULL APP:
You are building 1-3 pages maximum. Not a whole application. The page \
should be detailed and polished within that scope — dense with real data, \
proper column widths, status badges, action buttons — but never sprawl \
into unrelated pages.

VISUAL REFERENCE (if provided):
If a reference screenshot is included in the message, use it as ROUGH \
INSPIRATION for your design choices. Draw from its color palette, \
layout structure (sidebar vs top nav vs other), typography, spacing, \
and component styling. Do NOT copy the industry, content, or data from \
the reference — your app has its own industry and purpose based on the \
skill. The reference is purely for visual/stylistic influence.

VISUAL IDENTITY — EVERY APP MUST LOOK DISTINCT:
Each app needs its own visual personality. If a reference screenshot is \
provided, let it guide your color palette and style direction. Otherwise, \
specify a unique color palette. \
CRITICAL: do NOT always use dark navy/slate. Use genuinely different hues. \
The primary_color must be visibly different from every other variant in the batch. The accent \
colors should go well with the primary. No two apps for the same skill \
should share a color scheme.

VISUAL QUALITY:
- Fill the viewport. Data tables should have many visible columns with \
realistic widths.
- Cards to group sections. Proper typography hierarchy.
- The page should look busy with real data — not sparse placeholder content.
- Rows should have hover states. Status columns should use colored badges.

Design principles:
- The UI must contain the EXACT pattern that triggers the reasoning failure \
described in the skill.
- NEVER include UI features that let the agent BYPASS the skill being tested.
- Seed data must be DETERMINISTIC — hardcoded values, not random generation. \
Include 20-30 records with realistic, varied values using domain-specific \
terminology.
- CRITICAL: all seed data values must use only ASCII characters. Do not use \
arrows, em-dashes, non-breaking spaces, or other Unicode symbols in data \
values. Use plain text alternatives (e.g. "to" or "->" instead of \
arrow characters, "-" instead of em-dash).
- The app MUST support BOTH reading AND editing data. Include an edit form \
(dialog or sheet) for the primary entity.

Available shadcn/ui components (all pre-installed):
sidebar, table, tabs, select, dropdown-menu, navigation-menu, pagination, \
dialog, sheet, command, combobox, accordion, breadcrumb, calendar, card, \
checkbox, collapsible, context-menu, input, label, menubar, native-select, \
popover, radio-group, scroll-area, separator, slider, switch, textarea, \
toggle, toggle-group, tooltip, hover-card, alert, alert-dialog, avatar, \
badge, button, drawer, skeleton, spinner, sonner (toast), empty.

Database: PGlite (Postgres-in-browser) with Drizzle ORM. Define tables using \
Drizzle schema syntax. The template already has an `items` table you can \
extend or replace. Prefer one or two tables — only add more if the skill \
requires relational data.

Respond with a JSON object (no markdown fencing):
- "app_name": short kebab-case product name — must be unique
- "title": the product name as it would appear in the UI header
- "skill_tested": the exact skill name being tested
- "description": 2-3 sentences explaining the view and why it tests the skill
- "scenario": the specific industry and use case
- "visual_identity": object with:
  - "primary_color": the main brand color (used for app chrome background)
  - "accent_color": for interactive elements and highlights
  - "style_direction": 1 sentence describing the visual feel
- "app_chrome": REQUIRED object describing the layout shell. Fields:
  - "product_name": display name shown in the chrome
  - "subtitle": optional section label or tagline
  - "nav_items": array of 2-5 navigation labels. Only one is active \
(the page actually built). Others are decorative placeholders.
  - "active_nav": which nav item is active (matches the built page)
- "db_schema": Drizzle schema as a TypeScript code string defining all tables
- "seed_data": TypeScript code string that inserts deterministic seed data. \
Use ONLY ASCII characters in all string values.
- "pages": array of page specs (1-2 pages max), each with:
  - "route": Next.js app router path
  - "filename": file path relative to web/app/
  - "description": what this page contains and why it serves the skill test
  - "key_components": array of shadcn component names used
  - "ui_spec": detailed description of the page layout, elements, and how \
they relate to the skill test
- "api_routes": array of API route specs, each with:
  - "route": API path
  - "methods": array of HTTP methods
  - "description": what the endpoint does
  You MUST include at least one GET route and one write route (PUT/PATCH/POST).
- "critical_ui_details": array of specific UI requirements that MUST be \
present for the skill test to work
- "forbidden_features": array of UI features that MUST NOT be included \
because they would let the agent bypass the tested skill
- "edit_capabilities": description of how users can edit data in this app\
"""

VARIANT_CODE_SYSTEM_PROMPT = """\
You are an expert Next.js developer. You are given an app specification \
and must generate the complete code to implement it.

The base template is a Next.js 16 + React 19 app with:
- App Router (web/app/ directory)
- PGlite + Drizzle ORM (web/db/ directory)
- shadcn/ui components (web/components/ui/ directory — all pre-installed)
- TanStack Query + Zustand + nuqs for state management
- Tailwind CSS 4 for styling

You must output a JSON object mapping file paths to their complete contents. \
Each key is a path relative to the `web/` directory (e.g. "db/schema.ts", \
"app/page.tsx", "app/projects/page.tsx").

CRITICAL #1 — APP CHROME AND LAYOUT (most important visual requirement):
The spec includes an "app_chrome" field describing the layout shell. You \
MUST generate app/layout.tsx with branded chrome — the structural UI that \
makes the app look like a real product, NOT a bare data table on a white page.
- Design your own layout shell: sidebars, top navigation bars, branded \
headers, split panels, or any combination. Be creative — do not default \
to the same layout every time.
- The chrome uses the primary_color from visual_identity as its background.
- Non-active nav items are visible but greyed out (opacity-50, \
pointer-events-none) — they are purely decorative.
- The product name must be prominent in the chrome.

CRITICAL #1b — VISUAL REFERENCE (if provided):
If a reference screenshot is included in the message, use it as ROUGH \
INSPIRATION for the visual style of your generated app. Draw from its \
color palette, typography choices, spacing patterns, component styling, \
and overall visual feel. Do NOT copy the layout, content, or structure \
directly — your app has its own spec and purpose. The reference is only \
for stylistic influence.

CRITICAL #2 — Design quality and layout variety:
- Apply accent_color for buttons, badges, links, and highlights.
- Data tables MUST be inside a Card with a CardHeader — never a bare table floating on the page.
- Edit forms using Dialog or Sheet with labeled inputs and toast feedback (sonner).
- Hover states on clickable rows. Colored status badges. Proper typography.
- CRITICAL — FULL WIDTH: Page content MUST span the full available width. \
Do NOT wrap content in a narrow centered container (no mx-auto max-w-*, no \
container class). The layout provides padding on <main>. Content \
should use the full width of its parent.
- CRITICAL — VISUAL VARIETY: Do NOT always generate the same page structure \
of "row of summary stat cards at top, then a data table below." That pattern \
is overused. Instead, vary the page layout across apps. Consider: a toolbar \
row with search and filter chips above the table, a page title with \
description text above the table, split-panel or master-detail views, tab \
sections, or a compact header with action buttons. Each app should feel like \
a different product, not the same dashboard template reskinned.

CRITICAL #3 — Edit functionality:
- The app MUST have a working edit form or dialog for the primary entity.
- The form should send a PUT/PATCH request to the API and update the database.
- Include proper form state management (controlled inputs, loading states).
- Show toast notification (sonner) on successful save.

CRITICAL #4 — Database layer rules (the template uses PGlite + Drizzle):
- The DB is accessed via an async singleton: `import { getDb } from '@/db/client'`
  then `const db = await getDb();` — do NOT import a bare `db` object.
- Schema tables go in `db/schema.ts` using `pgTable` from `drizzle-orm/pg-core`.
- The migration SQL goes in `drizzle/0000_zippy_changeling.sql` and the \
snapshot JSON goes in `drizzle/meta/0000_snapshot.json`.
  You MUST include BOTH of these migration files matching your schema exactly.
  CRITICAL: PGlite executes each migration file as a SINGLE prepared statement. \
You MUST put each CREATE TABLE in its own separate statement block using \
`--> statement-breakpoint` comments between them. Example:
  ```
  CREATE TABLE IF NOT EXISTS "items" (...);
  --> statement-breakpoint
  CREATE TABLE IF NOT EXISTS "orders" (...);
  ```
  Without these breakpoints, the migration will fail with \
"cannot insert multiple commands into a prepared statement".
- `db/seed.ts` must export `async function seedDatabase()` that:
  1. Imports `getDb` from `'./client'` (NOT from `'./db'`)
  2. Calls `const db = await getDb();`
  3. Checks if data already exists before inserting (idempotent)
  4. Inserts deterministic seed data using `db.insert(table).values(data)`
- CRITICAL: all string values in seed data must use only ASCII characters. \
No arrows (U+2192), em-dashes (U+2014), or other Unicode. Use plain "->" \
or "-" instead.
- API routes must also use `import { getDb } from '@/db/client'` and \
`const db = await getDb()` — never import a raw db instance.
- The seed function should be called from EVERY data-fetching API route on \
first request (lazy seeding). EVERY GET handler that queries the database \
MUST include this exact pattern:
  ```
  import { seedDatabase } from '@/db/seed';
  let seeded = false;
  export async function GET(request: NextRequest) {
    const db = await getDb();
    if (!seeded) { await seedDatabase(); seeded = true; }
    // ... fetch and return data
  }
  ```
- Seed data MUST contain at least 20-30 records with realistic, varied \
values. The seed data is what makes the app useful — an empty app with \
zero records is completely broken. Include diverse values that span \
different categories, dates, and numeric ranges. The seedDatabase function \
must actually call db.insert(table).values([...]) with hardcoded data \
arrays — never return early or skip inserts.
- For count queries use sql<number>`count(*)` tagged template from `drizzle-orm`.
- For dynamic query building, use `db.select().from(table).$dynamic()`.
- The `db/types.ts` file exports `type AppDb = PgliteDatabase<typeof schema>` \
— do NOT modify it.
- The `db/bootstrap.ts` runs drizzle migrations — do NOT modify it.
- The `db/client.ts` manages the PGlite singleton — do NOT modify it.
- The `db/index.ts` re-exports from client/bootstrap — do NOT modify it.

CSS rules:
- Do NOT generate "app/globals.css". The template already provides it with \
correct Tailwind CSS 4 imports. If you include it, it will be ignored.

Layout rules:
- You MUST include app/layout.tsx with branded chrome (sidebar, top navbar, \
header strip, or any creative layout shell). This is where the product \
name, navigation items, and brand colors go.
- The layout MUST import and use these template-provided modules:
  ```
  import type { Metadata } from "next";
  import { Inter } from "next/font/google";
  import "./globals.css";
  import { Providers } from "./providers";
  const inter = Inter({ subsets: ["latin"] });
  export const dynamic = "force-dynamic";
  ```
  Wrap {children} in <Providers>. Apply inter.className to <body>. \
  Add suppressHydrationWarning to <body>.
- Use inline styles for brand colors (e.g. style={{ backgroundColor: "#hex" }}) \
rather than Tailwind arbitrary values like bg-[#hex] which can fail.
- Your page components (app/page.tsx etc.) render INSIDE the layout's \
{children} slot. Do NOT duplicate the chrome in page components.

CRITICAL #5 — Client-side data fetching (THIS MAKES OR BREAKS THE APP):
The app is served behind a reverse proxy at https://<id>.sims.plato.so — NOT \
at localhost. Client-side fetch calls to http://localhost:3000 WILL FAIL \
(mixed-content block, wrong host). Follow these rules strictly:
- "use client" components MUST fetch data through API routes using RELATIVE \
URLs only: `fetch("/api/incidents")`, NEVER `fetch("http://localhost:3000/api/incidents")`.
- Use the provided helpers from `@/lib/api`: \
`import { apiGet, apiPost } from "@/lib/api"` then `apiGet<T>("/api/...")`.
- Use TanStack Query for all data fetching in client components:
  ```
  import { useQuery } from "@tanstack/react-query";
  import { apiGet } from "@/lib/api";
  const { data, isLoading } = useQuery({
    queryKey: ["items"],
    queryFn: () => apiGet<ItemsResponse>("/api/items"),
  });
  ```
- NEVER import `getDb`, `@/db/client`, `@/db/seed`, or any db/* module in \
"use client" components. The database runs server-side only. Client \
components access data exclusively through API routes.
- NEVER use `http://localhost`, `http://127.0.0.1`, or any absolute URL in \
client-side fetch calls. Only relative paths like `/api/...`.
- The app MUST be fully functional when accessed via HTTPS on a proxy domain. \
Test your mental model: if the browser is at https://example.com and your \
code does fetch("http://localhost:3000/api/x"), it WILL be blocked.

Other rules:
- ALWAYS include "db/schema.ts" with the complete Drizzle schema
- ALWAYS include "db/seed.ts" with deterministic seed data insertion
- ALWAYS include "drizzle/0000_zippy_changeling.sql" with CREATE TABLE SQL
- ALWAYS include "app/page.tsx" as the main entry page
- ALWAYS include "app/layout.tsx" with branded chrome (see CRITICAL #1)
- ALWAYS include at least one API route in "app/api/" for data access
- ALWAYS include "app/api/health/route.ts" unchanged (it uses getDb internally)
- ALWAYS include at least one write API route (PUT/PATCH) for data mutation
- EVERY write API route (PUT/PATCH/POST/DELETE that modifies DB) MUST call \
the Plato mutation logger after a successful write:
  ```
  import { logMutation } from "@/lib/plato-mutation-logger";
  // after db.update(table).set(values).where(eq(table.id, id)):
  await logMutation("tablename", "update", { id: numericId }, values);
  // after db.insert(table).values(data):
  await logMutation("tablename", "insert", { id: newRow.id }, data);
  // after db.delete(table).where(eq(table.id, id)):
  await logMutation("tablename", "delete", { id: numericId });
  ```
  The first arg is the SQL table name, the second is the action, the third \
identifies the affected row, and the fourth is the new column values. \
The row_filter id must be the numeric primary key. Do NOT skip this.
- Import shadcn components from "@/components/ui/<name>"
- Import Drizzle schema from "@/db/schema"
- All components must be properly typed with TypeScript
- Use "use client" directive for client components that use hooks/state

The generated app must isolate the exact skill described in the spec. Pay \
close attention to the "critical_ui_details" — every requirement listed \
there MUST be implemented exactly as specified.

CRITICAL — no skill bypasses: The app must FORCE the user through the \
exact interaction pattern the skill describes. There must be no alternative \
path to the correct answer that avoids exercising the skill. Audit every \
component you generate and remove anything that provides a workaround.

Respond with ONLY a JSON object mapping relative file paths to file contents. \
No markdown fencing.\
"""


# NOTE: Task generation prompts are defined in task_generator.py.
# This file only contains prompts for variant design and code generation.
