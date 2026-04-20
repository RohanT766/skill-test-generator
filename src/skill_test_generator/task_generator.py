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
You design retrieval tasks that test whether an AI web agent can exercise \
a specific SKILL. The agent must read data from the application and return \
an answer.

THINK FIRST — before writing any task, reason through these questions:
1. What does a naive agent (one lacking this skill) get wrong?
2. What is the minimum output that, if returned correctly, proves the \
skill was used?
3. What is the minimum information the agent needs to attempt the task?
4. Is there ANY alternative path to the answer that bypasses the skill \
entirely (e.g. the value is also visible on the default view, or reachable \
via a different page/route that doesn't require the skill)? If yes, pick a \
different data point.
5. Would a naive agent confidently return a WRONG answer for this task? \
The best tasks are ones where skipping the skill gives a plausible but \
incorrect result — not ones where the agent would simply fail or be unsure.

THEN write each task following these principles:
- Pick data points that MAXIMALLY exploit the skill gap. Choose records \
where the naive shortcut gives a confident wrong answer. If the data \
contains decoys or near-matches designed to trip up agents, target those \
specific records.
- Ask for the minimum number of values needed to prove the skill was used. \
Usually this is ONE value. Only ask for multiple values when the skill \
itself is about extracting or correlating multiple pieces of information.
- The instruction should be the shortest unambiguous sentence that requires \
the skill. Include only what the agent strictly needs to identify the task.
- Do not add clarifying details, parenthetical hints, or extra context \
beyond what is necessary to specify the question.
- Do not mention, describe, or allude to the skill, the UI mechanism, or \
the challenge the agent will face — e.g. never mention pagination, \
scrolling, hidden content, tabs, dropdowns, expanding sections, truncation, \
or similar mechanisms. The agent must discover these on its own.
- State what to find, not how to find it. Never reference UI elements, \
navigation, or workflow steps.
- The correct answer must only be reachable by exercising the skill. A \
naive approach (e.g. looking only at initially visible data) must give a \
WRONG answer. Verify there is no workaround — no other page, shortcut, \
or surface in the app that leaks the answer without the skill.
- Compute the expected answer from the seed data AND the live API data \
provided. If both are available and they disagree, use the live API data.
- CRITICAL — expected_output values must match how the data appears in the \
UI, not how it is stored in the database. If the UI displays "1,234.56" or \
"$1,234.56", the expected value is the string "1,234.56" or "$1,234.56". \
If a number is displayed with decimals (e.g. 99.00), use a float (99.0), \
not an int (99). If the UI shows a formatted string, use that exact string. \
When in doubt, prefer strings over numbers in expected_output.

Respond with a JSON object (no markdown fencing):
- "tasks": array of task objects, each with:
  - "name": short kebab-case identifier
  - "title": human-readable title
  - "instruction": 1-2 sentence retrieval question
  - "start_url": URL path where the agent starts (e.g. "/")
  - "scoring_type": always "output"
  - "output_schema": JSON Schema for the answer (minimal keys needed)
  - "expected_output": the correct answer matching output_schema
  - "hint": a strategic nudge that helps a skilled agent solve the task \
WITHOUT giving away the answer. Describe the navigation strategy, data \
layout, and traps/decoys the sim contains, but NEVER mention the actual \
expected answer value, entity name, number, or any specific data that \
appears in expected_output. The hint must be useful but not a spoiler.
  - "scoring_hint": what to verify (internal, not shown to agent)
  - "skill_required": why this task needs the tested skill (internal)\
"""

_MUTATION_ONLY_SYSTEM_PROMPT = """\
You design mutation tasks that test whether an AI web agent can exercise \
a specific SKILL. The agent must change data in the application.

THINK FIRST — before writing any task, reason through these questions:
1. What does a naive agent (one lacking this skill) get wrong?
2. What single mutation, if performed correctly, proves the skill was used?
3. What is the minimum information the agent needs to identify the record \
and the change?
4. Is there ANY alternative way to perform this mutation that bypasses the \
skill entirely (e.g. the record is editable from a different view that \
doesn't require the skill)? If yes, pick a different record or mutation.
5. Would a naive agent confidently mutate the WRONG record? The best tasks \
target records where the skill gap causes the agent to act on a decoy \
instead of the correct target.

THEN write each task following these principles:
- Pick records that MAXIMALLY exploit the skill gap. Target records where \
a naive agent would confidently find and modify the wrong one.
- If the data contains near-matches or decoys, target those specific records.
- Each task has exactly one deterministic set of mutations.
- The instruction should be the shortest unambiguous sentence that requires \
the skill. Include only what the agent strictly needs to identify the task.
- Do not add clarifying details, parenthetical hints, or extra context \
beyond what is necessary.
- Do not mention, describe, or allude to the skill, the UI mechanism, or \
the challenge — e.g. never mention pagination, scrolling, hidden content, \
tabs, dropdowns, expanding sections, truncation, or similar mechanisms. \
The agent must discover these on its own.
- State the desired outcome, not the method. Never reference UI elements, \
navigation, or workflow steps.
- The target record must only be reachable by exercising the skill. Verify \
there is no workaround — no other page, shortcut, or surface in the app \
that exposes the record without the skill.
- Only generate mutation tasks for resources that have write API endpoints.

Respond with a JSON object (no markdown fencing):
- "tasks": array of task objects, each with:
  - "name": short kebab-case identifier
  - "title": human-readable title
  - "instruction": 1-2 sentence objective
  - "start_url": URL path where the agent starts (e.g. "/")
  - "scoring_type": always "mutations"
  - "expected_mutations": array of DB changes:
    - "table": table name
    - "action": "insert" | "update" | "delete"
    - "row_filter": identifies the row (e.g. {"id": 5})
    - "values": expected field values after the change
  - "hint": a strategic nudge that helps a skilled agent solve the task \
WITHOUT giving away the answer. Describe the navigation strategy, data \
layout, and traps/decoys the sim contains, but NEVER mention the actual \
expected answer value, entity name, number, or any specific data that \
appears in expected_output or expected_mutations. The hint must be \
useful but not a spoiler.
  - "scoring_hint": what to verify (internal, not shown to agent)
  - "skill_required": why this task needs the tested skill (internal)\
"""

_MIXED_SYSTEM_PROMPT = """\
You design tasks that test whether an AI web agent can exercise a specific \
SKILL. Tasks are either retrieval (return a value) or mutation (change data).

THINK FIRST — before writing any task, reason through these questions:
1. What does a naive agent (one lacking this skill) get wrong?
2. For output tasks: what is the minimum output that proves the skill was used?
3. For mutation tasks: what single mutation proves the skill was used?
4. What is the minimum information the agent needs to attempt the task?
5. Is there ANY alternative path to the answer or record that bypasses the \
skill entirely (e.g. visible on the default view, reachable via a different \
page that doesn't require the skill)? If yes, pick a different target.
6. Would a naive agent confidently return a WRONG answer or mutate the WRONG \
record? The best tasks are ones where skipping the skill gives a plausible \
but incorrect result.

THEN write each task following these principles:
- Pick data points that MAXIMALLY exploit the skill gap. Choose records \
where the naive shortcut gives a confident wrong answer. If the data \
contains decoys or near-matches designed to trip up agents, target those.
- Output tasks ask for the minimum number of values needed to prove the \
skill was used. Usually this is ONE value. Only ask for multiple values \
when the skill itself is about extracting or correlating multiple pieces \
of information.
- Mutation tasks have exactly one deterministic set of mutations.
- The instruction should be the shortest unambiguous sentence that requires \
the skill. Include only what the agent strictly needs to identify the task.
- Do not add clarifying details, parenthetical hints, or extra context \
beyond what is necessary.
- Do not mention, describe, or allude to the skill, the UI mechanism, or \
the challenge — e.g. never mention pagination, scrolling, hidden content, \
tabs, dropdowns, expanding sections, truncation, or similar mechanisms. \
The agent must discover these on its own.
- State what to find or achieve, not how. Never reference UI elements, \
navigation, or workflow steps.
- The correct answer must only be reachable by exercising the skill. Verify \
there is no workaround — no other page, shortcut, or surface in the app \
that leaks the answer without the skill.
- Only generate mutation tasks for resources that have write API endpoints.
- Compute expected answers from the seed data AND the live API data \
provided. If both are available and they disagree, use the live API data.
- CRITICAL — expected_output values must match how the data appears in the \
UI, not how it is stored in the database. If the UI displays "1,234.56" or \
"$1,234.56", the expected value is the string "1,234.56" or "$1,234.56". \
If a number is displayed with decimals (e.g. 99.00), use a float (99.0), \
not an int (99). If the UI shows a formatted string, use that exact string. \
When in doubt, prefer strings over numbers in expected_output.

Respond with a JSON object (no markdown fencing):
- "tasks": array of task objects, each with:
  - "name": short kebab-case identifier
  - "title": human-readable title
  - "instruction": 1-2 sentence objective
  - "start_url": URL path where the agent starts (e.g. "/")
  - "scoring_type": "output" or "mutations"
  - "output_schema": (REQUIRED for output tasks) JSON Schema (minimal keys)
  - "expected_output": (REQUIRED for output tasks) the correct answer
  - "expected_mutations": (REQUIRED for mutation tasks) array of DB changes:
    - "table": table name
    - "action": "insert" | "update" | "delete"
    - "row_filter": identifies the row (e.g. {"id": 5})
    - "values": expected field values after the change
  - "hint": a strategic nudge that helps a skilled agent solve the task \
WITHOUT giving away the answer. Describe the navigation strategy, data \
layout, and traps/decoys the sim contains, but NEVER mention the actual \
expected answer value, entity name, number, or any specific data that \
appears in expected_output or expected_mutations. The hint must be \
useful but not a spoiler.
  - "scoring_hint": what to verify (internal, not shown to agent)
  - "skill_required": why this task needs the tested skill (internal)\
"""


async def generate_tasks_for_variant(
    client: anthropic.AsyncAnthropic,
    spec: dict,
    model: str,
    output_tasks: int = 3,
    mutation_tasks: int = 3,
    live_api_data: dict[str, str] | None = None,
) -> list[dict]:
    """Generate evaluation tasks for a single variant spec.

    Args:
        live_api_data: Optional mapping of API route path to JSON response
            body fetched from the running app (e.g. {"/api/shipments": "..."}).
            When provided, the LLM uses this ground-truth data to compute
            expected_output instead of relying solely on seed data text.
    """
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

    if live_api_data:
        user_prompt += "## Live API Data (ground truth — use this over seed data if they disagree)\n\n"
        for route, body in live_api_data.items():
            truncated = body[:20000] if len(body) > 20000 else body
            user_prompt += f"### `GET {route}`\n\n```json\n{truncated}\n```\n\n"

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
            f"Compute the EXACT correct expected_output or expected_mutations "
            f"for each task from the data above."
        )
    elif mutation_tasks > 0:
        system_prompt = _MUTATION_ONLY_SYSTEM_PROMPT
        user_prompt += (
            f"Generate exactly {mutation_tasks} mutation tasks. "
            f"Compute the EXACT correct expected_mutations for each task."
        )
    else:
        system_prompt = _OUTPUT_ONLY_SYSTEM_PROMPT
        user_prompt += (
            f"Generate exactly {output_tasks} retrieval tasks. "
            f"Compute the EXACT correct expected_output for each task "
            f"from the data above."
        )

    max_attempts = 3
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            async with client.messages.stream(
                model=model,
                max_tokens=8192,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            ) as stream:
                response = await stream.get_final_message()

            raw_text = response.content[0].text
            parsed = _extract_json(raw_text)
            tasks = parsed.get("tasks", [])
            if not tasks:
                raise ValueError("LLM returned valid JSON but 'tasks' array is empty")
            return tasks
        except (ValueError, json.JSONDecodeError, KeyError, IndexError) as e:
            last_err = e
            logger.warning(
                "Task generation attempt %d/%d failed for '%s': %s",
                attempt,
                max_attempts,
                spec.get("slug", "?"),
                e,
            )
            if attempt < max_attempts:
                await asyncio.sleep(2 * attempt)
    raise last_err  # type: ignore[misc]


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
                if not tasks:
                    logger.error("No tasks generated for '%s' after retries", slug)
                    return slug, []

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


def validate_expected_outputs(
    tasks: list[dict],
    live_api_data: dict[str, str],
) -> list[dict]:
    """Check each output task's expected_output against live API data.

    For each output task, verify that every value in expected_output appears
    somewhere in the live API responses. Tasks that fail validation get an
    ``_invalid`` flag and a ``_validation_error`` message.

    Returns the (possibly mutated) task list.
    """
    if not live_api_data:
        return tasks

    all_api_text = " ".join(live_api_data.values())

    for task in tasks:
        if task.get("scoring_type") != "output":
            continue
        expected = task.get("expected_output")
        if not expected or not isinstance(expected, dict):
            continue

        for key, value in expected.items():
            needle = str(value)
            if needle and needle not in all_api_text:
                task["_invalid"] = True
                task["_validation_error"] = (
                    f"expected_output[{key!r}] = {value!r} not found in "
                    f"any live API response"
                )
                logger.warning(
                    "Task %s failed validation: %s",
                    task.get("name", "?"),
                    task["_validation_error"],
                )
                break

    return tasks


def _build_v2_scoring_config(task: dict, sim_name: str) -> dict | None:
    """Build a V2ScoringConfig dict from a generated task's scoring fields.

    Supports multiple input formats:
    - Standard codegen: ``expected_output`` + ``output_schema`` fields
    - Hillclimb agent: ``scoring_config.scoring_schema`` dict
    - Pre-built v2: ``scoring_config.v2_scoring_config`` passthrough
    """
    scoring_type = task.get("scoring_type", "output")
    scoring_config = task.get("scoring_config") or {}

    # Passthrough: already a fully-formed v2 config (e.g. from API fetch)
    if isinstance(scoring_config, dict) and scoring_config.get("v2_scoring_config"):
        return scoring_config["v2_scoring_config"]

    if scoring_type == "output":
        output_schema = task.get("output_schema")
        expected_output = task.get("expected_output")

        # Fallback: hillclimb agent writes scoring_schema inside scoring_config
        if not expected_output and isinstance(scoring_config, dict):
            expected_output = scoring_config.get("scoring_schema")

        if expected_output:
            return {
                "output_config": {
                    "type": "json_schema",
                    "scoring_schema": expected_output,
                },
            }

        if output_schema:
            return {
                "output_config": {
                    "type": "json_schema",
                    "scoring_schema": output_schema,
                },
            }

        return None

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
    import re

    configs: list[dict] = []

    for spec in variant_specs:
        slug = spec.get("slug", spec.get("_generation", {}).get("slug", "unknown"))
        app_name = spec.get("app_name", "").strip()
        app_name = re.sub(r"[^a-z0-9-]", "", app_name.lower().replace(" ", "-"))
        app_name = app_name or sim_name_prefix
        skill_slug = re.sub(r"-\d+$", "", slug)
        sim_name = f"{app_name}-{skill_slug}"
        tasks = all_tasks.get(slug, [])

        for task in tasks:
            scoring_type = task.get("scoring_type", "output")
            v2_scoring_config = _build_v2_scoring_config(task, sim_name)

            config: dict = {
                "sim": sim_name,
                "task_name": f"{skill_slug}-{task.get('name', 'unnamed')}",
                "title": task.get("title", ""),
                "instruction": task.get("instruction", ""),
                "hint": task.get("hint", ""),
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
