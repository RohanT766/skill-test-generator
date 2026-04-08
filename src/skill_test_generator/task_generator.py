"""Generate evaluation tasks for each skill-test variant."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import anthropic

from .prompts import TASK_GENERATION_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def _extract_json(text: str) -> dict:
    text = text.strip().removeprefix("\ufeff")

    fence = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n\s*```", text)
    if fence:
        text = fence.group(1).strip()

    if "{" not in text:
        raise ValueError("No JSON object found in LLM output")

    brace_start = text.index("{")
    depth = 0
    in_string = False
    escape_next = False
    end = brace_start
    for i, ch in enumerate(text[brace_start:], brace_start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end == brace_start:
        last_brace = text.rfind("}")
        if last_brace > brace_start:
            end = last_brace

    raw = text[brace_start : end + 1]

    def _clean_js(s: str) -> str:
        s = re.sub(r"//[^\n]*", "", s)
        s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
        s = re.sub(r",\s*([}\]])", r"\1", s)
        return s

    for candidate in (raw, _clean_js(raw)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise ValueError(
        f"Failed to parse JSON from LLM output (first 500 chars): {raw[:500]}"
    )


async def generate_tasks_for_variant(
    client: anthropic.AsyncAnthropic,
    spec: dict,
    model: str,
    output_tasks: int = 3,
    mutation_tasks: int = 3,
) -> list[dict]:
    """Generate evaluation tasks for a single variant spec."""
    user_prompt = (
        f"## Application\n\n"
        f"**Name:** {spec.get('title', spec.get('app_name', 'Unknown'))}\n\n"
        f"**Skill Tested:** {spec.get('skill_tested', spec.get('skill_name', ''))}\n\n"
        f"**Skill Description:** {spec.get('skill_description', spec.get('description', ''))}\n\n"
    )

    db_schema = spec.get("db_schema", "")
    if db_schema:
        user_prompt += f"## Database Schema\n\n```typescript\n{db_schema}\n```\n\n"

    seed_data = spec.get("seed_data", "")
    if seed_data:
        user_prompt += f"## Seed Data\n\n```typescript\n{seed_data}\n```\n\n"

    user_prompt += "## Pages\n\n"
    for page in spec.get("pages", []):
        user_prompt += (
            f"### {page.get('route', '/')}\n"
            f"{page.get('description', '')}\n"
            f"**UI Spec:** {page.get('ui_spec', '')}\n"
            f"**Components:** {', '.join(page.get('key_components', []))}\n\n"
        )

    api_routes = spec.get("api_routes", [])
    has_write_endpoint = False
    if api_routes:
        user_prompt += "## API Routes\n\n"
        for route in api_routes:
            methods = route.get("methods", [route.get("method", "GET")])
            if isinstance(methods, str):
                methods = [methods]
            methods_str = ", ".join(methods)
            user_prompt += f"- `{route.get('route', '')}` [{methods_str}]: {route.get('description', '')}\n"
            if any(m.upper() in ("PUT", "PATCH", "POST", "DELETE") for m in methods):
                has_write_endpoint = True
        user_prompt += "\n"

    edit_caps = spec.get("edit_capabilities", "")
    if edit_caps:
        user_prompt += f"## Edit Capabilities\n\n{edit_caps}\n\n"

    critical = spec.get("critical_ui_details", [])
    if critical:
        user_prompt += "## Critical UI Details\n\n"
        for detail in critical:
            user_prompt += f"- {detail}\n"
        user_prompt += "\n"

    total_tasks = output_tasks + mutation_tasks
    if has_write_endpoint and mutation_tasks > 0:
        user_prompt += (
            f"Generate exactly {total_tasks} tasks at varying difficulty "
            f"(at least one easy, one medium, one hard): "
            f"{output_tasks} output task(s) and {mutation_tasks} mutation task(s). "
            f"Use the seed data above to compute the EXACT correct "
            f"expected_output or expected_mutations for each task."
        )
    else:
        actual_total = output_tasks + mutation_tasks
        user_prompt += (
            f"Generate exactly {actual_total} output tasks at varying difficulty "
            f"(at least one easy, one medium, one hard). "
            f"{'This app has NO write API endpoints, so generate ' if not has_write_endpoint else ''}"
            f"ONLY output tasks. Use the seed data above to compute the EXACT "
            f"correct expected_output for each task."
        )

    async with client.messages.stream(
        model=model,
        max_tokens=8192,
        system=TASK_GENERATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = await stream.get_final_message()
    parsed = _extract_json(response.content[0].text)
    return parsed.get("tasks", [])


async def generate_all_tasks(
    variant_specs: list[dict],
    output_dir: Path,
    anthropic_api_key: str,
    model: str,
    concurrency: int = 4,
    output_tasks: int = 3,
    mutation_tasks: int = 3,
) -> dict[str, list[dict]]:
    """Generate tasks for all variants. Returns {slug: [tasks]}."""
    client = anthropic.AsyncAnthropic(api_key=anthropic_api_key)
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, list[dict]] = {}

    async def gen_one(spec: dict) -> tuple[str, list[dict]]:
        slug = spec.get("slug", spec.get("_generation", {}).get("slug", "unknown"))
        tasks_path = output_dir / slug / "tasks.json"

        if tasks_path.exists():
            logger.info("Tasks for '%s' already exist, loading from disk", slug)
            return slug, json.loads(tasks_path.read_text()).get("tasks", [])

        async with semaphore:
            try:
                logger.info("Generating tasks for variant: %s", slug)
                tasks = await generate_tasks_for_variant(
                    client,
                    spec,
                    model,
                    output_tasks=output_tasks,
                    mutation_tasks=mutation_tasks,
                )

                tasks_path.parent.mkdir(parents=True, exist_ok=True)
                tasks_path.write_text(json.dumps({"tasks": tasks}, indent=2))

                logger.info("Generated %d tasks for '%s'", len(tasks), slug)
                return slug, tasks
            except Exception as e:
                logger.error("Failed to generate tasks for '%s': %s", slug, e)
                return slug, []

    task_coros = [gen_one(spec) for spec in variant_specs]
    for slug, tasks in await asyncio.gather(*task_coros):
        results[slug] = tasks

    total = sum(len(t) for t in results.values())
    logger.info("Generated %d total tasks across %d variants", total, len(results))
    return results


def _build_v2_scoring_config(task: dict, sim_name: str) -> dict | None:
    """Build a V2ScoringConfig dict from a generated task's scoring fields."""
    scoring_type = task.get("scoring_type", "output")

    if scoring_type == "output":
        output_schema = task.get("output_schema")
        expected_output = task.get("expected_output")
        if not output_schema or not expected_output:
            return None

        return {
            "output_config": {
                "type": "json_schema",
                "scoring_schema": expected_output,
            },
        }

    if scoring_type == "mutations":
        mutations = []
        for mut in task.get("expected_mutations", []):
            mutations.append(
                {
                    "table": mut.get("table", ""),
                    "action": mut.get("action", "update"),
                    "row_filter": mut.get("row_filter", {}),
                    "values": mut.get("values", {}),
                }
            )

        if not mutations:
            return None

        return {
            "mutation_configs": {
                sim_name: {
                    "scoring_config": {
                        "type": "state_mutation_match",
                        "mutations": mutations,
                    },
                },
            },
        }

    return None


def build_plato_task_configs(
    variant_specs: list[dict],
    all_tasks: dict[str, list[dict]],
    sim_name_prefix: str,
) -> list[dict]:
    """Convert generated tasks into Plato-compatible task config format.

    Each task config can be used to create a Plato test case via the API,
    including V2ScoringConfig for automated evaluation.
    """
    configs: list[dict] = []

    for spec in variant_specs:
        slug = spec.get("slug", spec.get("_generation", {}).get("slug", "unknown"))
        sim_name = f"{sim_name_prefix}-{slug}"
        tasks = all_tasks.get(slug, [])

        for task in tasks:
            scoring_type = task.get("scoring_type", "output")
            v2_scoring_config = _build_v2_scoring_config(task, sim_name)

            config: dict = {
                "sim": sim_name,
                "task_name": f"{slug}--{task.get('name', 'unnamed')}",
                "title": task.get("title", ""),
                "difficulty": task.get("difficulty", "medium"),
                "instruction": task.get("instruction", ""),
                "start_url": task.get("start_url", "/"),
                "scoring_type": scoring_type,
                "metadata": {
                    "skill_tested": spec.get(
                        "skill_tested", spec.get("skill_name", "")
                    ),
                    "skill_description": spec.get(
                        "skill_description", spec.get("description", "")
                    ),
                    "variant_slug": slug,
                    "scoring_hint": task.get("scoring_hint", ""),
                },
            }

            if v2_scoring_config:
                config["v2_scoring_config"] = v2_scoring_config

            if scoring_type == "output":
                config["output_schema"] = task.get("output_schema")
                config["expected_output"] = task.get("expected_output")

            configs.append(config)

    return configs
