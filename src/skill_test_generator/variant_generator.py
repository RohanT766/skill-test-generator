"""Generate web app variants from skill definitions using LLM design + code generation."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path

import anthropic

from .config import SkillDefinition
from .json_utils import extract_json as _extract_json
from .prompts import VARIANT_CODE_SYSTEM_PROMPT, VARIANT_DESIGN_SYSTEM_PROMPT
from .skill_ingestion import _slugify

logger = logging.getLogger(__name__)


def _validate_code_files(code_files: dict[str, str]) -> list[str]:
    """Check generated code for structural issues. Returns list of errors."""
    errors: list[str] = []
    required = {
        "db/schema.ts": "Drizzle schema definition",
        "db/seed.ts": "Seed data function",
        "app/page.tsx": "Main page component",
        "drizzle/0000_zippy_changeling.sql": "SQL migration",
    }
    for path, desc in required.items():
        if path not in code_files:
            errors.append(f"Missing {path} ({desc})")

    data_routes = [
        k
        for k in code_files
        if k.startswith("app/api/") and "health" not in k and k.endswith("route.ts")
    ]
    if not data_routes:
        errors.append(
            "No data API routes (need at least one app/api/*/route.ts besides health)"
        )

    sql = code_files.get("drizzle/0000_zippy_changeling.sql", "")
    if (
        sql
        and sql.upper().count("CREATE TABLE") > 1
        and "--> statement-breakpoint" not in sql
    ):
        errors.append(
            "Multiple CREATE TABLE in migration without --> statement-breakpoint"
        )

    seed = code_files.get("db/seed.ts", "")
    if seed:
        if "seedDatabase" not in seed:
            errors.append("db/seed.ts missing seedDatabase export")
        if "from './db'" in seed or 'from "./db"' in seed:
            errors.append("db/seed.ts imports from './db' — must use './client'")
        if "insert" not in seed.lower() and ".values(" not in seed:
            errors.append("db/seed.ts doesn't appear to insert any data")

    for route_path in data_routes:
        content = code_files[route_path]
        if "seedDatabase" not in content and "seed" not in content.lower():
            errors.append(f"{route_path} missing lazy seedDatabase() call")

    schema_content = code_files.get("db/schema.ts", "")
    if schema_content:
        schema_exports = set(
            re.findall(r"export\s+(?:const|let|var)\s+(\w+)", schema_content)
        )
        safe_names = {
            "eq",
            "and",
            "or",
            "not",
            "sql",
            "desc",
            "asc",
            "lt",
            "gt",
            "gte",
            "lte",
            "ne",
            "inArray",
            "like",
            "ilike",
            "between",
            "isNull",
            "isNotNull",
            "exists",
            "notExists",
            "count",
            "getDb",
            "closeDb",
            "ensureSchema",
            "db",
            "AppDb",
            "seedDatabase",
        }
        for route_path in data_routes:
            content = code_files[route_path]
            schema_imports = re.findall(
                r"import\s*(?:type\s+)?\{([^}]+)\}\s*from\s*['\"](?:@/db(?:/schema)?(?:\.ts)?|[^'\"]*schema[^'\"]*)['\"]",
                content,
            )
            for imp_group in schema_imports:
                for imp in imp_group.split(","):
                    imp_name = imp.strip().split(" as ")[0].strip()
                    if (
                        imp_name
                        and imp_name not in schema_exports
                        and imp_name not in safe_names
                    ):
                        errors.append(
                            f"{route_path} imports '{imp_name}' from schema "
                            f"but only {schema_exports} are exported"
                        )

    layout = code_files.get("app/layout.tsx", "")
    if layout and ";</html>" in layout.replace(" ", ""):
        errors.append("app/layout.tsx has semicolon after </html>")

    for path, content in code_files.items():
        if path.startswith("app/api/") or path.startswith("db/"):
            continue
        is_client = '"use client"' in content or "'use client'" in content
        if is_client and re.search(
            r"""from\s+['"]@/db/(?:client|seed|index|bootstrap)['"]""", content
        ):
            errors.append(
                f"{path} is a 'use client' component but imports server-only db module"
            )
        if not path.startswith("app/api/") and re.search(
            r"""https?://(?:localhost|127\.0\.0\.1)(?::\d+)?/api/""", content
        ):
            errors.append(
                f"{path} uses absolute localhost URL for API calls "
                f"(will break behind HTTPS proxy)"
            )

    return errors


async def design_variant(
    client: anthropic.AsyncAnthropic,
    skill: SkillDefinition,
    model: str,
    variant_index: int = 1,
    total_variants: int = 1,
    prior_scenarios: list[str] | None = None,
) -> dict:
    """Use LLM to design a variant app specification for a skill."""
    user_prompt = (
        f"## Skill to Test\n\n"
        f"**Name:** {skill.name}\n\n"
        f"**Description:** {skill.description}\n\n"
        f"**Observed in {len(skill.session_ids)} failing sessions "
        f"across {len(skill.sim_sources)} simulator(s)**\n\n"
    )

    if total_variants > 1:
        user_prompt += (
            f"This is variant {variant_index} of {total_variants} for this skill. "
            f"Each variant MUST use a COMPLETELY DIFFERENT industry, product name, "
            f"layout type (vary between top_nav, sidebar, header_strip), and color "
            f"scheme. Pick an industry that hasn't been used yet.\n\n"
        )
        if prior_scenarios:
            user_prompt += (
                "Products already designed (DO NOT reuse these industries):\n"
            )
            for ps in prior_scenarios:
                user_prompt += f"- {ps}\n"
            user_prompt += "\n"

    user_prompt += (
        "Design a focused, polished snippet of a realistic product that "
        "isolates and tests this exact skill. Give it a real company name "
        "and domain-specific data. Remember: include app_chrome with the "
        "layout shell description."
    )

    async with client.messages.stream(
        model=model,
        max_tokens=16384,
        system=VARIANT_DESIGN_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = await stream.get_final_message()
    if response.stop_reason == "max_tokens":
        logger.warning("Design LLM hit max_tokens — output may be truncated")
    return _extract_json(response.content[0].text)


async def generate_variant_code(
    client: anthropic.AsyncAnthropic,
    spec: dict,
    model: str,
    fix_context: str | None = None,
) -> dict[str, str]:
    """Use LLM to generate complete code files from a variant spec."""
    user_prompt = (
        f"## App Specification\n\n```json\n{json.dumps(spec, indent=2)}\n```\n\n"
    )
    if fix_context:
        user_prompt += (
            f"## CRITICAL: Previous Code Had Errors — Fix Them\n\n"
            f"{fix_context}\n\n"
            f"Regenerate ALL code files, fixing every issue above. "
            f"Ensure db/seed.ts inserts 20+ realistic records and "
            f"every data API route calls seedDatabase().\n\n"
        )
    user_prompt += (
        "Generate the complete code for this application. Output a JSON object "
        "mapping file paths (relative to web/) to their complete file contents."
    )

    async with client.messages.stream(
        model=model,
        max_tokens=32768,
        system=VARIANT_CODE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = await stream.get_final_message()
    return _extract_json(response.content[0].text)


def _copy_sohan_template(template_source: Path, dest: Path) -> None:
    """Copy the sohan template to destination, skipping node_modules and caches."""
    ignore_dirs = {"node_modules", ".next", ".next-*", ".turbo", ".cache", ".runtime"}

    def ignore_fn(_directory: str, entries: list[str]) -> set[str]:
        return {e for e in entries if e in ignore_dirs}

    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(template_source, dest, ignore=ignore_fn)


def _post_process_code(code_files: dict[str, str]) -> dict[str, str]:
    """Fix common LLM code-generation mistakes before writing to disk."""
    import re as _re

    for path, content in list(code_files.items()):
        original_len = len(content)
        content = content.replace("\x00", "")
        content = _re.sub(r"[^\x09\x0a\x0d\x20-\x7e\u00a0-\uffff]", "", content)
        if len(content) != original_len:
            logger.warning(
                "Stripped %d binary chars from %s", original_len - len(content), path
            )
        code_files[path] = content

    for path, content in list(code_files.items()):
        if path.endswith("layout.tsx"):
            content = _re.sub(r"</html>\s*;", "</html>", content)

        if path == "db/seed.ts":
            if "from './db'" in content or 'from "./db"' in content:
                content = content.replace("from './db'", "from './client'")
                content = content.replace('from "./db"', 'from "./client"')
            if "import { db }" in content:
                content = content.replace(
                    "import { db } from './client'",
                    "import { getDb } from './client'",
                )
                if "const db = await getDb()" not in content:
                    content = content.replace(
                        "export async function seedDatabase() {",
                        "export async function seedDatabase() {\n  const db = await getDb();",
                    )

        if path.startswith("app/api/") and path.endswith("route.ts"):
            if "from '@/db/db'" in content or 'from "@/db/db"' in content:
                content = content.replace("from '@/db/db'", "from '@/db/client'")
                content = content.replace('from "@/db/db"', 'from "@/db/client"')
            if "import { db }" in content and "getDb" not in content:
                content = content.replace("import { db }", "import { getDb }")

        code_files[path] = content

    sql_key = "drizzle/0000_zippy_changeling.sql"
    if sql_key in code_files:
        sql = code_files[sql_key]
        if (
            sql.upper().count("CREATE TABLE") > 1
            and "--> statement-breakpoint" not in sql
        ):
            fixed_lines: list[str] = []
            for line in sql.split("\n"):
                if line.strip().upper().startswith("CREATE TABLE") and fixed_lines:
                    prev_nonempty = next(
                        (ln for ln in reversed(fixed_lines) if ln.strip()), ""
                    )
                    if prev_nonempty.strip().endswith(";"):
                        fixed_lines.append("--> statement-breakpoint")
                fixed_lines.append(line)
            code_files[sql_key] = "\n".join(fixed_lines)

    schema_content = code_files.get("db/schema.ts", "")
    if schema_content:
        schema_exports = set(
            re.findall(r"export\s+(?:const|let|var)\s+(\w+)", schema_content)
        )
        safe_names = {
            "eq",
            "and",
            "or",
            "not",
            "sql",
            "desc",
            "asc",
            "lt",
            "gt",
            "gte",
            "lte",
            "ne",
            "inArray",
            "like",
            "ilike",
            "between",
            "isNull",
            "isNotNull",
            "exists",
            "notExists",
            "count",
            "getDb",
            "closeDb",
            "ensureSchema",
            "db",
            "AppDb",
            "seedDatabase",
        }
        for route_path in list(code_files.keys()):
            if not (
                route_path.startswith("app/api/") and route_path.endswith("route.ts")
            ):
                continue
            content = code_files[route_path]
            schema_imports = re.findall(
                r"import\s*(?:type\s+)?\{([^}]+)\}\s*from\s*['\"](?:@/db(?:/schema)?(?:\.ts)?|[^'\"]*schema[^'\"]*)['\"]",
                content,
            )
            bad_import = False
            for imp_group in schema_imports:
                for imp in imp_group.split(","):
                    imp_name = imp.strip().split(" as ")[0].strip()
                    if (
                        imp_name
                        and imp_name not in schema_exports
                        and imp_name not in safe_names
                    ):
                        bad_import = True
                        logger.warning(
                            "Removing %s: imports '%s' not in schema exports %s",
                            route_path,
                            imp_name,
                            schema_exports,
                        )
                        break
                if bad_import:
                    break
            if bad_import:
                del code_files[route_path]

    # Fix absolute localhost URLs in client-side code — these break when
    # served behind https://<id>.sims.plato.so (mixed-content block).
    _LOCALHOST_RE = _re.compile(
        r"""https?://(?:localhost|127\.0\.0\.1)(?::\d+)?(/api/[^\s"'`)]*)""",
    )
    for path, content in list(code_files.items()):
        if not path.startswith("app/api/") and (
            path.endswith(".tsx") or path.endswith(".ts")
        ):
            new_content = _LOCALHOST_RE.sub(r"\1", content)
            if new_content != content:
                logger.warning("Replaced absolute localhost URL(s) in %s", path)
                content = new_content
                code_files[path] = content

    # Prevent client components from importing server-only db modules.
    _DB_IMPORT_RE = _re.compile(
        r"""^import\s+.*from\s+['"]@/db/(?:client|seed|index|bootstrap)['"];?\s*$""",
        _re.MULTILINE,
    )
    for path, content in list(code_files.items()):
        if path.startswith("app/api/") or path.startswith("db/"):
            continue
        is_client = '"use client"' in content or "'use client'" in content
        if is_client and _DB_IMPORT_RE.search(content):
            logger.warning(
                "Removing server-only db import(s) from client component %s", path
            )
            content = _DB_IMPORT_RE.sub("// [removed: server-only db import]", content)
            code_files[path] = content

    for path, content in list(code_files.items()):
        if path.endswith("page.tsx") or (
            path.endswith("layout.tsx") and path != "app/layout.tsx"
        ):
            if "export const dynamic" not in content:
                if '"use client"' in content or "'use client'" in content:
                    content = _re.sub(
                        r"""(["']use client["'];?\s*\n)""",
                        r'\1\nexport const dynamic = "force-dynamic";\n',
                        content,
                        count=1,
                    )
                else:
                    content = 'export const dynamic = "force-dynamic";\n\n' + content
                code_files[path] = content

    for route_path, content in list(code_files.items()):
        if not (
            route_path.startswith("app/api/")
            and route_path.endswith("route.ts")
            and "health" not in route_path
            and "seedDatabase" not in content
            and "getDb" in content
        ):
            continue
        if "import { seedDatabase }" not in content:
            content = "import { seedDatabase } from '@/db/seed';\n" + content
        if "let seeded" not in content and "export async function GET" in content:
            content = content.replace(
                "export async function GET",
                "\nlet seeded = false;\nexport async function GET",
            )
        if (
            "let seeded" in content
            and "await seedDatabase()" not in content
            and "const db = await getDb();" in content
        ):
            content = content.replace(
                "const db = await getDb();",
                "const db = await getDb();\n  if (!seeded) { await seedDatabase(); seeded = true; }",
            )
        code_files[route_path] = content

    return code_files


_TEMPLATE_PROTECTED = frozenset(
    {
        "package.json",
        "tsconfig.json",
        "next.config.ts",
        "next.config.js",
        "postcss.config.mjs",
        "tailwind.config.ts",
        "db/client.ts",
        "db/bootstrap.ts",
        "db/types.ts",
        "db/index.ts",
        "components/theme-provider.tsx",
        "app/providers.tsx",
        "app/globals.css",
    }
)


_LAYOUT_ROTATION = ["top_nav", "sidebar", "header_strip"]


def _build_layout_tsx(spec: dict) -> str | None:
    """Build a layout.tsx with branded chrome from the spec's app_chrome field.

    Uses inline styles for background colors (Tailwind arbitrary classes like
    bg-#hex are invalid and silently produce no CSS).  Deterministically rotates
    the layout type based on a hash of the product name so that variants within
    the same run get different layouts.
    """
    chrome = spec.get("app_chrome", {})
    if not chrome:
        return None

    product_name = chrome.get("product_name", spec.get("title", "App"))
    subtitle = chrome.get("subtitle", "")
    nav_items = chrome.get("nav_items", [])
    active_nav = chrome.get("active_nav", nav_items[0] if nav_items else "")

    # Force variety — ignore what the LLM picked, rotate deterministically
    layout_type = _LAYOUT_ROTATION[hash(product_name) % len(_LAYOUT_ROTATION)]

    vi = spec.get("visual_identity", {})
    primary = vi.get("primary_color", "#334155")
    if not primary.startswith("#"):
        primary = "#334155"

    pages = spec.get("pages", [])
    active_route = pages[0]["route"] if pages else "/"

    _COMMON_HEAD = f'''import type {{ Metadata }} from "next";
import {{ Inter }} from "next/font/google";
import "./globals.css";
import {{ Providers }} from "./providers";

const inter = Inter({{ subsets: ["latin"] }});
export const dynamic = "force-dynamic";

export const metadata: Metadata = {{
  title: "{product_name}",
  description: "{subtitle or product_name}",
}};

'''

    if layout_type == "sidebar":
        nav_lines = []
        for item in nav_items:
            if item == active_nav:
                nav_lines.append(
                    f'              <a href="{active_route}" className="block px-3 py-2 text-sm rounded font-medium" style={{{{ backgroundColor: "rgba(255,255,255,0.12)" }}}}>{item}</a>'
                )
            else:
                nav_lines.append(
                    f'              <span className="block px-3 py-2 text-sm rounded cursor-default" style={{{{ color: "rgba(255,255,255,0.4)" }}}}>{item}</span>'
                )
        nav_jsx = "\n".join(nav_lines)
        sub_jsx = (
            f'\n              <p className="text-xs mt-1" style={{{{ color: "rgba(255,255,255,0.5)" }}}}>{subtitle}</p>'
            if subtitle
            else ""
        )

        return (
            _COMMON_HEAD
            + f'''export default function RootLayout({{ children }}: Readonly<{{ children: React.ReactNode }}>) {{
  return (
    <html lang="en">
      <body className={{inter.className}} suppressHydrationWarning>
        <Providers>
          <div className="flex min-h-screen">
            <aside className="text-white w-56 flex-shrink-0 flex flex-col" style={{{{ backgroundColor: "{primary}" }}}}>
              <div className="p-4 pb-2">
                <h1 className="font-bold text-lg tracking-tight">{product_name}</h1>{sub_jsx}
              </div>
              <nav className="flex flex-col gap-0.5 px-2 mt-2 flex-1">
{nav_jsx}
              </nav>
              <div className="p-4 text-xs" style={{{{ color: "rgba(255,255,255,0.3)" }}}}>v1.0</div>
            </aside>
            <main className="flex-1 overflow-auto bg-gray-50 p-6">{"{children}"}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}}
'''
        )

    elif layout_type == "top_nav":
        tab_parts = []
        for item in nav_items:
            if item == active_nav:
                tab_parts.append(
                    f'<a href="{active_route}" className="px-3 py-1.5 text-sm rounded font-medium" style={{{{ backgroundColor: "rgba(255,255,255,0.15)" }}}}>{item}</a>'
                )
            else:
                tab_parts.append(
                    f'<span className="px-3 py-1.5 text-sm cursor-default" style={{{{ color: "rgba(255,255,255,0.4)" }}}}>{item}</span>'
                )
        tabs_jsx = "\n                  ".join(tab_parts)
        sub_jsx = (
            f'\n                <span className="text-sm" style={{{{ color: "rgba(255,255,255,0.6)" }}}}>{subtitle}</span>'
            if subtitle
            else ""
        )

        return (
            _COMMON_HEAD
            + f'''export default function RootLayout({{ children }}: Readonly<{{ children: React.ReactNode }}>) {{
  return (
    <html lang="en">
      <body className={{inter.className}} suppressHydrationWarning>
        <Providers>
          <header className="text-white shadow-md" style={{{{ backgroundColor: "{primary}" }}}}>
            <div className="flex items-center px-6 h-14 gap-4">
              <span className="font-bold text-lg tracking-tight">{product_name}</span>{sub_jsx}
              <nav className="flex items-center gap-1 ml-auto">
                  {tabs_jsx}
              </nav>
            </div>
          </header>
          <main className="bg-gray-50 min-h-[calc(100vh-3.5rem)] p-6">{"{children}"}</main>
        </Providers>
      </body>
    </html>
  );
}}
'''
        )

    else:
        # header_strip — simple branded bar, no nav tabs
        sub_jsx = (
            f' <span className="mx-2 opacity-30">|</span> <span className="text-sm font-normal" style={{{{ color: "rgba(255,255,255,0.7)" }}}}>{subtitle}</span>'
            if subtitle
            else ""
        )

        return (
            _COMMON_HEAD
            + f'''export default function RootLayout({{ children }}: Readonly<{{ children: React.ReactNode }}>) {{
  return (
    <html lang="en">
      <body className={{inter.className}} suppressHydrationWarning>
        <Providers>
          <header className="text-white" style={{{{ backgroundColor: "{primary}" }}}}>
            <div className="flex items-center px-6 h-12">
              <span className="font-semibold tracking-tight">{product_name}</span>{sub_jsx}
            </div>
          </header>
          <main className="bg-gray-50 min-h-[calc(100vh-3rem)] p-6">{"{children}"}</main>
        </Providers>
      </body>
    </html>
  );
}}
'''
        )


def apply_variant_code(
    variant_dir: Path,
    code_files: dict[str, str],
    spec: dict | None = None,
) -> list[str]:
    """Write generated code files into a variant's web/ directory.

    Returns list of file paths written (relative to variant_dir).
    """
    code_files = _post_process_code(code_files)
    web_dir = variant_dir / "web"
    written: list[str] = []

    generated_api_dirs: set[str] = set()
    for rel_path in code_files:
        if rel_path.startswith("app/api/"):
            parts = rel_path.split("/")
            if len(parts) > 2:
                generated_api_dirs.add(parts[2])

    template_api_dir = web_dir / "app" / "api"
    if template_api_dir.exists():
        generated_api_dirs.add("health")
        for entry in template_api_dir.iterdir():
            if entry.is_dir() and entry.name not in generated_api_dirs:
                shutil.rmtree(entry)
                logger.debug("Removed stale template API route: %s", entry.name)

    # Build branded layout.tsx from spec if available
    if spec:
        layout_content = _build_layout_tsx(spec)
        if layout_content:
            code_files["app/layout.tsx"] = layout_content
            logger.info("Built branded layout.tsx from spec app_chrome")

    for rel_path, content in code_files.items():
        if rel_path in _TEMPLATE_PROTECTED:
            logger.debug("Skipping protected template file: %s", rel_path)
            continue
        target = web_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        written.append(f"web/{rel_path}")
        logger.debug("Wrote %s", target)

    _cleanup_orphan_routes(web_dir)
    return written


def _cleanup_orphan_routes(web_dir: Path) -> None:
    """Remove API route files that import tables not exported by the schema."""
    schema_file = web_dir / "db" / "schema.ts"
    if not schema_file.exists():
        return
    schema_content = schema_file.read_text()
    schema_exports = set(
        re.findall(r"export\s+(?:const|let|var)\s+(\w+)", schema_content)
    )
    safe_names = {
        "eq",
        "and",
        "or",
        "not",
        "sql",
        "desc",
        "asc",
        "lt",
        "gt",
        "gte",
        "lte",
        "ne",
        "inArray",
        "like",
        "ilike",
        "between",
        "isNull",
        "isNotNull",
        "exists",
        "notExists",
        "count",
        "getDb",
        "closeDb",
        "ensureSchema",
        "db",
        "AppDb",
        "seedDatabase",
    }
    api_dir = web_dir / "app" / "api"
    if not api_dir.exists():
        return
    to_remove: list[Path] = []
    for route_file in api_dir.rglob("route.ts"):
        if "health" in str(route_file):
            continue
        content = route_file.read_text()
        all_imports = re.findall(
            r"import\s*(?:type\s+)?\{([^}]+)\}\s*from\s*['\"](?:@/db[^'\"]*|\.\.?/[^'\"]*db[^'\"]*)['\"]",
            content,
        )
        bad = False
        for imp_group in all_imports:
            for imp in imp_group.split(","):
                name = imp.strip().split(" as ")[0].strip()
                if name and name not in schema_exports and name not in safe_names:
                    rel = route_file.relative_to(web_dir)
                    logger.warning(
                        "Removing orphan route %s: imports '%s' not in schema",
                        rel,
                        name,
                    )
                    bad = True
                    break
            if bad:
                break
        if bad:
            to_remove.append(route_file)
    for route_file in to_remove:
        route_dir = route_file.parent
        route_file.unlink()
        while (
            route_dir != api_dir and route_dir.exists() and not any(route_dir.iterdir())
        ):
            route_dir.rmdir()
            route_dir = route_dir.parent


async def design_all_variants(
    skills: list[SkillDefinition],
    anthropic_api_key: str,
    model: str,
    concurrency: int = 4,
    specs_per_skill: int = 1,
) -> list[dict]:
    """Design variant specs for all skills (no code generation).

    When specs_per_skill > 1, generates multiple distinct scenarios per skill
    sequentially (so each variant knows about prior scenarios to avoid overlap),
    but different skills are processed concurrently.
    """
    client = anthropic.AsyncAnthropic(api_key=anthropic_api_key)
    semaphore = asyncio.Semaphore(concurrency)
    results: list[dict] = []

    async def design_skill_variants(skill: SkillDefinition) -> list[dict]:
        skill_results: list[dict] = []
        prior_scenarios: list[str] = []
        base_slug = _slugify(skill.short_name or skill.name)

        for vi in range(1, specs_per_skill + 1):
            slug = base_slug if specs_per_skill == 1 else f"{base_slug}-v{vi}"

            async with semaphore:
                max_retries = 2
                for attempt in range(max_retries + 1):
                    try:
                        spec = await design_variant(
                            client,
                            skill,
                            model,
                            variant_index=vi,
                            total_variants=specs_per_skill,
                            prior_scenarios=prior_scenarios if vi > 1 else None,
                        )
                        spec["skill_name"] = skill.name
                        spec["skill_description"] = skill.description
                        spec["slug"] = slug

                        scenario_desc = spec.get(
                            "scenario", spec.get("description", "")
                        )
                        if scenario_desc:
                            prior_scenarios.append(
                                f"{spec.get('title', slug)}: {scenario_desc}"
                            )
                        skill_results.append(spec)
                        logger.info("  Designed spec for %s", slug)
                        break
                    except Exception as e:
                        logger.warning(
                            "Design attempt %d/%d for '%s' (v%d) failed: %s",
                            attempt + 1,
                            max_retries + 1,
                            skill.name,
                            vi,
                            e,
                        )
                        if attempt >= max_retries:
                            logger.error("Giving up on design for '%s'", slug)

        return skill_results

    tasks = [design_skill_variants(skill) for skill in skills]
    for skill_results in await asyncio.gather(*tasks):
        results.extend(skill_results)

    total_expected = len(skills) * specs_per_skill
    logger.info("Designed %d/%d variant specs", len(results), total_expected)
    return results
