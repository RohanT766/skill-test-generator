"""Publish generated skill-test variants as Plato simulators."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


async def _install_and_build(variant_dir: Path) -> bool:
    """Install dependencies and build a variant to verify it's valid."""
    web_dir = variant_dir / "web"
    if not web_dir.exists():
        logger.error("No web/ directory in variant %s", variant_dir.name)
        return False

    env = dict(os.environ)
    cache_root = variant_dir / ".cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    env["XDG_CACHE_HOME"] = str(cache_root)

    bun = shutil.which("bun")
    if not bun:
        logger.warning(
            "bun not found, skipping build validation for %s", variant_dir.name
        )
        return True

    try:
        proc = await asyncio.create_subprocess_exec(
            bun,
            "install",
            cwd=str(web_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                "bun install failed for %s:\n%s",
                variant_dir.name,
                stderr.decode()[:500],
            )
            return False

        proc = await asyncio.create_subprocess_exec(
            bun,
            "run",
            "build",
            cwd=str(web_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "Build check failed for %s (may still be usable):\n%s",
                variant_dir.name,
                stderr.decode()[:500],
            )
            return True

        logger.info("Variant '%s' builds successfully", variant_dir.name)
        return True
    except Exception as e:
        logger.error("Build validation error for %s: %s", variant_dir.name, e)
        return False


async def validate_variant(variant_dir: Path) -> bool:
    """Quick validation: check that key files exist and the app can build."""
    required_files = [
        "web/app/page.tsx",
        "web/db/schema.ts",
        "web/package.json",
    ]
    for rel in required_files:
        if not (variant_dir / rel).exists():
            logger.error(
                "Missing required file %s in variant %s", rel, variant_dir.name
            )
            return False

    return await _install_and_build(variant_dir)


def package_variant_metadata(
    variant_dir: Path,
    spec: dict,
    tasks: list[dict],
    sim_name: str,
) -> dict:
    """Create the sim metadata package for a variant.

    This bundles everything needed to register the variant as a Plato sim:
    - App spec (what was built)
    - Task definitions (how to test against it)
    - Sim configuration (how to run it)
    """
    return {
        "sim_name": sim_name,
        "variant_dir": str(variant_dir),
        "spec": {
            "app_name": spec.get("app_name", ""),
            "title": spec.get("title", ""),
            "skill_tested": spec.get("skill_tested", spec.get("skill_name", "")),
            "description": spec.get("description", ""),
        },
        "task_count": len(tasks),
        "sim_config": {
            "type": "webclone-variant",
            "runtime": {
                "port": 3000,
                "start_command": "bash start.sh",
                "stop_command": "bash stop.sh",
                "health_check": "/api/health",
            },
        },
    }


async def publish_variant_to_plato(
    sim_metadata: dict,
    tasks: list[dict],
    plato_api_url: str,
    plato_api_key: str,
    artifact_id: str | None = None,
) -> dict:
    """Publish a variant as a Plato simulator with associated test cases.

    Uses the V2 testcase API with proper CreateTestCaseRequest fields:
    - prompt (not instruction)
    - simulator_artifact_ids (not simulator)
    - v2_scoring_config + output_schema for automated evaluation
    """
    headers = {"X-API-Key": plato_api_key, "Content-Type": "application/json"}

    async with httpx.AsyncClient(
        base_url=plato_api_url, timeout=30, headers=headers
    ) as client:
        sim_name = sim_metadata["sim_name"]

        sim_payload = {
            "name": sim_name,
            "description": sim_metadata["spec"].get("description", ""),
            "config": sim_metadata["sim_config"],
            "metadata": {
                "skill_tested": sim_metadata["spec"].get("skill_tested", ""),
                "generator": "skill-test-generator",
            },
        }

        try:
            resp = await client.post("/api/v2/simulators", json=sim_payload)
            if resp.status_code in (200, 201):
                sim_record = resp.json()
                logger.info(
                    "Published sim '%s' -> %s", sim_name, sim_record.get("id", "")
                )
            elif resp.status_code == 409:
                logger.info("Sim '%s' already exists, updating tasks only", sim_name)
                sim_record = {"name": sim_name, "status": "already_exists"}
            else:
                logger.warning(
                    "Sim publish returned %d for '%s': %s",
                    resp.status_code,
                    sim_name,
                    resp.text[:200],
                )
                sim_record = {"name": sim_name, "error": resp.text[:200]}
        except Exception as e:
            logger.error("Failed to publish sim '%s': %s", sim_name, e)
            sim_record = {"name": sim_name, "error": str(e)}

        created_tasks = []
        for task in tasks:
            task_payload: dict = {
                "name": task.get("task_name", task.get("name", "")),
                "prompt": task.get("instruction", ""),
                "start_url": task.get("start_url", "/"),
                "metadata": task.get("metadata", {}),
            }

            if artifact_id:
                task_payload["simulator_artifact_ids"] = [artifact_id]

            v2_scoring = task.get("v2_scoring_config")
            if v2_scoring:
                task_payload["v2_scoring_config"] = v2_scoring

            output_schema = task.get("output_schema")
            if output_schema:
                task_payload["output_schema"] = output_schema

            try:
                resp = await client.post("/api/v2/testcases", json=task_payload)
                if resp.status_code in (200, 201):
                    tc = resp.json()
                    created_tasks.append(tc)
                    logger.info(
                        "Created testcase '%s' -> %s (scoring: %s)",
                        task_payload["name"],
                        tc.get("id", ""),
                        "v2" if v2_scoring else "none",
                    )
                else:
                    logger.warning(
                        "Task creation returned %d for '%s': %s",
                        resp.status_code,
                        task_payload["name"],
                        resp.text[:300],
                    )
            except Exception as e:
                logger.error("Failed to create task '%s': %s", task_payload["name"], e)

        return {
            "sim": sim_record,
            "tasks_created": len(created_tasks),
            "tasks_total": len(tasks),
            "testcase_ids": [t.get("id") for t in created_tasks if t.get("id")],
        }


async def publish_all_variants(
    variant_specs: list[dict],
    all_tasks: dict[str, list[dict]],
    sim_name_prefix: str,
    variants_dir: Path,
    output_dir: Path,
    plato_api_url: str,
    plato_api_key: str,
    artifact_ids: dict[str, str] | None = None,
    concurrency: int = 4,
) -> list[dict]:
    """Validate and publish all variants as Plato sims with scoring configs."""
    semaphore = asyncio.Semaphore(concurrency)
    results: list[dict] = []
    artifact_ids = artifact_ids or {}

    async def publish_one(spec: dict) -> dict | None:
        slug = spec.get("slug", spec.get("_generation", {}).get("slug", "unknown"))
        variant_dir = variants_dir / slug
        sim_name = f"{sim_name_prefix}-{slug}"
        tasks = all_tasks.get(slug, [])
        artifact_id = artifact_ids.get(slug)

        async with semaphore:
            valid = await validate_variant(variant_dir)
            if not valid:
                logger.warning("Variant '%s' failed validation, skipping publish", slug)
                return {"sim_name": sim_name, "status": "validation_failed"}

            metadata = package_variant_metadata(variant_dir, spec, tasks, sim_name)

            metadata_path = output_dir / slug / "sim_metadata.json"
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.write_text(json.dumps(metadata, indent=2))

            result = await publish_variant_to_plato(
                metadata,
                tasks,
                plato_api_url,
                plato_api_key,
                artifact_id=artifact_id,
            )
            result["sim_name"] = sim_name
            result["slug"] = slug
            return result

    coros = [publish_one(spec) for spec in variant_specs]
    for result in await asyncio.gather(*coros):
        if result is not None:
            results.append(result)

    published = sum(1 for r in results if "error" not in r.get("sim", {}))
    logger.info("Published %d/%d variants as sims", published, len(results))
    return results
