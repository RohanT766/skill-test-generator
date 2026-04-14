"""Generate web app variants from skill definitions using LLM design + code generation."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import shutil
from pathlib import Path

import anthropic

from .config import SkillDefinition
from .json_utils import extract_json as _extract_json
from .prompts import VARIANT_CODE_SYSTEM_PROMPT, VARIANT_DESIGN_SYSTEM_PROMPT
from .skill_ingestion import _slugify

logger = logging.getLogger(__name__)


def _resolve_reference_screenshots_dir() -> Path | None:
    """Find the reference-screenshots directory (bundled in container or local dev)."""
    bundled = Path("/world/templates/reference-screenshots")
    if bundled.is_dir():
        return bundled

    local = Path(__file__).resolve().parents[3] / "templates" / "reference-screenshots"
    if local.is_dir():
        return local

    return None


def _load_reference_manifest() -> list[dict]:
    """Load the reference screenshots manifest.json."""
    ref_dir = _resolve_reference_screenshots_dir()
    if not ref_dir:
        return []
    manifest_path = ref_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        return json.loads(manifest_path.read_text())
    except Exception as e:
        logger.warning("Failed to load reference manifest: %s", e)
        return []


_FILTER_SYSTEM_PROMPT = """\
You select which reference screenshot categories are relevant for a given \
skill definition. You will be given a skill name and description, plus the \
full list of available content_type and industry labels.

Pick 1-4 content_type values that would make good visual inspiration for an \
app testing this skill. Choose types where the skill would naturally occur — \
e.g. a pagination skill fits "data table", "product grid", "listing page"; \
a truncation skill fits "data table", "detail page".

Optionally pick 0-2 industry values if the skill strongly implies a domain. \
If any industry could work, return an empty array for industries.

Respond with a JSON object (no markdown fencing):
{
  "content_types": ["type1", "type2"],
  "industries": []
}\
"""


async def select_reference_screenshot(
    client: anthropic.AsyncAnthropic,
    model: str,
    skill_name: str = "",
    skill_description: str = "",
) -> tuple[dict, bytes] | None:
    """Pick a reference screenshot using an LLM to choose relevant filters.

    A small LLM call selects which content_types (and optionally industries)
    are relevant for the skill. Then we filter the manifest by union of
    content_types and randomly pick one image from the filtered set.
    Falls back to the full set if no match.
    """
    manifest = _load_reference_manifest()
    if not manifest:
        return None

    ref_dir = _resolve_reference_screenshots_dir()
    if not ref_dir:
        return None

    content_types = sorted(set(e.get("content_type", "") for e in manifest))
    industries = sorted(set(e.get("industry", "") for e in manifest))

    filter_prompt = (
        f"## Skill\n"
        f"**Name:** {skill_name}\n"
        f"**Description:** {skill_description}\n\n"
        f"## Available content_types\n{json.dumps(content_types)}\n\n"
        f"## Available industries\n{json.dumps(industries)}\n"
    )

    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=256,
            system=_FILTER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": filter_prompt}],
        )
        filters = _extract_json(resp.content[0].text)
        wanted_types = {t.lower() for t in filters.get("content_types", [])}
        wanted_industries = {i.lower() for i in filters.get("industries", [])}
        logger.info(
            "LLM filter for '%s': types=%s, industries=%s",
            skill_name,
            wanted_types,
            wanted_industries,
        )
    except Exception as e:
        logger.warning("LLM filter call failed, using full manifest: %s", e)
        wanted_types = set()
        wanted_industries = set()

    candidates = manifest
    if wanted_types:
        filtered = [
            e for e in manifest
            if e.get("content_type", "").lower() in wanted_types
        ]
        if wanted_industries and filtered:
            narrowed = [
                e for e in filtered
                if e.get("industry", "").lower() in wanted_industries
            ]
            if narrowed:
                filtered = narrowed
        if filtered:
            candidates = filtered
            logger.info(
                "Filtered screenshots to %d candidates",
                len(filtered),
            )

    random.shuffle(candidates)
    for entry in candidates:
        img_path = ref_dir / entry["filename"]
        if img_path.exists():
            return entry, img_path.read_bytes()

    return None


def _validate_code_files(code_files: dict[str, str]) -> list[str]:
    """Check generated code for structural issues. Returns list of errors."""
    errors: list[str] = []
    required = {
        "db/schema.ts": "Drizzle schema definition",
        "db/seed.ts": "Seed data function",
        "app/layout.tsx": "Root layout with branded chrome",
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
    """Use LLM to design a variant app specification for a skill.

    Selects a reference screenshot based on the skill, passes it to the
    design LLM as visual inspiration, and stores the reference metadata
    in the returned spec under `_reference_screenshot`.
    """
    ref = await select_reference_screenshot(
        client=client,
        model=model,
        skill_name=skill.name,
        skill_description=skill.description,
    )

    content_blocks: list[dict] = []

    if ref:
        ref_meta, ref_bytes = ref
        ext = ref_meta.get("filename", "ref.png").rsplit(".", 1)[-1]
        media_type = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
        content_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(ref_bytes).decode(),
            },
        })
        ref_ct = ref_meta.get("content_type", "")
        ref_patterns = ", ".join(ref_meta.get("ui_patterns", []))
        ref_desc = ref_meta.get("description", "")
        ref_industry = ref_meta.get("industry", "")
        content_blocks.append({
            "type": "text",
            "text": (
                "## Visual Reference (MATCH THIS CLOSELY)\n\n"
                "The image above is a screenshot from a real website. Your app must "
                "closely match its visual design — replicate the layout structure "
                "(sidebar vs top nav, positioning, proportions), color scheme, "
                "typography density, and spacing. The screenshot's industry is a "
                "strong hint — use the same or a closely related industry. "
                "Generate your own product name, data values, and entities — do NOT "
                "copy specific text — but the visual shell must look like it belongs "
                "to the same product family as the screenshot.\n\n"
                f"**Page type:** {ref_ct}\n"
                f"**Industry:** {ref_industry}\n"
                f"**UI patterns:** {ref_patterns}\n"
                f"**Description:** {ref_desc}\n\n"
            ),
        })
        logger.info(
            "Design ref for '%s': %s (%s)",
            skill.name,
            ref_meta.get("filename"),
            ref_ct,
        )

    user_text = (
        f"## Skill to Test\n\n"
        f"**Name:** {skill.name}\n\n"
        f"**Description:** {skill.description}\n\n"
        f"**Observed in {len(skill.session_ids)} failing sessions "
        f"across {len(skill.sim_sources)} simulator(s)**\n\n"
    )

    if total_variants > 1:
        user_text += (
            f"This is variant {variant_index} of {total_variants} for this skill. "
            f"Each variant MUST use a COMPLETELY DIFFERENT industry, product name, "
            f"layout style, and color scheme. Pick an industry that hasn't been used yet.\n\n"
        )
        if prior_scenarios:
            user_text += (
                "Products already designed (DO NOT reuse these industries):\n"
            )
            for ps in prior_scenarios:
                user_text += f"- {ps}\n"
            user_text += "\n"

    user_text += (
        "Design a focused, polished snippet of a realistic product that "
        "isolates and tests this exact skill. Give it a real company name "
        "and domain-specific data. Remember: include app_chrome with the "
        "layout shell description."
    )
    content_blocks.append({"type": "text", "text": user_text})

    async with client.messages.stream(
        model=model,
        max_tokens=16384,
        system=VARIANT_DESIGN_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content_blocks}],
    ) as stream:
        response = await stream.get_final_message()
    if response.stop_reason == "max_tokens":
        logger.warning("Design LLM hit max_tokens — output may be truncated")

    spec = _extract_json(response.content[0].text)

    if ref:
        spec["_reference_screenshot"] = ref_meta.get("filename", "")

    return spec


async def generate_variant_code(
    client: anthropic.AsyncAnthropic,
    spec: dict,
    model: str,
    fix_context: str | None = None,
    reference_screenshot: tuple[dict, bytes] | None = None,
) -> dict[str, str]:
    """Use LLM to generate complete code files from a variant spec.

    If reference_screenshot is provided, the image is sent alongside the spec
    as the visual target for layout, colors, and styling.
    """
    content_blocks: list[dict] = []

    if reference_screenshot:
        ref_meta, ref_bytes = reference_screenshot
        ext = ref_meta.get("filename", "ref.png").rsplit(".", 1)[-1]
        media_type = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
        content_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(ref_bytes).decode(),
            },
        })
        ref_desc = ref_meta.get("description", "")
        ref_ct = ref_meta.get("content_type", "")
        ref_industry = ref_meta.get("industry", "")
        ref_patterns = ", ".join(ref_meta.get("ui_patterns", []))
        content_blocks.append({
            "type": "text",
            "text": (
                "## Visual Reference (MATCH THIS CLOSELY)\n\n"
                "The image above is the same reference screenshot used during the "
                "design phase. Your generated code must closely replicate its "
                "visual design — layout structure, color scheme, nav pattern, "
                "typography density, and spacing. The spec defines the industry, "
                "data, and skill-specific UI — follow the spec for those. The "
                "screenshot governs the visual shell. Do NOT copy specific text "
                "or data values from the screenshot.\n\n"
                f"**Reference info:** {ref_ct} | {ref_industry}\n"
                f"**UI patterns:** {ref_patterns}\n"
                f"**Description:** {ref_desc}\n\n"
            ),
        })

    user_text = f"## App Specification\n\n```json\n{json.dumps(spec, indent=2)}\n```\n\n"
    if fix_context:
        user_text += (
            f"## CRITICAL: Previous Code Had Errors — Fix Them\n\n"
            f"{fix_context}\n\n"
            f"Regenerate ALL code files, fixing every issue above. "
            f"Ensure db/seed.ts inserts 20+ realistic records and "
            f"every data API route calls seedDatabase().\n\n"
        )
    user_text += (
        "Generate the complete code for this application. Output a JSON object "
        "mapping file paths (relative to web/) to their complete file contents."
    )
    content_blocks.append({"type": "text", "text": user_text})

    async with client.messages.stream(
        model=model,
        max_tokens=32768,
        system=VARIANT_CODE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content_blocks}],
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
