"""Skill Test Generator World — full pipeline from skill gaps to running evaluations."""

from __future__ import annotations

import asyncio
import importlib.resources
import io
import json
import logging
import tarfile
import time
from pathlib import Path

import httpx
from plato.worlds import BaseWorld, Observation, StepResult, register_world

from .config import (
    SkillDefinition,
    SkillTestGeneratorConfig,
    SkillTestGeneratorState,
    Stage,
    VariantStatus,
)

logger = logging.getLogger(__name__)

DEFAULT_RUNTIME = {"type": "vm", "vm": {"cpus": 2, "memory": 4096}}

SKIP_DIRS = {".turbo", ".cache", ".runtime", "__pycache__", ".git", "node_modules"}
S3_BUCKET = "plato-browser-session-data-prod"
S3_PREFIX = "skill-test-generator/variants"


def _resolve_template_source(template_name: str) -> Path:
    """Resolve the sohan template path from bundled dir, webclone package, or sibling dir."""
    bundled = Path("/world/templates") / template_name
    if bundled.is_dir():
        return bundled

    try:
        ref = (
            importlib.resources.files("webclone")
            .joinpath("templates")
            .joinpath(template_name)
        )
        path = Path(str(ref))
        if path.is_dir():
            return path
    except Exception:
        pass

    worlds_dir = Path(__file__).resolve().parents[4] / "worlds"
    if not worlds_dir.is_dir():
        worlds_dir = Path(__file__).resolve().parents[3]
    fallback = (
        worlds_dir / "webclone" / "src" / "webclone" / "templates" / template_name
    )
    if fallback.is_dir():
        return fallback

    raise RuntimeError(
        f"Could not resolve template '{template_name}'. "
        f"Ensure the template exists at /world/templates/{template_name} or plato-world-webclone is installed."
    )


@register_world("plato-world-skill-test-generator")
class SkillTestGeneratorWorld(
    BaseWorld[SkillTestGeneratorConfig, SkillTestGeneratorState]
):
    """Generates targeted skill-test simulators from benchmark-review skill gaps.

    Pipeline:
      Config → INGEST  → skills from S3
             → DESIGN  → variant specs (parallel LLM calls)
             → CODEGEN → claude-code agents on Chronos VMs (parallel, 1 per spec)
             → TASKS   → testcase definitions (parallel LLM calls)
             → PUBLISH → Plato sims + testcases
             → RUN     → CUA benchmark via Chronos
             → EVALUATE → collect results
    """

    def __init__(self) -> None:
        super().__init__()
        self._skills: list[SkillDefinition] = []
        self._variant_specs: list[dict] = []
        self._all_tasks: dict[str, list[dict]] = {}

    async def reset(self) -> Observation:
        self._state = SkillTestGeneratorState()
        self.config.code.mkdir(parents=True, exist_ok=True)
        self.config.output.mkdir(parents=True, exist_ok=True)

        logger.info("=" * 60)
        logger.info("SKILL TEST GENERATOR")
        logger.info("=" * 60)

        await self._preflight_checks()

        return Observation(text="Ready.")

    async def _preflight_checks(self) -> None:
        """Validate API keys and credentials before running the pipeline."""
        config = self.config
        errors: list[str] = []

        if not config.anthropic_api_key:
            errors.append("anthropic_api_key is empty")
        else:
            try:
                import anthropic

                client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)
                await client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=1,
                    messages=[{"role": "user", "content": "ping"}],
                )
                logger.info("Preflight: Anthropic API key valid")
            except Exception as e:
                errors.append(f"Anthropic API key invalid: {e}")

        if not config.plato_api_key:
            errors.append("plato_api_key is empty")
        else:
            try:
                async with httpx.AsyncClient(
                    base_url=config.plato_api_url, timeout=10
                ) as http:
                    resp = await http.get(
                        "/api/v1/env/simulators",
                        headers={"X-API-Key": config.plato_api_key},
                        params={"page": 1, "page_size": 1},
                    )
                    resp.raise_for_status()
                    logger.info("Preflight: Plato API key valid")
            except Exception as e:
                errors.append(f"Plato API key invalid: {e}")

        if config.s3_skills:
            if not config.aws_access_key_id or not config.aws_secret_access_key:
                errors.append(
                    "s3_skills requires aws_access_key_id and aws_secret_access_key"
                )
            else:
                try:
                    import boto3

                    kwargs: dict = {"region_name": "us-west-1"}
                    kwargs["aws_access_key_id"] = config.aws_access_key_id
                    kwargs["aws_secret_access_key"] = config.aws_secret_access_key
                    if config.aws_session_token:
                        kwargs["aws_session_token"] = config.aws_session_token
                    s3 = boto3.Session(**kwargs).client("s3")
                    s3.head_bucket(Bucket="plato-browser-session-data-prod")
                    logger.info("Preflight: AWS credentials valid (S3 accessible)")
                except Exception as e:
                    errors.append(f"AWS credentials invalid (S3 inaccessible): {e}")

        if not config.s3_skills and not config.custom_skills:
            errors.append("No skills configured: set s3_skills or custom_skills")

        if errors:
            msg = "Preflight checks FAILED:\n" + "\n".join(f"  - {e}" for e in errors)
            logger.error(msg)
            raise RuntimeError(msg)

        logger.info("Preflight: all checks passed")

    async def step(self) -> StepResult:
        config = self.config
        state = self.state

        if config.resume_variants and not state.variants:
            logger.info(
                "Resuming from pre-existing variants (%d)", len(config.resume_variants)
            )
            for rv in config.resume_variants:
                vs = VariantStatus(
                    skill_name=rv["skill_name"],
                    slug=rv["slug"],
                    sim_name=rv.get("sim_name", ""),
                    artifact_id=rv.get("artifact_id", ""),
                    testcase_ids=rv.get("testcase_ids", []),
                    task_count=len(rv.get("testcase_ids", [])),
                    stage="published",
                )
                state.variants.append(vs)
            state.skills_loaded = len({vs.skill_name for vs in state.variants})

        all_stages = [
            Stage.INGEST,
            Stage.DESIGN,
            Stage.CODEGEN,
            Stage.RUN,
            Stage.EVALUATE,
        ]
        if config.resume_variants:
            stages = [Stage.RUN, Stage.EVALUATE]
        elif config.stage:
            stages = [config.stage]
        else:
            stages = all_stages

        handler_map = {
            Stage.INGEST: self._run_ingest,
            Stage.DESIGN: self._run_design,
            Stage.CODEGEN: self._run_variant_pipelines,
            Stage.RUN: self._run_agent_eval,
            Stage.EVALUATE: self._run_evaluate,
        }

        for stage in stages:
            if state.stage_completed.get(stage.value):
                logger.info("Stage '%s' already completed, skipping", stage.value)
                continue

            handler = handler_map.get(stage)
            if not handler:
                logger.warning("Stage '%s' has no handler, skipping", stage.value)
                continue

            state.current_stage = stage
            logger.info("=" * 60)
            logger.info("STAGE: %s", stage.value.upper())
            logger.info("=" * 60)

            await handler()
            state.stage_completed[stage.value] = True

        return StepResult(
            observation=Observation(text=self._build_summary()),
            done=True,
        )

    # ------------------------------------------------------------------
    # INGEST
    # ------------------------------------------------------------------

    async def _run_ingest(self) -> None:
        from .skill_ingestion import (
            _slugify,
            generate_short_names,
            persist_short_names_to_s3,
            prepare_skills,
        )

        config = self.config
        self._skills = prepare_skills(
            s3_skill_names=config.s3_skills or None,
            custom_skills=config.custom_skills or None,
            aws_access_key_id=config.aws_access_key_id,
            aws_secret_access_key=config.aws_secret_access_key,
            aws_session_token=config.aws_session_token,
            max_skills=config.max_skills,
        )

        if any(not s.short_name for s in self._skills):
            await generate_short_names(self._skills, config.anthropic_api_key)
            persist_short_names_to_s3(
                self._skills,
                aws_access_key_id=config.aws_access_key_id,
                aws_secret_access_key=config.aws_secret_access_key,
                aws_session_token=config.aws_session_token,
            )

        self.state.skills_loaded = len(self._skills)
        sps = config.specs_per_skill
        variants: list[VariantStatus] = []
        for s in self._skills:
            base_slug = _slugify(s.short_name or s.name)
            short = s.short_name or ""
            if sps == 1:
                variants.append(
                    VariantStatus(skill_name=s.name, short_name=short, slug=base_slug)
                )
            else:
                for vi in range(1, sps + 1):
                    variants.append(
                        VariantStatus(
                            skill_name=s.name,
                            short_name=short,
                            slug=f"{base_slug}-v{vi}",
                        )
                    )
        self.state.variants = variants
        (self.config.output / "skills.json").write_text(
            json.dumps([s.model_dump() for s in self._skills], indent=2)
        )
        for i, s in enumerate(self._skills, 1):
            logger.info(
                "  %d. [%d sess] %s (short: %s)",
                i,
                len(s.session_ids),
                s.name,
                s.short_name,
            )

    # ------------------------------------------------------------------
    # DESIGN — parallel spec generation via streaming LLM calls
    # ------------------------------------------------------------------

    async def _run_design(self) -> None:
        from .variant_generator import design_all_variants

        config = self.config
        if not self._skills:
            self._load_skills_from_disk()
        if not self._skills:
            logger.warning("No skills loaded. Run ingest first.")
            return

        self._variant_specs = await design_all_variants(
            skills=self._skills,
            anthropic_api_key=config.anthropic_api_key,
            model=config.design_model,
            concurrency=config.concurrency,
            specs_per_skill=config.specs_per_skill,
        )
        (config.output / "variant_specs.json").write_text(
            json.dumps(self._variant_specs, indent=2, default=str)
        )
        run_suffix = hex(int(time.time()))[2:]
        for vs in self.state.variants:
            match = [s for s in self._variant_specs if s.get("slug") == vs.slug]
            vs.stage = "designed" if match else "design_failed"
            if match:
                vs.sim_name = f"{config.sim_name_prefix}-{vs.slug}-{run_suffix}"

    # ------------------------------------------------------------------
    # CODEGEN — parallel claude-code agent sessions on Chronos VMs
    # ------------------------------------------------------------------

    def _code_data_dir(self) -> Path:
        """Return the writable data directory inside the code workspace."""
        try:
            code_ws = self.workspace("code")
            return code_ws.path
        except Exception:
            return self.config.code

    def _agent_mount_prefix(self) -> str:
        """Return the mount path the agent VM sees for the code workspace."""
        try:
            code_ws = self.workspace("code")
            return code_ws.mount_path
        except Exception:
            return str(self.config.code)

    # ------------------------------------------------------------------
    # VARIANT PIPELINES — per-variant isolated VMs
    # ------------------------------------------------------------------

    async def _run_variant_pipelines(self) -> None:
        """Run full per-variant pipelines in parallel.

        Each variant goes through codegen → verify → agent-fix → build →
        snapshot → task-gen → testcase-creation on its own isolated VM.
        Replaces the old sequential CODEGEN → TASKS → PUBLISH stages.

        Two independent semaphores gate concurrency:
          • llm_sem  — concurrent LLM API calls  (config.concurrency)
          • vm_sem   — concurrent pipeline VMs    (config.max_parallel_pipelines)
        """
        import anthropic as _anthropic

        from .codegen_agent import build_codegen_instruction
        from .task_generator import build_plato_task_configs, generate_tasks_for_variant
        from .variant_generator import (
            _copy_sohan_template,
            _validate_code_files,
            apply_variant_code,
            generate_variant_code,
        )

        config = self.config
        if not self._variant_specs:
            self._load_variant_specs_from_disk()
        if not self._variant_specs:
            logger.warning("No variant specs found — nothing to pipeline.")
            return

        template_source = _resolve_template_source(config.template_name)
        code_data = self._code_data_dir()
        agent_prefix = self._agent_mount_prefix()
        variants_dir = code_data / "variants"
        variants_dir.mkdir(parents=True, exist_ok=True)

        llm_client = _anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)
        llm_sem = asyncio.Semaphore(config.concurrency)
        vm_sem = asyncio.Semaphore(config.max_parallel_pipelines)

        eligible = [vs for vs in self.state.variants if vs.stage == "designed"]
        logger.info(
            "Parallel pipelines: %d variants  (llm_concurrency=%d, max_vms=%d)",
            len(eligible),
            config.concurrency,
            config.max_parallel_pipelines,
        )

        async def _single_pipeline(vs: VariantStatus) -> None:
            spec = next(
                (s for s in self._variant_specs if s.get("slug") == vs.slug),
                None,
            )
            if not spec:
                vs.stage = "pipeline_failed"
                vs.error = "no spec found"
                return

            try:
                await _single_pipeline_inner(vs, spec)
            except Exception as e:
                logger.error("  [%s] Unhandled pipeline error: %s", vs.slug, e)
                if vs.stage != "published":
                    vs.stage = "pipeline_failed"
                    vs.error = vs.error or f"unhandled: {e}"

        async def _single_pipeline_inner(vs: VariantStatus, spec: dict) -> None:
            variant_dir = variants_dir / vs.slug
            sim_name = vs.sim_name or f"{config.sim_name_prefix}-{vs.slug}"
            vs.sim_name = sim_name

            # ── Phase 1: LLM codegen ──────────────────────────────────
            _copy_sohan_template(template_source, variant_dir)
            variant_dir.mkdir(parents=True, exist_ok=True)
            (variant_dir / "spec.json").write_text(json.dumps(spec, indent=2))

            files_written: list[str] = []
            validation_errors: list[str] = []
            async with llm_sem:
                try:
                    logger.info("  [%s] One-shot codegen …", vs.slug)
                    code_files = await generate_variant_code(
                        llm_client,
                        spec,
                        config.design_model,
                    )
                    validation_errors = _validate_code_files(code_files)
                    files_written = apply_variant_code(
                        variant_dir,
                        code_files,
                        spec=spec,
                    )
                    logger.info("  [%s] Wrote %d files", vs.slug, len(files_written))
                except Exception as e:
                    logger.error("  [%s] Codegen failed: %s", vs.slug, e)
                    vs.stage = "pipeline_failed"
                    vs.error = f"codegen: {e}"
                    return

            if validation_errors:
                logger.warning(
                    "  [%s] Validation warnings: %s", vs.slug, validation_errors
                )

            # ── Phase 2: Pipeline VM (verify → build → snapshot) ──────
            artifact_id: str | None = None
            last_checks: list[dict] = []

            for attempt in range(2):
                tarball = self._tar_variant(variant_dir, sim_name)
                url = self._upload_to_s3(
                    tarball,
                    f"{S3_PREFIX}/{sim_name}-pipe-{attempt}.tar.gz",
                )

                async with vm_sem:
                    logger.info("  [%s] Pipeline VM attempt %d …", vs.slug, attempt + 1)
                    try:
                        result = await self._pipeline_vm_verify_build_publish(
                            vs=vs,
                            sim_name=sim_name,
                            spec=spec,
                            presigned_url=url,
                        )
                    except Exception as e:
                        logger.error("  [%s] Pipeline VM error: %s", vs.slug, e)
                        result = {"artifact_id": None, "verified": False, "checks": []}

                last_checks = result.get("checks", [])

                if result.get("artifact_id"):
                    artifact_id = result["artifact_id"]
                    break

                if attempt == 0 and config.coder_agent and not result.get("verified"):
                    logger.info("  [%s] Verify failed, launching agent fix …", vs.slug)
                    try:
                        instruction = build_codegen_instruction(
                            spec=spec,
                            slug=vs.slug,
                            variant_dir=f"{agent_prefix}/variants/{vs.slug}",
                            verify_port=config.codegen_verify_port,
                            files_written=files_written,
                            validation_errors=validation_errors,
                            deps_installed=False,
                            check_results=last_checks,
                        )
                        runner = self.agent(
                            config.coder_agent,
                            display_name=f"fix-{vs.slug}",
                            workspaces=[self.workspace("code")],
                        )
                        await runner.run(instruction=instruction)
                        logger.info("  [%s] Agent fix complete", vs.slug)
                    except Exception as e:
                        logger.error("  [%s] Agent fix error: %s", vs.slug, e)
                        break
                else:
                    break

            if not artifact_id:
                vs.stage = "pipeline_failed"
                vs.error = "no artifact after all attempts"
                return

            vs.artifact_id = artifact_id
            logger.info("  [%s] Artifact: %s", vs.slug, artifact_id)

            # ── Phase 3: Generate tasks ───────────────────────────────
            async with llm_sem:
                try:
                    logger.info("  [%s] Generating tasks …", vs.slug)
                    tasks = await generate_tasks_for_variant(
                        llm_client,
                        spec,
                        config.design_model,
                        output_tasks=config.output_tasks_per_variant,
                        mutation_tasks=config.mutation_tasks_per_variant,
                    )
                    self._all_tasks[vs.slug] = tasks
                    vs.task_count = len(tasks)

                    tasks_dir = config.output / vs.slug
                    tasks_dir.mkdir(parents=True, exist_ok=True)
                    (tasks_dir / "tasks.json").write_text(
                        json.dumps({"tasks": tasks}, indent=2),
                    )
                    logger.info("  [%s] Generated %d tasks", vs.slug, len(tasks))
                except Exception as e:
                    logger.error("  [%s] Task gen failed: %s", vs.slug, e)
                    vs.error = f"task gen: {e}"

            # ── Phase 4: Create testcases ─────────────────────────────
            tasks = self._all_tasks.get(vs.slug, [])
            if tasks and artifact_id:
                try:
                    await self._create_testcases(vs, tasks, artifact_id)
                    vs.stage = "published"
                except Exception as e:
                    logger.error("  [%s] Testcase creation error: %s", vs.slug, e)
                    vs.stage = "pipeline_failed"
                    vs.error = f"testcase creation: {e}"
            else:
                vs.stage = "pipeline_failed"
                vs.error = "no tasks for testcase creation"

            logger.info("  [%s] Pipeline complete → %s", vs.slug, vs.stage)

        await asyncio.gather(*[_single_pipeline(vs) for vs in eligible])

        # Persist combined results
        plato_configs = build_plato_task_configs(
            self._variant_specs,
            self._all_tasks,
            config.sim_name_prefix,
        )
        (config.output / "plato_task_configs.json").write_text(
            json.dumps(plato_configs, indent=2),
        )

        publish_results = []
        for vs in eligible:
            if vs.artifact_id:
                publish_results.append(
                    {
                        "slug": vs.slug,
                        "sim_name": vs.sim_name,
                        "artifact_id": vs.artifact_id,
                        "testcase_count": len(vs.testcase_ids),
                    }
                )
        (config.output / "publish_results.json").write_text(
            json.dumps(publish_results, indent=2),
        )

        published = sum(1 for vs in eligible if vs.stage == "published")
        failed = sum(1 for vs in eligible if "failed" in vs.stage)
        logger.info(
            "All pipelines done: %d published, %d failed / %d total",
            published,
            failed,
            len(eligible),
        )

    # ------------------------------------------------------------------

    async def _pipeline_vm_verify_build_publish(
        self,
        vs: VariantStatus,
        sim_name: str,
        spec: dict,
        presigned_url: str,
    ) -> dict:
        """Run verify + build + snapshot on an isolated pipeline VM.

        Returns ``{"artifact_id": str|None, "verified": bool, "checks": [...]}``.
        The VM is always closed in the ``finally`` block.
        """
        from plato._generated.api.v2.sessions import (
            close as sessions_close,
            execute as sessions_execute,
            make as sessions_make,
            snapshot as sessions_snapshot,
        )
        from plato._generated.models import (
            AppSchemasBuildModelsSimConfigCompute as SimConfigCompute,
            AppApiV2SchemasSessionCreateSnapshotRequest,
            CreateSessionFromEnvs,
            EnvFromResource,
            Envs,
            ExecuteCommandRequest,
            RunSessionSource,
        )

        config = self.config
        api_key = config.plato_api_key
        api_url = config.plato_api_url
        session_id: str | None = None
        checks: list[dict] = []

        async with httpx.AsyncClient(
            base_url=api_url,
            timeout=httpx.Timeout(300.0, connect=30.0),
        ) as http:

            async def _exec(cmd: str, timeout: int = 30) -> tuple[str, bool]:
                r = await sessions_execute.asyncio(
                    client=http,
                    session_id=session_id,
                    body=ExecuteCommandRequest(command=cmd, timeout=timeout),
                    x_api_key=api_key,
                )
                for _, v in r.results.items():
                    return (v.stdout or "").strip(), bool(v.success)
                return "", False

            try:
                # ── Create VM ─────────────────────────────────────────
                env = EnvFromResource(
                    simulator=sim_name,
                    sim_config=SimConfigCompute(
                        cpus=config.pipeline_vm_cpus,
                        memory=config.pipeline_vm_memory,
                        disk=10240,
                    ),
                )
                body = CreateSessionFromEnvs(
                    envs=[Envs(root=env)],
                    timeout=1800,
                    source=RunSessionSource.SDK,
                )
                resp = await sessions_make.asyncio(
                    client=http,
                    body=body,
                    x_api_key=api_key,
                )
                session_id = resp.session_id
                logger.info("  [%s] VM %s created", vs.slug, session_id)

                for _ in range(60):
                    await asyncio.sleep(3)
                    sr = await http.get(
                        f"/api/v2/sessions/{session_id}",
                        headers={"X-API-Key": api_key},
                    )
                    jobs = sr.json().get("jobs", [{}])
                    if jobs and jobs[0].get("status") == "running":
                        break
                else:
                    raise RuntimeError("VM never reached running state")
                logger.info("  [%s] VM running", vs.slug)

                # ── Download + setup ──────────────────────────────────
                await _exec(
                    f"curl -sfL '{presigned_url}' -o /tmp/variant.tar.gz && "
                    f"mkdir -p /tmp/variant && tar xzf /tmp/variant.tar.gz -C /tmp/variant",
                    timeout=180,
                )

                await _exec(
                    "if ! command -v bun >/dev/null 2>&1; then "
                    "  curl -fsSL https://bun.sh/install | bash && "
                    "  ln -sf /root/.bun/bin/bun /usr/local/bin/bun && "
                    "  ln -sf /root/.bun/bin/bunx /usr/local/bin/bunx; "
                    "fi",
                    timeout=120,
                )

                await _exec(
                    "if ! command -v node >/dev/null 2>&1; then "
                    "  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && "
                    "  apt-get install -y --no-install-recommends nodejs && "
                    "  rm -rf /var/lib/apt/lists/*; "
                    "fi",
                    timeout=120,
                )

                app_dir = "/tmp/variant/web"
                preamble = 'export PATH="/root/.bun/bin:/usr/local/bin:$PATH"'

                out, _ = await _exec(
                    f"{preamble} && cd {app_dir} && bun install 2>&1 | tail -10",
                    timeout=300,
                )
                logger.info("  [%s] bun install: %s", vs.slug, out[-300:])

                await _exec(
                    f"""python3 -c "
import pathlib, re
p = pathlib.Path('{app_dir}/db/client.ts')
src = p.read_text()
if 'mkdirSync' not in src:
    src = 'import fs from \\"node:fs\\";\\n' + src
    src = src.replace(
        'const c = new PGlite(getDataDir())',
        'const dataDir = getDataDir();\\n      fs.mkdirSync(dataDir, {{ recursive: true }});\\n      const c = new PGlite(dataDir)',
    )
    p.write_text(src)
    print('Patched db/client.ts with mkdirSync')
else:
    print('db/client.ts already patched')
" """,
                    timeout=10,
                )

                # ── VERIFY (dev server) ───────────────────────────────
                await _exec("fuser -k 3000/tcp 2>/dev/null; sleep 1", timeout=10)
                await _exec(
                    f"{preamble} && cd {app_dir} && mkdir -p /tmp/pglite-data && "
                    f"PORT=3000 APP_PORT=3000 "
                    f"nohup bun run dev -- --hostname 0.0.0.0 --port 3000 "
                    f"> /tmp/dev.log 2>&1 &",
                    timeout=30,
                )

                dev_ok = False
                for _ in range(40):
                    out, _ = await _exec(
                        "curl -sf http://127.0.0.1:3000/api/health -o /dev/null "
                        "&& echo OK || echo FAIL",
                        timeout=10,
                    )
                    if "OK" in out:
                        dev_ok = True
                        break
                    await asyncio.sleep(3)

                if not dev_ok:
                    log_tail, _ = await _exec(
                        "tail -50 /tmp/dev.log 2>/dev/null", timeout=10
                    )
                    checks.append(
                        {
                            "name": "server_startup",
                            "pass": False,
                            "error": f"Dev server never healthy. Log: {log_tail[-500:]}",
                        }
                    )
                    return {"artifact_id": None, "verified": False, "checks": checks}

                checks.append({"name": "server_startup", "pass": True, "error": ""})
                checks.append({"name": "GET /api/health", "pass": True, "error": ""})

                for r in spec.get("api_routes", []):
                    route = r.get("route", "")
                    if not route or "[" in route or "/health" in route:
                        continue
                    out, _ = await _exec(
                        f"curl -s -w '\\nHTTP_CODE:%{{http_code}}' "
                        f"http://127.0.0.1:3000{route} 2>&1 | tail -c 2000",
                        timeout=15,
                    )
                    ok = "HTTP_CODE:200" in out and len(out) > 30
                    checks.append(
                        {
                            "name": f"GET {route}",
                            "pass": ok,
                            "error": "" if ok else out[:300],
                        }
                    )

                page_html, _ = await _exec(
                    "curl -s http://127.0.0.1:3000/ 2>&1 | head -c 50000",
                    timeout=15,
                )
                has_localhost = (
                    "localhost:3000/api" in page_html
                    or "127.0.0.1:3000/api" in page_html
                )
                if has_localhost:
                    checks.append(
                        {
                            "name": "no_localhost_urls",
                            "pass": False,
                            "error": "Page HTML contains hardcoded localhost API URLs",
                        }
                    )
                    logger.warning(
                        "  [%s] Frontend has localhost URLs in HTML — will break behind proxy",
                        vs.slug,
                    )
                else:
                    checks.append(
                        {"name": "no_localhost_urls", "pass": True, "error": ""}
                    )

                n_pass = sum(1 for c in checks if c["pass"])
                n_fail = sum(1 for c in checks if not c["pass"])
                logger.info(
                    "  [%s] Verify: %d passed, %d failed", vs.slug, n_pass, n_fail
                )

                if not all(c["pass"] for c in checks):
                    return {"artifact_id": None, "verified": False, "checks": checks}

                # ── BUILD ─────────────────────────────────────────────
                await _exec("fuser -k 3000/tcp 2>/dev/null; sleep 2", timeout=10)

                build_out, build_ok = await _exec(
                    f"{preamble} && cd {app_dir} && "
                    "NODE_ENV=production NEXT_DIST_DIR=.next "
                    "node ./node_modules/next/dist/bin/next build 2>&1 | tail -40",
                    timeout=300,
                )
                logger.info("  [%s] Build: %s", vs.slug, build_out[-300:])

                check_out, _ = await _exec(
                    f"test -f {app_dir}/.next/BUILD_ID "
                    "&& echo HAS_PROD_BUILD || echo NO_PROD_BUILD",
                    timeout=10,
                )
                has_prod_build = "HAS_PROD_BUILD" in check_out
                if not build_ok or not has_prod_build:
                    err = (
                        f"Production build failed or missing BUILD_ID "
                        f"(build_ok={build_ok}, check={check_out.strip()}). "
                        f"Build log tail: {build_out[-1200:]}"
                    )
                    checks.append(
                        {
                            "name": "production_build",
                            "pass": False,
                            "error": err[:2000],
                        }
                    )
                    logger.error("  [%s] %s", vs.slug, err)
                    return {"artifact_id": None, "verified": False, "checks": checks}
                checks.append({"name": "production_build", "pass": True, "error": ""})

                # ── SERVE (for snapshot) ──────────────────────────────
                await _exec(
                    "fuser -k 3000/tcp 2>/dev/null; "
                    "docker stop $(docker ps -q) 2>/dev/null; sleep 2",
                    timeout=30,
                )
                start_cmd = (
                    "node ./node_modules/next/dist/bin/next start "
                    "--hostname 0.0.0.0 -p 3000"
                )
                await _exec(
                    f"{preamble} && cd {app_dir} && mkdir -p /tmp/pglite-data && "
                    f"NEXT_DIST_DIR=.next PORT=3000 APP_PORT=3000 "
                    f"nohup {start_cmd} > /tmp/next.log 2>&1 &",
                    timeout=30,
                )

                for _ in range(40):
                    out, _ = await _exec(
                        "curl -sf http://127.0.0.1:3000/api/health -o /dev/null || "
                        "curl -sf http://127.0.0.1:3000/ -o /dev/null "
                        "&& echo OK || echo FAIL",
                        timeout=10,
                    )
                    if "OK" in out:
                        break
                    await asyncio.sleep(5)
                else:
                    raise RuntimeError("Server unhealthy after build")

                await asyncio.sleep(8)

                # Seed API routes before snapshot
                seed_routes = [
                    r.get("route", "")
                    for r in spec.get("api_routes", [])
                    if r.get("route")
                    and "/health" not in r.get("route", "")
                    and "[" not in r.get("route", "")
                ]
                if not seed_routes:
                    seed_routes = ["/api/items", "/api/data"]
                for route in seed_routes:
                    for seed_attempt in range(10):
                        out, _ = await _exec(
                            f"curl -s -w '\\nHTTP_CODE:%{{http_code}}' "
                            f"http://127.0.0.1:3000{route} 2>&1 | tail -c 2000",
                            timeout=30,
                        )
                        if "HTTP_CODE:200" in out and len(out) > 30:
                            logger.info(
                                "  [%s] Seed %s OK (attempt %d)",
                                vs.slug,
                                route,
                                seed_attempt,
                            )
                            break
                        await asyncio.sleep(3)
                await asyncio.sleep(3)

                # ── BOOT SERVICE (so app starts on snapshot restore) ──
                svc_exec = (
                    "/usr/bin/node ./node_modules/next/dist/bin/next start "
                    "--hostname 0.0.0.0 -p 3000"
                )
                svc_unit = (
                    "[Unit]\n"
                    "Description=Next.js App\n"
                    "After=network.target\n"
                    "\n"
                    "[Service]\n"
                    "Type=simple\n"
                    f"WorkingDirectory={app_dir}\n"
                    f"Environment=PATH=/root/.bun/bin:/usr/local/bin:/usr/bin:/bin\n"
                    "Environment=NODE_ENV=production\n"
                    "Environment=NEXT_DIST_DIR=.next\n"
                    "Environment=PORT=3000\n"
                    "Environment=APP_PORT=3000\n"
                    f"ExecStartPre=/bin/mkdir -p /tmp/pglite-data\n"
                    f"ExecStart={svc_exec}\n"
                    "Restart=always\n"
                    "RestartSec=3\n"
                    "\n"
                    "[Install]\n"
                    "WantedBy=multi-user.target\n"
                )
                await _exec(
                    f"cat > /etc/systemd/system/nextapp.service << 'SVCEOF'\n"
                    f"{svc_unit}SVCEOF",
                    timeout=10,
                )
                await _exec(
                    "systemctl daemon-reload && systemctl enable nextapp.service",
                    timeout=15,
                )
                logger.info("  [%s] Installed nextapp.service for boot", vs.slug)

                # ── ENSURE SIMULATOR CATALOG ENTRY EXISTS ─────────────
                from plato._generated.api.v1.env import create_simulator
                from plato._generated.models import (
                    CreateSimulatorRequest,
                    SimulatorConfig,
                    Type6,
                )

                PLATO_LOGO = "https://plato-api.anthropic.com/static/plato-logo.png"
                try:
                    await create_simulator.asyncio(
                        client=http,
                        body=CreateSimulatorRequest(
                            name=sim_name,
                            simType="docker_app",
                            config=SimulatorConfig(type=Type6.docker_app),
                            enabled=True,
                            imgUrl=PLATO_LOGO,
                            internalAppPort=3000,
                        ),
                        x_api_key=api_key,
                    )
                    logger.info("  [%s] Created simulator '%s'", vs.slug, sim_name)
                except Exception as e:
                    if "already exists" in str(e).lower() or "409" in str(e):
                        logger.info(
                            "  [%s] Simulator '%s' already exists",
                            vs.slug,
                            sim_name,
                        )
                    else:
                        logger.warning(
                            "  [%s] Could not create simulator '%s': %s",
                            vs.slug,
                            sim_name,
                            e,
                        )

                # ── SNAPSHOT ──────────────────────────────────────────
                wait_selector = self._derive_wait_selector(vs.slug)
                flows_yaml = (
                    "flows:\n"
                    "- name: login\n"
                    "  steps:\n"
                    "  - type: navigate\n"
                    "    url: /\n"
                    "    timeout: 60000\n"
                    "    retries: 3\n"
                    "    retry_delay_ms: 5000\n"
                    "  - type: wait\n"
                    "    duration: 5000\n"
                    "  - type: wait_for_selector\n"
                    f'    selector: "{wait_selector}"\n'
                    "    timeout: 60000\n"
                    "    retries: 5\n"
                    "    retry_delay_ms: 3000\n"
                    "  - type: wait\n"
                    "    duration: 3000\n"
                )

                snap = await sessions_snapshot.asyncio(
                    client=http,
                    session_id=session_id,
                    body=AppApiV2SchemasSessionCreateSnapshotRequest(
                        override_service=sim_name,
                        override_dataset="base",
                        internal_app_port=3000,
                        flows=flows_yaml,
                        target="sims.plato.so",
                    ),
                    x_api_key=api_key,
                )

                artifact_id: str | None = None
                for _, snap_result in snap.results.items():
                    if snap_result.success and snap_result.artifact_id:
                        artifact_id = snap_result.artifact_id
                        break

                if not artifact_id:
                    errors = [r.error for r in snap.results.values() if r.error]
                    raise RuntimeError(f"Snapshot failed: {errors}")

                logger.info("  [%s] Snapshot: %s", vs.slug, artifact_id)

                try:
                    from plato._generated.api.v1.cluster import prefetch_snapshot
                    from plato._generated.models import PrefetchRequest

                    prefetch_snapshot.sync(
                        client=httpx.Client(base_url=api_url, timeout=60.0),
                        body=PrefetchRequest(artifact_id=artifact_id),
                        x_api_key=api_key,
                    )
                except Exception:
                    pass

                return {"artifact_id": artifact_id, "verified": True, "checks": checks}

            finally:
                if session_id:
                    try:
                        await sessions_close.asyncio(
                            client=http,
                            session_id=session_id,
                            x_api_key=api_key,
                        )
                        logger.info("  [%s] Pipeline VM closed", vs.slug)
                    except Exception:
                        pass

    def _derive_wait_selector(self, slug: str) -> str:
        """Build a CSS selector for snapshot login flow based on variant spec components."""
        spec = next((s for s in self._variant_specs if s.get("slug") == slug), {})
        all_components: set[str] = set()
        for page in spec.get("pages", []):
            for comp in page.get("key_components", []):
                all_components.add(comp.lower())

        selectors: list[str] = []
        if "table" in all_components:
            selectors.append("tbody tr")
        if "card" in all_components:
            selectors.append("[class*=card]")
        if "sidebar" in all_components:
            selectors.append("[class*=sidebar], nav")
        if "accordion" in all_components:
            selectors.append("[data-state]")

        if not selectors:
            selectors = ["main", "[role='main']", "h1", "h2"]

        return ", ".join(selectors)

    def _tar_variant(self, variant_dir: Path, name: str) -> bytes:
        """Create a tarball of the variant directory, excluding heavy dirs.

        Includes the ``.next`` production build but skips per-port dev caches
        (``.next-<port>``).
        """
        import os as _os

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for root, dirs, files in _os.walk(variant_dir):
                dirs[:] = [
                    d for d in dirs if d not in SKIP_DIRS and not d.startswith(".next-")
                ]
                for fname in files:
                    full = Path(root) / fname
                    arcname = str(full.relative_to(variant_dir))
                    tar.add(str(full), arcname=arcname)
        buf.seek(0)
        size_mb = len(buf.getvalue()) / (1024 * 1024)
        logger.info("  Tarball for %s: %.1fMB", name, size_mb)
        return buf.getvalue()

    def _upload_to_s3(self, data: bytes, key: str) -> str:
        """Upload bytes to S3 and return a presigned GET URL."""
        import boto3

        config = self.config
        kwargs: dict = {"region_name": "us-west-1"}
        if config.aws_access_key_id:
            kwargs["aws_access_key_id"] = config.aws_access_key_id
            kwargs["aws_secret_access_key"] = config.aws_secret_access_key
            if config.aws_session_token:
                kwargs["aws_session_token"] = config.aws_session_token
        session = boto3.Session(**kwargs)
        s3 = session.client("s3")
        s3.put_object(Bucket=S3_BUCKET, Key=key, Body=data)
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=3600,
        )
        logger.info("  Uploaded to s3://%s/%s", S3_BUCKET, key)
        return url

    async def _create_testcases(
        self,
        vs: VariantStatus,
        tasks: list[dict],
        artifact_id: str,
    ) -> None:
        """Create Plato testcases for each task in a variant."""
        from .task_generator import _build_v2_scoring_config

        config = self.config
        sim_name = vs.sim_name or f"{config.sim_name_prefix}-{vs.slug}"

        from plato._generated.api.v1.simulator import get_simulator_id
        from plato._generated.api.v2.testcases import create_testcase
        from plato._generated.models import CreateTestCaseRequest

        from .skill_ingestion import _slugify as _slug

        skill_tag = _slug(vs.short_name or vs.skill_name)
        tc_tags = ["skill-test-generator", skill_tag]

        async with httpx.AsyncClient(
            base_url=config.plato_api_url,
            timeout=httpx.Timeout(60.0),
        ) as http:
            simulator_id: int | None = None
            try:
                sid_resp = await get_simulator_id.asyncio(
                    client=http,
                    simulator_name=sim_name,
                    x_api_key=config.plato_api_key,
                )
                simulator_id = sid_resp.simulator_id
                logger.info("  Resolved sim '%s' -> id %d", sim_name, simulator_id)
            except Exception as e:
                logger.warning(
                    "  Could not resolve simulator_id for '%s': %s", sim_name, e
                )

            for task in tasks:
                scoring_type = task.get("scoring_type", "output")
                v2_scoring_config = _build_v2_scoring_config(task, sim_name)

                tc_name = f"{vs.slug}--{task.get('name', 'unnamed')}"
                req = CreateTestCaseRequest(
                    name=tc_name,
                    prompt=task.get("instruction", ""),
                    start_url=task.get("start_url", "/"),
                    simulator_artifact_ids=[artifact_id],
                    simulator_id=simulator_id,
                    tags=tc_tags,
                )

                if v2_scoring_config:
                    req.v2_scoring_config = v2_scoring_config
                if scoring_type == "output" and task.get("output_schema"):
                    req.output_schema = task["output_schema"]

                try:
                    resp = await create_testcase.asyncio(
                        client=http,
                        body=req,
                        x_api_key=config.plato_api_key,
                    )
                    tc = resp.test_case
                    tc_id = (
                        tc.get("publicId")
                        or tc.get("public_id")
                        or str(tc.get("id", ""))
                    )
                    vs.testcase_ids.append(tc_id)
                    task["_testcase_id"] = tc_id
                    logger.info("  Created testcase '%s' -> %s", tc_name, tc_id)
                except Exception as e:
                    logger.error("  Testcase creation error for '%s': %s", tc_name, e)

    # ------------------------------------------------------------------
    # RUN: launch CUA benchmark sessions via Chronos, wait for completion
    # ------------------------------------------------------------------

    async def _run_agent_eval(self) -> None:
        config = self.config

        if config.sessions_per_testcase <= 0:
            logger.info("Skipping RUN stage: sessions_per_testcase is 0")
            return

        if not self._all_tasks:
            self._load_tasks_from_disk()

        from plato.chronos.api.jobs import launch_job
        from plato.chronos.api.sessions import get_session, get_session_status
        from plato.chronos.models import (
            LaunchJobRequest,
            VMResources,
            WorldConfigInput,
            WorldRuntimeConfig,
        )

        sem = asyncio.Semaphore(config.run_concurrency)
        n_sessions = config.sessions_per_testcase

        work_items: list[tuple] = []
        for vs in self.state.variants:
            if not vs.testcase_ids:
                logger.info("Skipping %s: no testcases", vs.slug)
                continue
            tasks = self._all_tasks.get(vs.slug, [])
            logger.info(
                "Queuing %d testcases × %d sessions for skill: %s",
                len(vs.testcase_ids),
                n_sessions,
                vs.skill_name,
            )
            for i, tc_id in enumerate(vs.testcase_ids):
                task = tasks[i] if i < len(tasks) else {}
                task_name = task.get("name", f"task-{i}")
                for session_num in range(1, n_sessions + 1):
                    work_items.append((vs, tc_id, task, task_name, session_num))

        logger.info(
            "RUN phase: %d total sessions (semaphore=%d)",
            len(work_items),
            config.run_concurrency,
        )

        async with httpx.AsyncClient(
            base_url=config.chronos_url, timeout=httpx.Timeout(120.0)
        ) as http:

            async def _run_one(
                idx: int,
                vs: VariantStatus,
                tc_id: str,
                task: dict,
                task_name: str,
                session_num: int,
            ) -> None:
                stagger_delay = idx * 2.0
                if stagger_delay > 0:
                    await asyncio.sleep(stagger_delay)
                async with sem:
                    session_label = (
                        f"{task_name} [{session_num}/{n_sessions}] (testcase {tc_id})"
                    )
                    logger.info("  Launching: %s", session_label)
                    try:
                        result = await self._launch_and_wait(
                            http,
                            config,
                            tc_id,
                            task_name,
                            launch_job,
                            get_session_status,
                            get_session,
                            LaunchJobRequest,
                            WorldConfigInput,
                            WorldRuntimeConfig,
                            VMResources,
                        )
                        result["session_num"] = session_num
                        vs.chronos_session_ids.append(result.get("chronos_id", ""))
                        vs.plato_session_ids.append(result.get("plato_id", ""))
                        vs.task_results.append(result)

                        status_str = "PASS" if result.get("score", 0) > 0 else "FAIL"
                        logger.info(
                            "    %s | score=%.2f | chronos=%s | plato=%s",
                            status_str,
                            result.get("score", 0),
                            result.get("chronos_url", ""),
                            result.get("plato_url", ""),
                        )
                    except Exception as e:
                        logger.error("    Failed to run testcase %s: %s", tc_id, e)
                        vs.task_results.append(
                            {
                                "testcase_id": tc_id,
                                "task_name": task_name,
                                "session_num": session_num,
                                "status": "error",
                                "error": str(e),
                            }
                        )

            await asyncio.gather(
                *[
                    _run_one(i, vs, tc_id, task, tn, sn)
                    for i, (vs, tc_id, task, tn, sn) in enumerate(work_items)
                ]
            )

        for vs in self.state.variants:
            if vs.testcase_ids:
                vs.stage = "evaluated"

    async def _launch_and_wait(
        self,
        http,
        config,
        tc_id,
        task_name,
        launch_job_mod,
        get_session_status_mod,
        get_session_mod,
        LaunchJobRequest,
        WorldConfigInput,
        WorldRuntimeConfig,
        VMResources,
    ) -> dict:
        """Launch a single CUA benchmark session and wait for it to complete."""
        agent_config: dict = {
            "model_name": config.eval_agent_model,
            "max_turns": config.eval_max_turns,
            "display_width": config.eval_display_width,
            "display_height": config.eval_display_height,
        }

        if config.eval_agent_model == "amazon/nova-act":
            if config.nova_act_api_key:
                agent_config["nova_act_api_key"] = config.nova_act_api_key
            else:
                agent_config["nova_act_workflow_name"] = config.nova_act_workflow_name
        elif "anthropic" in config.eval_agent_model or "claude" in config.eval_agent_model:
            if config.anthropic_api_key:
                agent_config["anthropic_api_key"] = config.anthropic_api_key

        if config.aws_access_key_id:
            agent_config["aws_access_key_id"] = config.aws_access_key_id
            agent_config["envgen_aws_access_key_id"] = config.aws_access_key_id
        if config.aws_secret_access_key:
            agent_config["aws_secret_access_key"] = config.aws_secret_access_key
            agent_config["envgen_aws_secret_access_key"] = config.aws_secret_access_key
        if config.aws_session_token:
            agent_config["aws_session_token"] = config.aws_session_token
            agent_config["envgen_aws_session_token"] = config.aws_session_token
        agent_config.setdefault("envgen_aws_region", "us-west-1")
        agent_config.setdefault(
            "envgen_aws_s3_bucket", "plato-browser-session-data-prod"
        )

        if config.record_sessions:
            agent_config["record_screen"] = True
            agent_config["use_extension_recorder"] = True

        world_config = {
            "version": "2",
            "task_id": tc_id,
            "envs": [],
            "record_session": config.record_sessions,
            "login_flow": True,
            "login_flow_retries": 4,
            "login_flow_retry_delay_ms": 10000,
            "agent": {
                "package": config.cua_agent_package,
                "config": agent_config,
                "runtime": {
                    "type": "vm",
                    "vm": {
                        "cpus": 2,
                        "memory": 4096,
                        "timeout": 3600,
                    },
                },
            },
            "plato_api_key": config.plato_api_key,
        }

        request = LaunchJobRequest(
            world=WorldConfigInput(
                package=config.cua_world_package,
                runtime=WorldRuntimeConfig(
                    type="vm",
                    vm=VMResources(cpus=2, memory=4096),
                ),
                config=world_config,
            ),
        )

        resp = await launch_job_mod.asyncio(
            client=http, body=request, x_api_key=config.plato_api_key
        )
        chronos_id = resp.session_id
        logger.info("    Session launched: %s", chronos_id)

        status = await self._poll_until_done(http, chronos_id, config.plato_api_key)

        return {
            "testcase_id": tc_id,
            "task_name": task_name,
            "chronos_id": chronos_id,
            "chronos_url": f"{config.chronos_url}/sessions/{chronos_id}",
            "plato_id": status.get("plato_session_id", ""),
            "plato_url": f"{config.plato_api_url}/sessions/{status.get('plato_session_id', '')}"
            if status.get("plato_session_id")
            else "",
            "status": status.get("status", "unknown"),
            "score": status.get("score", 0),
            "scoring_details": status.get("scoring_details"),
            "error": status.get("error"),
        }

    async def _poll_until_done(
        self,
        http,
        session_id,
        api_key,
        timeout=1800,
        poll_interval=15,
    ) -> dict:
        """Poll Chronos session status until terminal."""
        terminal = {"completed", "failed", "cancelled", "error"}
        elapsed = 0.0

        while elapsed < timeout:
            try:
                raw = await http.get(
                    f"/api/sessions/{session_id}",
                    headers={"X-API-Key": api_key},
                )
                if raw.status_code == 200:
                    data = raw.json()
                    status = data.get("status", "")
                    if status in terminal:
                        plato_session_id = data.get("plato_session_id")
                        score_data: dict = {"score": 0.0, "scoring_details": None}
                        error_msg = (
                            data.get("status_reason") if status == "failed" else None
                        )

                        if plato_session_id:
                            score_data = await self._get_session_score(
                                plato_session_id, api_key
                            )

                        return {
                            "status": status,
                            "plato_session_id": plato_session_id,
                            "score": score_data["score"],
                            "scoring_details": score_data.get("scoring_details"),
                            "error": error_msg,
                        }
                    if elapsed % 60 < poll_interval:
                        logger.info(
                            "    Session %s: %s (%.0fs)", session_id, status, elapsed
                        )
            except Exception as e:
                logger.warning("Poll error for %s: %s", session_id, e)

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        raise TimeoutError(f"Session {session_id} did not complete within {timeout}s")

    async def _get_session_score(self, plato_session_id: str, api_key: str) -> dict:
        """Fetch evaluation score and scoring details for a Plato session.

        Returns dict with 'score' (float) and 'scoring_details' (full config+result
        breakdown) so callers can compare what was expected vs what happened.
        """
        result: dict = {"score": 0.0, "scoring_details": None}
        try:
            api_url = self.config.plato_api_url
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{api_url}/api/evals/scoring/{plato_session_id}",
                    headers={"X-API-Key": api_key},
                )
                if resp.status_code == 200:
                    scores = resp.json()
                    result["scoring_details"] = scores

                    v2 = next(
                        (
                            s
                            for s in scores
                            if s.get("config", {}).get("type") == "v2_evaluate"
                        ),
                        None,
                    )
                    if v2:
                        result["score"] = v2.get("result", {}).get("score", 0.0)
                        return result
                    js = next(
                        (
                            s
                            for s in scores
                            if s.get("config", {}).get("type") == "json_schema"
                        ),
                        None,
                    )
                    if js:
                        result["score"] = js.get("result", {}).get("score", 0.0)
                        return result
        except Exception as e:
            logger.warning("Failed to get score for %s: %s", plato_session_id, e)
        return result

    # ------------------------------------------------------------------
    # EVALUATE: collect and summarize results
    # ------------------------------------------------------------------

    async def _run_evaluate(self) -> None:
        config = self.config

        if config.sessions_per_testcase <= 0:
            logger.info("Skipping EVALUATE stage: sessions_per_testcase is 0")
            return

        final_results = []
        for vs in self.state.variants:
            skill_result = {
                "skill_name": vs.skill_name,
                "slug": vs.slug,
                "sim_name": vs.sim_name,
                "artifact_id": vs.artifact_id,
                "testcase_count": len(vs.testcase_ids),
                "tasks": [],
            }

            passed = 0
            total = 0
            for tr in vs.task_results:
                total += 1
                is_pass = tr.get("score", 0) > 0
                if is_pass:
                    passed += 1

                task_entry: dict = {
                    "task_name": tr.get("task_name", ""),
                    "testcase_id": tr.get("testcase_id", ""),
                    "status": tr.get("status", ""),
                    "score": tr.get("score", 0),
                    "pass": is_pass,
                    "chronos_url": tr.get("chronos_url", ""),
                    "plato_url": tr.get("plato_url", ""),
                    "error": tr.get("error"),
                }

                scoring_details = tr.get("scoring_details")
                if scoring_details:
                    task_entry["scoring_details"] = scoring_details

                skill_result["tasks"].append(task_entry)

            skill_result["pass_rate"] = f"{passed}/{total}" if total else "0/0"
            skill_result["pass_pct"] = round(passed / total * 100, 1) if total else 0
            final_results.append(skill_result)

        (config.output / "final_results.json").write_text(
            json.dumps(final_results, indent=2)
        )

        logger.info("=" * 60)
        logger.info("FINAL RESULTS")
        logger.info("=" * 60)
        for r in final_results:
            logger.info(
                "  %s: %s (%.1f%%)",
                r["skill_name"],
                r["pass_rate"],
                r["pass_pct"],
            )
            for t in r["tasks"]:
                pflag = "PASS" if t["pass"] else "FAIL"
                logger.info(
                    "    [%s] %s | score=%.2f | chronos=%s | plato=%s",
                    pflag,
                    t["task_name"],
                    t["score"],
                    t.get("chronos_url", ""),
                    t.get("plato_url", ""),
                )
                details = t.get("scoring_details")
                if details and not t["pass"]:
                    for entry in details:
                        cfg = entry.get("config", {})
                        res = entry.get("result", {})
                        logger.info(
                            "      scoring_config: type=%s | result: score=%.2f reason=%s",
                            cfg.get("type", "?"),
                            res.get("score", 0),
                            res.get("reason", "n/a"),
                        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_skills_from_disk(self) -> None:
        p = self.config.output / "skills.json"
        if p.exists():
            self._skills = [
                SkillDefinition.model_validate(s) for s in json.loads(p.read_text())
            ]

    def _load_variant_specs_from_disk(self) -> None:
        p = self.config.output / "variant_specs.json"
        if p.exists():
            self._variant_specs = json.loads(p.read_text())

    def _load_tasks_from_disk(self) -> None:
        if not self._variant_specs:
            self._load_variant_specs_from_disk()
        for spec in self._variant_specs:
            slug = spec.get("slug", spec.get("_generation", {}).get("slug", "unknown"))
            p = self.config.output / slug / "tasks.json"
            if p.exists():
                self._all_tasks[slug] = json.loads(p.read_text()).get("tasks", [])

    def _build_summary(self) -> str:
        lines = [
            "=" * 60,
            "SKILL TEST GENERATOR — SUMMARY",
            "=" * 60,
            f"Skills: {self.state.skills_loaded}",
            "",
        ]
        for vs in self.state.variants:
            status = f"[{vs.stage}]"
            tasks = f" ({vs.task_count} tasks)" if vs.task_count else ""
            passed = sum(1 for t in vs.task_results if t.get("score", 0) > 0)
            total = len(vs.task_results)
            rate = f" pass_rate={passed}/{total}" if total else ""
            err = f" ERROR: {vs.error}" if vs.error else ""
            lines.append(f"  {status} {vs.skill_name}{tasks}{rate}{err}")
            for tr in vs.task_results:
                pflag = "PASS" if tr.get("score", 0) > 0 else "FAIL"
                lines.append(
                    f"    [{pflag}] {tr.get('task_name', '')} "
                    f"chronos={tr.get('chronos_url', '')} "
                    f"plato={tr.get('plato_url', '')}"
                )
        return "\n".join(lines)

    async def close(self) -> None:
        await super().close()
