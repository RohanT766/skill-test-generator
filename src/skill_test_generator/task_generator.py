"""Generate evaluation tasks for each skill-test variant."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import anthropic

from .json_utils import extract_json as _extract_json

logger = logging.getLogger(__name__)

_OUTPUT_ONLY_SYSTEM_PROMPT = """\
You are an expert test designer for AI web agent evaluation. You are given \
a web application spec, its database schema, its seed data, and the SKILL \
the app is designed to test. Your job is to write retrieval TASKS — \
questions an AI agent must answer by reading data from the application.

CRITICAL RULES:

1. SINGLE DETERMINISTIC ANSWER. Every task MUST have exactly ONE correct \
answer with NO ambiguity. Compute the answer from the seed data.
   - NEVER ask the agent to "list" or enumerate multiple items.
   - NEVER ask two separate questions in one task.

2. RETRIEVAL ONLY. Tasks ask the agent to FIND and REPORT a value. \
Never ask the agent to update, create, delete, reassign, or modify \
any data. The agent only reads.

3. OBJECTIVE ONLY, NEVER METHOD. State the desired outcome. Never mention \
UI elements, forms, buttons, dropdowns, pages, dialogs, or workflow steps.

4. NEVER HINT AT THE SKILL. Do not mention pagination, truncation, \
dropdowns, scrolling, expanding, hidden content, tabs, collapsing, or \
any UI mechanism. The agent must discover these on its own.

5. EVERY TASK REQUIRES THE SKILL. An agent that lacks this skill MUST \
fail or get the wrong answer. The correct answer should only be reachable \
by exercising the tested skill. Design tasks where the naive approach \
(e.g. only looking at visible data) gives a WRONG answer, but applying \
the skill gives the RIGHT answer.

Respond with a JSON object (no markdown fencing):
- "tasks": array of task objects, each with:
  - "name": short kebab-case identifier
  - "title": human-readable title
  - "instruction": 1-3 sentence retrieval question.
  - "start_url": URL path where the agent starts (e.g. "/")
  - "scoring_type": always "output"
  - "output_schema": JSON Schema for the answer. Must request exactly ONE value.
  - "expected_output": the single correct answer matching output_schema, \
computed from the seed data.
  - "scoring_hint": what to verify
  - "skill_required": why this task needs the tested skill (internal)\
"""

_MUTATION_ONLY_SYSTEM_PROMPT = """\
You are an expert test designer for AI web agent evaluation. You are given \
a web application spec, its database schema, its seed data, and the SKILL \
the app is designed to test. Your job is to write mutation TASKS — actions \
an AI agent must perform to change data in the application.

CRITICAL RULES:

1. SINGLE DETERMINISTIC OUTCOME. Every task MUST have exactly ONE correct \
set of database mutations with NO ambiguity.
   - NEVER ask two separate mutations in one task.

2. OBJECTIVE ONLY, NEVER METHOD. State the desired outcome. Never mention \
UI elements, forms, buttons, dropdowns, pages, dialogs, or workflow steps.
   - The agent must determine WHICH record and WHAT to change by reasoning.

3. NEVER HINT AT THE SKILL. Do not mention pagination, truncation, \
dropdowns, scrolling, expanding, hidden content, tabs, collapsing, or \
any UI mechanism. The agent must discover these on its own.

4. EVERY TASK REQUIRES THE SKILL. An agent that lacks this skill MUST \
fail or get the wrong answer.

5. ONLY FEASIBLE TASKS. Check the API routes — if there are no write \
endpoints for a resource, do NOT generate mutation tasks for it.

Respond with a JSON object (no markdown fencing):
- "tasks": array of task objects, each with:
  - "name": short kebab-case identifier
  - "title": human-readable title
  - "instruction": 1-3 sentence objective.
  - "start_url": URL path where the agent starts (e.g. "/")
  - "scoring_type": always "mutations"
  - "expected_mutations": array of DB changes:
    - "table": table name
    - "action": "insert" | "update" | "delete"
    - "row_filter": identifies the row (e.g. {"id": 5})
    - "values": expected field values after the change
  - "scoring_hint": what to verify
  - "skill_required": why this task needs the tested skill (internal)\
"""

_MIXED_SYSTEM_PROMPT = """\
You are an expert test designer for AI web agent evaluation. You are given \
a web application spec, its database schema, its seed data, and the SKILL \
the app is designed to test. Your job is to write TASKS — goals an AI \
agent must achieve in the application.

CRITICAL RULES:

1. SINGLE DETERMINISTIC ANSWER. Every task MUST have exactly ONE correct \
answer with NO ambiguity. Compute the answer from the seed data.
   - NEVER ask the agent to "list" or enumerate multiple items.
   - NEVER ask two separate questions in one task.

2. OBJECTIVE ONLY, NEVER METHOD. State the desired outcome. Never mention \
UI elements, forms, buttons, dropdowns, pages, dialogs, or workflow steps.

3. NEVER HINT AT THE SKILL. Do not mention pagination, truncation, \
dropdowns, scrolling, expanding, hidden content, tabs, collapsing, or \
any UI mechanism. The agent must discover these on its own.

4. EVERY TASK REQUIRES THE SKILL. An agent that lacks this skill MUST \
fail or get the wrong answer.

5. ONLY FEASIBLE TASKS. Check the API routes — if there are no write \
endpoints for a resource, do NOT generate mutation tasks for it.

Respond with a JSON object (no markdown fencing):
- "tasks": array of task objects, each with:
  - "name": short kebab-case identifier
  - "title": human-readable title
  - "instruction": 1-3 sentence objective.
  - "start_url": URL path where the agent starts (e.g. "/")
  - "scoring_type": "output" or "mutations"
  - "output_schema": (REQUIRED for output tasks) JSON Schema for the answer.
  - "expected_output": (REQUIRED for output tasks) the correct answer.
  - "expected_mutations": (REQUIRED for mutation tasks) array of DB changes:
    - "table": table name
    - "action": "insert" | "update" | "delete"
    - "row_filter": identifies the row (e.g. {"id": 5})
    - "values": expected field values after the change
  - "scoring_hint": what to verify
  - "skill_required": why this task needs the tested skill (internal)\
"""


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
    if api_routes:
        user_prompt += "## API Routes\n\n"
        for route in api_routes:
            methods = route.get("methods", [route.get("method", "GET")])
            if isinstance(methods, str):
                methods = [methods]
            methods_str = ", ".join(methods)
            user_prompt += f"- `{route.get('route', '')}` [{methods_str}]: {route.get('description', '')}\n"
        user_prompt += "\n"

    edit_caps = spec.get("edit_capabilities", "")
    if edit_caps and mutation_tasks > 0:
        user_prompt += f"## Edit Capabilities\n\n{edit_caps}\n\n"

    critical = spec.get("critical_ui_details", [])
    if critical:
        user_prompt += "## Critical UI Details\n\n"
        for detail in critical:
            user_prompt += f"- {detail}\n"
        user_prompt += "\n"

    total_tasks = output_tasks + mutation_tasks
    if output_tasks > 0 and mutation_tasks > 0:
        system_prompt = _MIXED_SYSTEM_PROMPT
        user_prompt += (
            f"Generate exactly {total_tasks} tasks: "
            f"{output_tasks} output task(s) and {mutation_tasks} mutation task(s). "
            f"Use the seed data above to compute the EXACT correct "
            f"expected_output or expected_mutations for each task."
        )
    elif mutation_tasks > 0:
        system_prompt = _MUTATION_ONLY_SYSTEM_PROMPT
        user_prompt += (
            f"Generate exactly {mutation_tasks} mutation tasks. "
            f"Use the seed data above to compute the EXACT correct "
            f"expected_mutations for each task."
        )
    else:
        system_prompt = _OUTPUT_ONLY_SYSTEM_PROMPT
        user_prompt += (
            f"Generate exactly {output_tasks} retrieval tasks. "
            f"Use the seed data above to compute the EXACT correct "
            f"expected_output for each task."
        )

    async with client.messages.stream(
        model=model,
        max_tokens=8192,
        system=system_prompt,
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
                    "tablename": mut.get("table", ""),
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
