"""Skill Test Generator World — full pipeline from skill gaps to running evaluations."""

from __future__ import annotations

import asyncio
import importlib.resources
import io
import json
import logging
import re
import tarfile
from pathlib import Path

import httpx
from plato.worlds import BaseWorld, Observation, StepResult, register_world

from .config import (
    IterationRecord,
    SkillDefinition,
    SkillTestGeneratorConfig,
    SkillTestGeneratorState,
    Stage,
    TestcaseHillclimbState,
    TestcaseIterationRecord,
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

        if not config.anthropic_api_key and not config.resume_variants:
            errors.append("anthropic_api_key is empty")
        elif config.anthropic_api_key:
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

        if (
            not config.s3_skills
            and not config.custom_skills
            and not config.resume_variants
        ):
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
                has_results = bool(rv.get("task_results"))
                rv_slug = rv["slug"]
                rv_vk = rv.get("variant_key", rv_slug)
                vs = VariantStatus(
                    skill_name=rv["skill_name"],
                    short_name=rv.get("short_name", ""),
                    slug=re.sub(r"-(?:v?\d+)$", "", rv_slug),
                    variant_key=rv_vk,
                    sim_name=rv.get("sim_name", ""),
                    artifact_id=rv.get("artifact_id", ""),
                    testcase_ids=rv.get("testcase_ids", []),
                    task_count=len(rv.get("testcase_ids", [])),
                    task_results=rv.get("task_results", []),
                    chronos_session_ids=rv.get("chronos_session_ids", []),
                    stage="evaluated" if has_results else "published",
                )
                state.variants.append(vs)
            state.skills_loaded = len({vs.skill_name for vs in state.variants})

        all_stages = [
            Stage.INGEST,
            Stage.DESIGN,
            Stage.CODEGEN,
            Stage.RUN,
            Stage.EVALUATE,
            Stage.HILLCLIMB,
        ]
        if config.resume_variants:
            has_existing_results = any(
                rv.get("task_results") for rv in config.resume_variants
            )
            if has_existing_results and config.hillclimb.enabled:
                stages = [Stage.HILLCLIMB]
            elif has_existing_results:
                stages = [Stage.EVALUATE]
            else:
                resume_stages = [Stage.RUN, Stage.EVALUATE]
                if config.hillclimb.enabled:
                    resume_stages.append(Stage.HILLCLIMB)
                stages = resume_stages
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
            Stage.HILLCLIMB: self._run_hillclimb,
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

        if config.dry_run:
            await self._cleanup_dry_run()

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
                    VariantStatus(
                        skill_name=s.name,
                        short_name=short,
                        slug=base_slug,
                        variant_key=base_slug,
                    )
                )
            else:
                for vi in range(1, sps + 1):
                    variants.append(
                        VariantStatus(
                            skill_name=s.name,
                            short_name=short,
                            slug=base_slug,
                            variant_key=f"{base_slug}-{vi}",
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
            concurrency=config.design_concurrency,
            specs_per_skill=config.specs_per_skill,
        )
        (config.output / "variant_specs.json").write_text(
            json.dumps(self._variant_specs, indent=2, default=str)
        )
        for vs in self.state.variants:
            match = [s for s in self._variant_specs if s.get("slug") == vs.variant_key]
            vs.stage = "designed" if match else "design_failed"
            if match:
                app_name = match[0].get("app_name", "").strip()
                app_name = re.sub(r"[^a-z0-9-]", "", app_name.lower().replace(" ", "-"))
                app_name = app_name or config.sim_name_prefix
                vs.sim_name = f"{app_name}-{vs.slug}"

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

        Three independent semaphores gate concurrency:
          • llm_sem  — concurrent LLM API calls  (config.design_concurrency)
          • vm_sem   — concurrent pipeline VMs    (config.vm_concurrency)
          • av_sem   — concurrent autoverify sessions (config.run_concurrency)
        """
        import anthropic as _anthropic

        from .codegen_agent import build_codegen_instruction
        from .task_generator import build_plato_task_configs, generate_tasks_for_variant
        from .variant_generator import (
            _copy_sohan_template,
            _load_reference_manifest,
            _resolve_reference_screenshots_dir,
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
        llm_sem = asyncio.Semaphore(config.design_concurrency)
        vm_sem = asyncio.Semaphore(config.vm_concurrency)
        av_sem = asyncio.Semaphore(config.run_concurrency)

        eligible = [vs for vs in self.state.variants if vs.stage == "designed"]
        logger.info(
            "Parallel pipelines: %d variants  (llm_concurrency=%d, max_vms=%d)",
            len(eligible),
            config.design_concurrency,
            config.vm_concurrency,
        )

        async def _single_pipeline(vs: VariantStatus) -> None:
            spec = next(
                (s for s in self._variant_specs if s.get("slug") == vs.variant_key),
                None,
            )
            if not spec:
                vs.stage = "pipeline_failed"
                vs.error = "no spec found"
                return

            try:
                await _single_pipeline_inner(vs, spec)
            except Exception as e:
                logger.error("  [%s] Unhandled pipeline error: %s", vs.variant_key, e)
                if vs.stage != "published":
                    vs.stage = "pipeline_failed"
                    vs.error = vs.error or f"unhandled: {e}"

        async def _single_pipeline_inner(vs: VariantStatus, spec: dict) -> None:
            variant_dir = variants_dir / vs.variant_key
            sim_name = vs.sim_name
            if not sim_name:
                app_name = spec.get("app_name", "").strip()
                app_name = re.sub(r"[^a-z0-9-]", "", app_name.lower().replace(" ", "-"))
                app_name = app_name or config.sim_name_prefix
                sim_name = f"{app_name}-{vs.slug}"
            vs.sim_name = sim_name

            # ── Phase 1: LLM codegen ──────────────────────────────────
            _copy_sohan_template(template_source, variant_dir)
            variant_dir.mkdir(parents=True, exist_ok=True)
            (variant_dir / "spec.json").write_text(json.dumps(spec, indent=2))

            icon_svg = spec.get("icon_svg", "")
            if icon_svg and icon_svg.strip().startswith("<svg"):
                (variant_dir / "icon.svg").write_text(icon_svg)

            files_written: list[str] = []
            validation_errors: list[str] = []
            async with llm_sem:
                try:
                    logger.info("  [%s] One-shot codegen …", vs.variant_key)
                    ref_screenshot: tuple[dict, bytes] | None = None
                    ref_filename = spec.get("_reference_screenshot", "")
                    if ref_filename:
                        ref_dir = _resolve_reference_screenshots_dir()
                        if ref_dir:
                            ref_path = ref_dir / ref_filename
                            if ref_path.exists():
                                manifest = _load_reference_manifest()
                                ref_entry = next(
                                    (
                                        e
                                        for e in manifest
                                        if e["filename"] == ref_filename
                                    ),
                                    None,
                                )
                                if ref_entry:
                                    ref_screenshot = (ref_entry, ref_path.read_bytes())
                                    logger.info(
                                        "  [%s] Codegen using same ref: %s (%s)",
                                        vs.variant_key,
                                        ref_filename,
                                        ref_entry.get("content_type"),
                                    )
                    code_files = await generate_variant_code(
                        llm_client,
                        spec,
                        config.design_model,
                        reference_screenshot=ref_screenshot,
                    )
                    validation_errors = _validate_code_files(code_files)
                    files_written = apply_variant_code(
                        variant_dir,
                        code_files,
                        spec=spec,
                    )
                    logger.info(
                        "  [%s] Wrote %d files", vs.variant_key, len(files_written)
                    )
                except Exception as e:
                    logger.error("  [%s] Codegen failed: %s", vs.variant_key, e)
                    vs.stage = "pipeline_failed"
                    vs.error = f"codegen: {e}"
                    return

            if validation_errors:
                logger.warning(
                    "  [%s] Validation warnings: %s", vs.variant_key, validation_errors
                )

            # ── Phase 2: Pipeline VM (verify → build → snapshot) ──
            artifact_id: str | None = None
            last_checks: list[dict] = []
            live_api_data: dict[str, str] = {}

            for attempt in range(3):
                tarball = self._tar_variant(variant_dir, sim_name)
                url = self._upload_to_s3(
                    tarball,
                    f"{S3_PREFIX}/{sim_name}-pipe-{attempt}.tar.gz",
                )

                async with vm_sem:
                    logger.info(
                        "  [%s] Pipeline VM attempt %d …", vs.variant_key, attempt + 1
                    )
                    try:
                        result = await self._pipeline_vm_verify_build_publish(
                            vs=vs,
                            sim_name=sim_name,
                            spec=spec,
                            presigned_url=url,
                        )
                    except Exception as e:
                        logger.error("  [%s] Pipeline VM error: %s", vs.variant_key, e)
                        result = {
                            "artifact_id": None,
                            "verified": False,
                            "checks": [],
                            "failure_type": "infra",
                        }

                last_checks = result.get("checks", [])
                failure_type = result.get("failure_type", "infra")

                if result.get("artifact_id"):
                    artifact_id = result["artifact_id"]
                    live_api_data = result.get("live_api_data", {})
                    break

                if failure_type == "code" and attempt == 0 and config.coder_agent:
                    logger.info(
                        "  [%s] Code error, launching agent fix …", vs.variant_key
                    )
                    try:
                        instruction = build_codegen_instruction(
                            spec=spec,
                            slug=vs.variant_key,
                            variant_dir=f"{agent_prefix}/variants/{vs.variant_key}",
                            verify_port=config.codegen_verify_port,
                            files_written=files_written,
                            validation_errors=validation_errors,
                            deps_installed=False,
                            check_results=last_checks,
                        )
                        runner = self.agent(
                            config.coder_agent,
                            display_name=f"fix-{vs.variant_key}",
                            workspaces=[self.workspace("code")],
                        )
                        await runner.run(instruction=instruction)
                        logger.info("  [%s] Agent fix complete", vs.variant_key)
                    except Exception as e:
                        logger.error("  [%s] Agent fix error: %s", vs.variant_key, e)
                        break
                elif failure_type == "infra":
                    logger.info(
                        "  [%s] Infra error (attempt %d), retrying VM …",
                        vs.variant_key,
                        attempt + 1,
                    )
                    continue
                else:
                    break

            if not artifact_id:
                vs.stage = "pipeline_failed"
                vs.error = "no artifact after all attempts"
                return

            vs.artifact_id = artifact_id
            logger.info("  [%s] Artifact: %s", vs.variant_key, artifact_id)

            # ── Phase 2b: Task generation (off-VM, uses fetched API data) ──
            generated_tasks: list[dict] = []
            if llm_client is not None:
                if not live_api_data:
                    logger.warning(
                        "  [%s] No live API data — falling back to spec-only task generation",
                        vs.variant_key,
                    )
                gen_coro = generate_tasks_for_variant(
                    llm_client,
                    spec,
                    config.design_model,
                    output_tasks=config.output_tasks_per_variant,
                    mutation_tasks=config.mutation_tasks_per_variant,
                    live_api_data=live_api_data or None,
                )
                if llm_sem is not None:
                    async with llm_sem:
                        generated_tasks = await gen_coro
                else:
                    generated_tasks = await gen_coro

                logger.info(
                    "  [%s] Generated %d tasks (off-VM%s)",
                    vs.variant_key,
                    len(generated_tasks),
                    "" if live_api_data else ", spec-only fallback",
                )

            if generated_tasks:
                self._all_tasks[vs.variant_key] = generated_tasks
                vs.task_count = len(generated_tasks)
                tasks_dir = config.output / vs.variant_key
                tasks_dir.mkdir(parents=True, exist_ok=True)
                (tasks_dir / "tasks.json").write_text(
                    json.dumps({"tasks": generated_tasks}, indent=2),
                )

            # ── Phase 2c+3: Autoverify (optional) + Create testcases ──
            tasks = self._all_tasks.get(vs.variant_key, [])
            if tasks and artifact_id:
                try:
                    new_ids = await self._autoverify_and_publish(
                        vs,
                        tasks,
                        artifact_id,
                        av_sem=av_sem,
                    )
                    if new_ids:
                        vs.testcase_ids.extend(new_ids)
                        vs.stage = "published"
                    else:
                        vs.stage = "pipeline_failed"
                        vs.error = "no testcases passed autoverify"
                except Exception as e:
                    logger.error(
                        "  [%s] Testcase creation error: %s", vs.variant_key, e
                    )
                    vs.stage = "pipeline_failed"
                    vs.error = f"testcase creation: {e}"
            else:
                vs.stage = "pipeline_failed"
                vs.error = "no tasks for testcase creation"

            logger.info("  [%s] Pipeline complete → %s", vs.variant_key, vs.stage)

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
                        "slug": vs.variant_key,
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
        """Run verify + build + snapshot on a pipeline VM.

        Also fetches live API data for task generation that runs after the VM
        is closed.

        Returns ``{"artifact_id": str|None, "verified": bool, "checks": [...],
        "live_api_data": {...}}``.
        The VM is always closed in the ``finally`` block.
        """
        from plato._generated.api.v2.sessions import (
            close as sessions_close,
            make as sessions_make,
        )
        from plato._generated.models import (
            AppSchemasBuildModelsSimConfigCompute as SimConfigCompute,
            CreateSessionFromEnvs,
            EnvFromResource,
            Envs,
            RunSessionSource,
        )

        config = self.config
        api_key = config.plato_api_key
        api_url = config.plato_api_url
        session_id: str | None = None
        checks: list[dict] = []
        api_routes = [r for r in spec.get("api_routes", []) if isinstance(r, dict)]

        async with httpx.AsyncClient(
            base_url=api_url,
            timeout=httpx.Timeout(300.0, connect=30.0),
        ) as http:
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
                logger.info("  [%s] VM %s created", vs.variant_key, session_id)

                _exec = self._make_exec_fn(http, session_id, api_key)
                await self._poll_vm_ready(http, session_id, api_key, vs.variant_key)

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

                # Ensure `node` is on PATH — Next.js Turbopack spawns node
                # subprocesses for webpack loaders even when run via bun
                await _exec(
                    f"{preamble} && "
                    "command -v node >/dev/null 2>&1 || "
                    'ln -sf "$(command -v bun)" /usr/local/bin/node',
                    timeout=10,
                )

                out, _ = await _exec(
                    f"{preamble} && cd {app_dir} && bun install 2>&1 | tail -10",
                    timeout=300,
                )
                logger.info("  [%s] bun install: %s", vs.variant_key, out[-300:])

                # Verify critical dependency is installed; force-install if missing
                chk, _ = await _exec(
                    f"test -d {app_dir}/node_modules/@electric-sql/pglite && echo OK || echo MISSING",
                    timeout=10,
                )
                if "MISSING" in chk:
                    logger.warning(
                        "  [%s] @electric-sql/pglite missing, reinstalling …",
                        vs.variant_key,
                    )
                    await _exec(
                        f"{preamble} && cd {app_dir} && bun install --no-save @electric-sql/pglite 2>&1 | tail -5",
                        timeout=120,
                    )

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

                # ── BUILD (production) ─────────────────────────────────
                await _exec("fuser -k 3000/tcp 2>/dev/null; sleep 1", timeout=10)

                # Clean any stale dev-mode .next artifacts before building
                await _exec(f"rm -rf {app_dir}/.next", timeout=10)

                build_out, build_ok = await _exec(
                    f"{preamble} && cd {app_dir} && "
                    "NODE_ENV=production NEXT_DIST_DIR=.next "
                    "bun ./node_modules/next/dist/bin/next build 2>&1 | tail -40",
                    timeout=300,
                )
                logger.info("  [%s] Build: %s", vs.variant_key, build_out[-300:])

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
                    logger.error("  [%s] %s", vs.variant_key, err)
                    return {
                        "artifact_id": None,
                        "verified": False,
                        "checks": checks,
                        "failure_type": "code",
                    }
                checks.append({"name": "production_build", "pass": True, "error": ""})

                # ── VERIFY (production server) ────────────────────────
                await _exec(
                    f"{preamble} && cd {app_dir} && mkdir -p /tmp/pglite-data && "
                    f"NEXT_DIST_DIR=.next NODE_ENV=production PORT=3000 APP_PORT=3000 "
                    f"nohup bun ./node_modules/next/dist/bin/next start "
                    f"--hostname 0.0.0.0 -p 3000 > /tmp/dev.log 2>&1 &",
                    timeout=30,
                )

                verify_checks = await self._verify_sim_on_vm(
                    _exec, api_routes, vs.variant_key
                )
                checks.extend(verify_checks)

                if not all(c["pass"] for c in checks):
                    return {
                        "artifact_id": None,
                        "verified": False,
                        "checks": checks,
                        "failure_type": "code",
                    }

                # ── FETCH LIVE API DATA (for taskgen after VM closes) ──
                live_api_data: dict[str, str] = {}

                list_routes = []
                dynamic_routes = []
                for r in api_routes:
                    route = r.get("route", "")
                    if not route or "/health" in route:
                        continue
                    methods = r.get("methods", [r.get("method", "GET")])
                    if isinstance(methods, str):
                        methods = [methods]
                    if "GET" not in methods:
                        continue
                    if "[" in route:
                        dynamic_routes.append(route)
                    else:
                        list_routes.append(route)

                for route in list_routes:
                    all_data = ""
                    page = 1
                    while page <= 20:
                        url_with_page = (
                            f"http://127.0.0.1:3000{route}?page={page}&pageSize=100"
                        )
                        out, ok = await _exec(
                            f"curl -s '{url_with_page}' 2>&1",
                            timeout=15,
                        )
                        if not ok or not out or len(out) < 5:
                            break
                        if page == 1:
                            all_data = out
                        else:
                            all_data += "\n" + out
                        try:
                            resp_json = json.loads(out)
                            if isinstance(resp_json, list):
                                resp_json = {"data": resp_json}
                            if not isinstance(resp_json, dict):
                                break
                            pagination = resp_json.get("pagination")
                            if not isinstance(pagination, dict):
                                pagination = {}
                            tp = (
                                resp_json.get("totalPages")
                                or resp_json.get("total_pages")
                                or pagination.get("totalPages")
                                or pagination.get("total_pages")
                            )
                            if tp and page >= int(tp):
                                break
                            rows = resp_json.get("data", resp_json.get("items", []))
                            if not rows:
                                break
                        except (json.JSONDecodeError, ValueError):
                            break
                        page += 1
                    if all_data:
                        live_api_data[route] = all_data

                # Fallback: probe dynamic routes with common IDs
                if not live_api_data and dynamic_routes:
                    logger.info(
                        "  [%s] No list endpoints — probing dynamic routes with sample IDs",
                        vs.variant_key,
                    )
                    for route in dynamic_routes:
                        for sample_id in ("1", "2", "3"):
                            concrete = re.sub(r"\[[^\]]+\]", sample_id, route)
                            out, ok = await _exec(
                                f"curl -s 'http://127.0.0.1:3000{concrete}' 2>&1",
                                timeout=10,
                            )
                            if ok and out and len(out) > 5:
                                try:
                                    json.loads(out)
                                    key = f"{route} (id={sample_id})"
                                    live_api_data[key] = out
                                except (json.JSONDecodeError, ValueError):
                                    pass

                if live_api_data:
                    logger.info(
                        "  [%s] Fetched live API data from %d route(s)",
                        vs.variant_key,
                        len(live_api_data),
                    )

                # ── SEED API ROUTES (server still running from verify) ──
                await self._seed_api_routes(
                    _exec, api_routes, vs.variant_key, retries=2
                )

                # ── BOOT SERVICE (so app starts on snapshot restore) ──
                svc_exec = (
                    "/root/.bun/bin/bun ./node_modules/next/dist/bin/next start "
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
                logger.info("  [%s] Installed nextapp.service for boot", vs.variant_key)

                # ── ENSURE SIMULATOR CATALOG ENTRY EXISTS ─────────────
                skill_def = next(
                    (s for s in self._skills if s.name == vs.skill_name), None
                )
                skill_desc = skill_def.description if skill_def else ""
                sim_description = (
                    f"[skill: {vs.skill_name}] {skill_desc}"
                    f" | {spec.get('description', '') or ''}"
                ).strip()
                actual_name = await self._register_simulator(
                    http,
                    api_key,
                    sim_name,
                    sim_description,
                    icon_svg=spec.get("icon_svg", ""),
                    label=vs.variant_key,
                    skill_gym=vs.skill_name,
                )
                if actual_name != sim_name:
                    sim_name = actual_name
                    vs.sim_name = sim_name

                # ── SNAPSHOT ──────────────────────────────────────────
                flows_yaml = self._build_flows_yaml(vs.variant_key)
                artifact_id = await self._take_snapshot(
                    http, session_id, api_key, sim_name, flows_yaml, vs.variant_key
                )

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

                return {
                    "artifact_id": artifact_id,
                    "verified": True,
                    "checks": checks,
                    "live_api_data": live_api_data,
                }

            finally:
                if session_id:
                    try:
                        await sessions_close.asyncio(
                            client=http,
                            session_id=session_id,
                            x_api_key=api_key,
                        )
                        logger.info("  [%s] Pipeline VM closed", vs.variant_key)
                    except Exception:
                        pass

    def _derive_wait_selector(self, slug: str) -> str:
        """CSS selector for the login flow to confirm the app rendered.

        The template layout.tsx always wraps children in <main id="app-root">,
        so this is guaranteed to appear once the page hydrates.
        """
        return "#app-root"

    @staticmethod
    async def _verify_sim_on_vm(
        _exec,
        api_routes: list[dict],
        label: str,
    ) -> list[dict]:
        """Verify a running sim on a VM: health check, route checks, localhost check.

        Assumes the production server is already started on port 3000.
        Returns a list of check dicts with 'name', 'pass', 'error'.
        """
        checks: list[dict] = []

        prod_ok = False
        for _ in range(40):
            out, _ = await _exec(
                "curl -sf http://127.0.0.1:3000/api/health -o /dev/null "
                "&& echo OK || echo FAIL",
                timeout=10,
            )
            if "OK" in out:
                prod_ok = True
                break
            await asyncio.sleep(3)

        if not prod_ok:
            log_tail, _ = await _exec("tail -50 /tmp/dev.log 2>/dev/null", timeout=10)
            checks.append(
                {
                    "name": "server_startup",
                    "pass": False,
                    "error": f"Production server never healthy. Log: {log_tail[-500:]}",
                }
            )
            return checks

        checks.append({"name": "server_startup", "pass": True, "error": ""})
        checks.append({"name": "GET /api/health", "pass": True, "error": ""})

        for r in api_routes:
            route = r.get("route", "")
            if not route or "[" in route or "/health" in route:
                continue
            out, _ = await _exec(
                f"curl -s -w '\\nHTTP_CODE:%{{http_code}}' "
                f"http://127.0.0.1:3000{route} 2>&1 | tail -c 2000",
                timeout=15,
            )
            body = out.split("HTTP_CODE:")[0].strip() if "HTTP_CODE:" in out else out
            ok = "HTTP_CODE:200" in out and len(body) >= 2
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
            "localhost:3000/api" in page_html or "127.0.0.1:3000/api" in page_html
        )
        if has_localhost:
            checks.append(
                {
                    "name": "no_localhost_urls",
                    "pass": False,
                    "error": "Page HTML contains hardcoded localhost API URLs",
                }
            )
        else:
            checks.append({"name": "no_localhost_urls", "pass": True, "error": ""})

        n_pass = sum(1 for c in checks if c["pass"])
        n_fail = sum(1 for c in checks if not c["pass"])
        logger.info("  [%s] Verify: %d passed, %d failed", label, n_pass, n_fail)
        for c in checks:
            if not c["pass"]:
                logger.warning(
                    "  [%s] FAILED check '%s': %s",
                    label,
                    c["name"],
                    c.get("error", "")[:300],
                )

        return checks

    # ------------------------------------------------------------------
    # Shared VM / session helpers (used by both pipeline and hillclimb)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_exec_fn(http, session_id: str, api_key: str):
        """Create a bound _exec(cmd, timeout) closure for VM shell commands."""
        from plato._generated.api.v2.sessions import execute as sessions_execute
        from plato._generated.models import ExecuteCommandRequest

        async def _exec(cmd: str, timeout: int = 30) -> tuple[str, bool]:
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    r = await sessions_execute.asyncio(
                        client=http,
                        session_id=session_id,
                        body=ExecuteCommandRequest(command=cmd, timeout=timeout),
                        x_api_key=api_key,
                    )
                    for _, v in r.results.items():
                        return (v.stdout or "").strip(), bool(v.success)
                    return "", False
                except Exception as e:
                    err_str = str(e)
                    is_transient = any(
                        code in err_str for code in ("502", "503", "504")
                    )
                    if is_transient and attempt < max_retries - 1:
                        delay = 5 * (attempt + 1)
                        logger.warning(
                            "_exec transient error (attempt %d/%d), retrying in %ds: %s",
                            attempt + 1,
                            max_retries,
                            delay,
                            err_str[:200],
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise

        return _exec

    @staticmethod
    async def _poll_vm_ready(http, session_id: str, api_key: str, label: str) -> None:
        """Poll until the VM session reaches 'running' status."""
        for _ in range(60):
            await asyncio.sleep(3)
            sr = await http.get(
                f"/api/v2/sessions/{session_id}",
                headers={"X-API-Key": api_key},
            )
            jobs = sr.json().get("jobs", [{}])
            if jobs and jobs[0].get("status") == "running":
                logger.info("  [%s] VM running", label)
                return
        raise RuntimeError(f"[{label}] VM never reached running state")

    def _build_flows_yaml(self, slug: str) -> str:
        """Build the flows YAML for snapshot login flow."""
        wait_selector = self._derive_wait_selector(slug)
        return (
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

    @staticmethod
    async def _take_snapshot(
        http,
        session_id: str,
        api_key: str,
        sim_name: str,
        flows_yaml: str,
        label: str,
    ) -> str:
        """Call the snapshot API and return the artifact_id.

        Raises RuntimeError if the snapshot fails.
        """
        from plato._generated.api.v2.sessions import snapshot as sessions_snapshot
        from plato._generated.models import (
            AppApiV2SchemasSessionCreateSnapshotRequest,
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
            raise RuntimeError(f"[{label}] Snapshot failed: {errors}")

        logger.info("  [%s] Snapshot: %s", label, artifact_id)
        return artifact_id

    @staticmethod
    async def _seed_api_routes(
        _exec,
        api_routes: list[dict],
        label: str,
        *,
        retries: int = 1,
        hard_timeout: float = 120,
    ) -> None:
        """Hit non-dynamic API routes to seed the database before snapshot."""
        seed_routes = [
            r.get("route", "")
            for r in api_routes
            if r.get("route")
            and "/health" not in r.get("route", "")
            and "[" not in r.get("route", "")
        ]
        if not seed_routes and retries > 1:
            seed_routes = ["/api/items", "/api/data"]
        deadline = asyncio.get_event_loop().time() + hard_timeout
        for route in seed_routes:
            for attempt in range(retries):
                if asyncio.get_event_loop().time() > deadline:
                    logger.warning(
                        "  [%s] Seed hard timeout (%.0fs) reached, skipping remaining routes",
                        label,
                        hard_timeout,
                    )
                    await asyncio.sleep(1)
                    return
                out, _ = await _exec(
                    f"curl -s -w '\\nHTTP_CODE:%{{http_code}}' "
                    f"http://127.0.0.1:3000{route} 2>&1 | tail -c 2000",
                    timeout=15,
                )
                if "HTTP_CODE:200" in out and len(out) > 30:
                    if retries > 1:
                        logger.info(
                            "  [%s] Seed %s OK (attempt %d)",
                            label,
                            route,
                            attempt,
                        )
                    break
                if attempt < retries - 1:
                    logger.debug(
                        "  [%s] Seed %s attempt %d failed, retrying…",
                        label,
                        route,
                        attempt,
                    )
                    await asyncio.sleep(2)
                else:
                    logger.warning(
                        "  [%s] Seed %s failed after %d attempts, skipping",
                        label,
                        route,
                        retries,
                    )
        await asyncio.sleep(2)

    @staticmethod
    def _classify_session_outcome(result: dict) -> str:
        """Determine PASS/FAIL/ERROR from a session result dict."""
        status = result.get("status", "")
        if result.get("score", 0) > 0:
            return "PASS"
        if status in ("failed", "error", "cancelled"):
            return "ERROR"
        return "FAIL"

    @staticmethod
    def _task_dict_from_file(tc_file: Path) -> dict | None:
        """Build a task dict from a testcase JSON workspace file."""
        try:
            tc_data = json.loads(tc_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        return {
            "name": tc_data.get("name", tc_file.stem),
            "instruction": tc_data.get("instruction", ""),
            "hint": tc_data.get("hint", ""),
            "start_url": tc_data.get("start_url", "/"),
            "scoring_type": tc_data.get("scoring_type", "output"),
            "output_schema": tc_data.get("output_schema"),
            "expected_output": tc_data.get("expected_output"),
            "scoring_config": tc_data.get("scoring_config"),
            "expected_mutations": tc_data.get("expected_mutations"),
        }

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

    def _upload_to_s3(self, data: bytes, key: str, expires: int = 3600) -> str:
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
            ExpiresIn=expires,
        )
        logger.info("  Uploaded to s3://%s/%s", S3_BUCKET, key)
        return url

    def _download_sim_from_s3(self, sim_name: str, dest: Path) -> None:
        """Download sim source tarball from S3 for resume/hillclimb scenarios.

        Tries the pipeline tarball keys (pipe-0, pipe-1, pipe-2) until one is
        found, then extracts the ``web/`` subtree into *dest*.
        """
        import boto3

        config = self.config
        kwargs: dict = {"region_name": "us-west-1"}
        if config.aws_access_key_id:
            kwargs["aws_access_key_id"] = config.aws_access_key_id
            kwargs["aws_secret_access_key"] = config.aws_secret_access_key
            if config.aws_session_token:
                kwargs["aws_session_token"] = config.aws_session_token
        s3 = boto3.Session(**kwargs).client("s3")

        for attempt in range(3):
            key = f"{S3_PREFIX}/{sim_name}-pipe-{attempt}.tar.gz"
            try:
                resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
                data = resp["Body"].read()
                break
            except s3.exceptions.NoSuchKey:
                continue
        else:
            logger.warning("  Could not find sim tarball in S3 for %s", sim_name)
            return

        dest.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO(data)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.name.startswith("web/"):
                    member.name = member.name[len("web/") :]
                    if member.name:
                        tar.extract(member, dest)
        logger.info(
            "  Downloaded sim source from s3://%s/%s → %s", S3_BUCKET, key, dest
        )

    @staticmethod
    def _icon_svg_to_data_uri(icon_svg: str) -> str | None:
        """Convert an SVG string to a data URI for use as imgUrl."""
        if not icon_svg or not icon_svg.strip().startswith("<svg"):
            return None
        import base64

        encoded = base64.b64encode(icon_svg.strip().encode("utf-8")).decode("ascii")
        return f"data:image/svg+xml;base64,{encoded}"

    async def _register_simulator(
        self,
        http,
        api_key: str,
        base_name: str,
        description: str,
        icon_svg: str = "",
        label: str = "",
        skill_gym: str = "",
    ) -> str:
        """Create a simulator, bumping to -v2, -v3 etc. on name collision.

        Returns the actual sim name that was registered.
        """
        from plato._generated.api.v1.env import create_simulator
        from plato._generated.models import (
            CreateSimulatorRequest,
            SimulatorConfig,
        )

        try:
            _sim_config = SimulatorConfig(type="docker_app")  # type: ignore[arg-type]
        except Exception:
            _sim_config = SimulatorConfig.from_dict({"type": "docker_app"})

        if skill_gym:
            _sim_config.is_skill_gym = True  # type: ignore[attr-defined]
            _sim_config.skill_name = skill_gym  # type: ignore[attr-defined]

        icon_url = (
            self._icon_svg_to_data_uri(icon_svg) or "https://plato.so/favicon.ico"
        )

        m = re.match(r"^(.*)-v(\d+)$", base_name)
        if m:
            stem, start_v = m.group(1), int(m.group(2))
            candidates = [base_name] + [
                f"{stem}-v{n}" for n in range(start_v + 1, start_v + 100)
            ]
        else:
            candidates = [base_name] + [f"{base_name}-v{n}" for n in range(2, 100)]
        for name in candidates:
            try:
                await create_simulator.asyncio(
                    client=http,
                    body=CreateSimulatorRequest(
                        name=name,
                        description=description,
                        simType="docker_app",
                        config=_sim_config,
                        enabled=True,
                        imgUrl=icon_url,
                        internalAppPort=3000,
                    ),
                    x_api_key=api_key,
                )
                logger.info("  [%s] Created simulator '%s'", label, name)
                return name
            except Exception as e:
                if "already exists" in str(e).lower() or "409" in str(e):
                    continue
                logger.warning(
                    "  [%s] Could not create simulator '%s': %s",
                    label,
                    name,
                    e,
                )
                return base_name
        return base_name

    async def _autoverify_and_publish(
        self,
        vs: VariantStatus,
        tasks: list[dict],
        artifact_id: str,
        av_sem: asyncio.Semaphore | None = None,
    ) -> list[str]:
        """Run autoverify (if enabled), filter to verified tasks, then publish.

        Returns the list of created testcase IDs (only for verified tasks
        when autoverify is enabled).
        """
        config = self.config
        if config.autoverify and tasks and artifact_id:
            sem = av_sem or asyncio.Semaphore(config.run_concurrency)
            try:
                await self._autoverify_tasks(vs, tasks, artifact_id, sem)
            except Exception as e:
                logger.error("  [%s] Autoverify error: %s", vs.variant_key, e)

            verified = [t for t in tasks if t.get("_av_scoring_config")]
            skipped = len(tasks) - len(verified)
            if skipped:
                logger.info(
                    "  [%s] Autoverify gate: publishing %d/%d tasks (%d failed verification)",
                    vs.variant_key,
                    len(verified),
                    len(tasks),
                    skipped,
                )
            tasks = verified

        if not tasks:
            return []
        return await self._create_testcases(vs, tasks, artifact_id)

    async def _create_testcases(
        self,
        vs: VariantStatus,
        tasks: list[dict],
        artifact_id: str,
    ) -> list[str]:
        """Create Plato testcases for each task in a variant.

        Returns the list of created testcase IDs.
        """
        from .task_generator import _build_v2_scoring_config

        config = self.config
        sim_name = vs.sim_name
        if not sim_name:
            logger.error(
                "  [%s] sim_name not set, cannot create testcases", vs.variant_key
            )
            return []

        from plato._generated.api.v2.testcases import create_testcase
        from plato._generated.models import CreateTestCaseRequest

        from .skill_ingestion import _slugify as _slug

        skill_tag = _slug(vs.short_name or vs.skill_name)
        tc_tags = ["skill-test-generator", skill_tag]
        created_ids: list[str] = []

        async with httpx.AsyncClient(
            base_url=config.plato_api_url,
            timeout=httpx.Timeout(60.0),
        ) as http:
            for task in tasks:
                scoring_type = task.get("scoring_type", "output")

                # Prefer autoverify-generated scoring config over LLM-generated
                av_config = task.get("_av_scoring_config")
                if av_config:
                    v2_scoring_config = av_config
                    logger.info(
                        "  [%s] Using autoverify scoring config for '%s'",
                        vs.variant_key,
                        task.get("name", "unnamed"),
                    )
                else:
                    v2_scoring_config = _build_v2_scoring_config(task, sim_name)

                if not v2_scoring_config and task.get("scoring_config"):
                    logger.warning(
                        "  [%s] scoring_config present but "
                        "no scoring config available for '%s' — "
                        "testcase may be ungradeable",
                        vs.variant_key,
                        task.get("name", "unnamed"),
                    )

                tc_name = f"{vs.slug}-{task.get('name', 'unnamed')}"
                hint = task.get("hint", "") or ""
                req = CreateTestCaseRequest(
                    name=tc_name,
                    prompt=task.get("instruction", ""),
                    hint=hint or None,
                    start_url=task.get("start_url", "/"),
                    simulator_artifact_ids=[artifact_id],
                    tags=tc_tags,
                )

                if v2_scoring_config:
                    req.v2_scoring_config = v2_scoring_config
                av_gen_result = task.get("_av_generation_result")
                if av_gen_result:
                    req.v2_scoring_generation_result = av_gen_result
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
                    created_ids.append(tc_id)
                    task["_testcase_id"] = tc_id
                    logger.info("  Created testcase '%s' -> %s", tc_name, tc_id)
                except Exception as e:
                    logger.error("  Testcase creation error for '%s': %s", tc_name, e)

        return created_ids

    async def _archive_testcases(self, public_ids: list[str]) -> int:
        """Archive testcases by public ID. Returns count successfully archived."""
        if not public_ids:
            return 0
        config = self.config
        archived = 0
        async with httpx.AsyncClient(
            base_url=config.plato_api_url,
            timeout=httpx.Timeout(30.0),
            headers={"X-API-Key": config.plato_api_key},
        ) as http:
            numeric_ids: list[int] = []
            for pub_id in public_ids:
                try:
                    resp = await http.get(
                        "/api/v1/testcases",
                        params={"public_id": pub_id, "page_size": 1},
                    )
                    resp.raise_for_status()
                    for tc in resp.json().get("testcases", []):
                        pid = tc.get("publicId") or tc.get("public_id", "")
                        if pid == pub_id:
                            numeric_ids.append(tc["id"])
                            break
                except Exception as e:
                    logger.warning("Failed to resolve testcase %s: %s", pub_id, e)

            if numeric_ids:
                try:
                    resp = await http.post(
                        "/api/v1/testcases/bulk-archive",
                        json={"test_case_ids": numeric_ids},
                    )
                    resp.raise_for_status()
                    archived = len(numeric_ids)
                except Exception as e:
                    logger.error("Failed to bulk-archive testcases: %s", e)
        return archived

    # ------------------------------------------------------------------
    # AUTOVERIFY: run agent sessions → call v2 auto_verify → get scoring config
    # ------------------------------------------------------------------

    async def _autoverify_tasks(
        self,
        vs: VariantStatus,
        tasks: list[dict],
        artifact_id: str,
        av_sem: asyncio.Semaphore,
    ) -> None:
        """Run autoverify for each task: launch agent sessions, extract output,
        call the v2 auto_verify endpoint, and replace the LLM-generated scoring
        config with the one derived from real agent runs.
        """
        config = self.config
        sim_name = vs.sim_name

        from plato.chronos.api.jobs import launch_job
        from plato.chronos.api.sessions import get_session_logs
        from plato.chronos.models import (
            LaunchJobRequest,
            VMResources,
            WorldConfigInput,
            WorldRuntimeConfig,
        )

        import json as _json
        import time as _time

        async def _av_task(task_idx: int, task: dict, chronos_http):
            """Run all AV sessions for a single task, evaluate agreement."""
            task_name = task.get("name", f"task-{task_idx}")
            scoring_type = task.get("scoring_type", "output")
            instruction = task.get("instruction", "")
            hint = task.get("hint", "")
            output_schema = (
                task.get("output_schema") if scoring_type == "output" else None
            )

            prompt = instruction
            if output_schema:
                schema_json = _json.dumps(output_schema, indent=2)
                prompt = (
                    f"{prompt}\n\n"
                    "## CRITICAL OUTPUT REQUIREMENT\n\n"
                    "You MUST return ONLY a JSON object matching this EXACT schema:\n\n"
                    f"```json\n{schema_json}\n```\n\n"
                    "Copy all values EXACTLY as they appear on the page. "
                    "Return ONLY the raw JSON object — no markdown, no explanation."
                )

            logger.info(
                "  [%s] Autoverify '%s' (%s) — %d sessions",
                vs.variant_key,
                task_name,
                scoring_type,
                config.autoverify_sessions,
            )

            async def _av_one(sess_num: int):
                async with av_sem:
                    try:
                        result = await self._run_autoverify_session(
                            chronos_http,
                            config,
                            sim_name,
                            artifact_id,
                            prompt,
                            hint,
                            task_name,
                            sess_num,
                            vs.variant_key,
                            launch_job,
                            LaunchJobRequest,
                            WorldConfigInput,
                            WorldRuntimeConfig,
                            VMResources,
                        )
                        if result:
                            agent_output = await self._extract_agent_output(
                                chronos_http,
                                result["chronos_id"],
                                config.plato_api_key,
                                get_session_logs,
                            )
                            session_id = result.get("plato_id") or result["chronos_id"]
                            logger.info(
                                "    [%s/%d/%d] session=%s output=%s",
                                task_name,
                                sess_num + 1,
                                config.autoverify_sessions,
                                session_id,
                                "yes" if agent_output is not None else "none",
                            )
                            return {
                                "session_id": session_id,
                                "agent_output": agent_output,
                            }
                    except Exception as e:
                        logger.error(
                            "    [%s/%d/%d] autoverify session error: %s",
                            task_name,
                            sess_num + 1,
                            config.autoverify_sessions,
                            e,
                        )
                    return None

            results = await asyncio.gather(
                *[_av_one(i) for i in range(config.autoverify_sessions)]
            )
            collected = [r for r in results if r is not None]

            if not collected:
                logger.warning(
                    "  [%s] No autoverify sessions for '%s', keeping LLM config",
                    vs.variant_key,
                    task_name,
                )
                return

            MIN_AGREE = 3
            outputs_with_data = [
                s for s in collected if s.get("agent_output") is not None
            ]
            if len(outputs_with_data) < MIN_AGREE:
                logger.warning(
                    "  [%s] Autoverify FAILED for '%s': only %d/%d sessions returned output (need %d)",
                    vs.variant_key,
                    task_name,
                    len(outputs_with_data),
                    len(collected),
                    MIN_AGREE,
                )
                return

            first_output = outputs_with_data[0]["agent_output"]
            agreeing = [outputs_with_data[0]]
            for s in outputs_with_data[1:]:
                if s["agent_output"] == first_output:
                    agreeing.append(s)

            if len(agreeing) < MIN_AGREE:
                logger.warning(
                    "  [%s] Autoverify FAILED for '%s': only %d/%d outputs agree (need %d)",
                    vs.variant_key,
                    task_name,
                    len(agreeing),
                    len(outputs_with_data),
                    MIN_AGREE,
                )
                return

            if scoring_type == "output":
                now_ms = int(_time.time() * 1000)
                n_used = len(agreeing)
                task["_av_scoring_config"] = {
                    "output_config": {
                        "type": "json_schema",
                        "scoring_schema": first_output,
                        "num_sessions_used": n_used,
                        "created_at": now_ms,
                    },
                    "num_sessions_used": n_used,
                    "created_at": now_ms,
                }
                logger.info(
                    "  [%s] Autoverify PASSED for '%s' (%d/%d sessions agree)",
                    vs.variant_key,
                    task_name,
                    n_used,
                    len(collected),
                )

        async with httpx.AsyncClient(
            base_url=config.chronos_url, timeout=httpx.Timeout(120.0)
        ) as chronos_http:
            await asyncio.gather(
                *[_av_task(i, t, chronos_http) for i, t in enumerate(tasks)]
            )

    async def _run_autoverify_session(
        self,
        http,
        config,
        sim_name,
        artifact_id,
        prompt,
        hint,
        task_name,
        sess_num,
        variant_key,
        launch_job_mod,
        LaunchJobRequest,
        WorldConfigInput,
        WorldRuntimeConfig,
        VMResources,
    ) -> dict | None:
        """Launch a single CUA benchmark session for autoverify and wait for completion."""
        agent_config: dict = {
            "model_name": config.autoverify_model,
            "max_turns": config.eval_max_turns,
        }
        if (
            "anthropic" in config.autoverify_model
            or "claude" in config.autoverify_model
        ):
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

        av_instruction = prompt
        if hint:
            av_instruction = f"{prompt}\n\n## HINT\n\n{hint}"

        world_config = {
            "version": "2",
            "instruction": av_instruction,
            "envs": [
                {"type": "artifact", "artifact_id": artifact_id, "alias": sim_name}
            ],
            "login_flow": True,
            "login_flow_retries": 4,
            "login_flow_retry_delay_ms": 10000,
            "record_session": False,
            "agent": {
                "package": config.cua_agent_package,
                "config": agent_config,
                "runtime": {
                    "type": "vm",
                    "vm": {"cpus": 2, "memory": 4096, "timeout": 3600},
                },
            },
            "plato_api_key": config.plato_api_key,
        }

        request = LaunchJobRequest(
            world=WorldConfigInput(
                package=config.cua_world_package,
                runtime=WorldRuntimeConfig(
                    type="vm", vm=VMResources(cpus=2, memory=4096)
                ),
                config=world_config,
            ),
        )

        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            resp = await launch_job_mod.asyncio(
                client=http,
                body=request,
                x_api_key=config.plato_api_key,
            )
            chronos_id = resp.session_id
            logger.info(
                "    [%s] AV session %d launched: %s (attempt %d/%d)",
                variant_key,
                sess_num + 1,
                chronos_id,
                attempt,
                max_attempts,
            )

            status = await self._poll_until_done(http, chronos_id, config.plato_api_key)

            if status.get("status") in ("completed",):
                return {
                    "chronos_id": chronos_id,
                    "plato_id": status.get("plato_session_id", ""),
                }

            is_infra = status.get("status") in ("failed", "error", "cancelled")
            if is_infra and attempt < max_attempts:
                logger.warning(
                    "    AV session %s infra failure (status=%s), retrying (%d/%d)…",
                    chronos_id,
                    status.get("status"),
                    attempt,
                    max_attempts,
                )
                continue

            logger.warning(
                "    AV session %s ended with status=%s",
                chronos_id,
                status.get("status"),
            )
            return None

        return None

    async def _extract_agent_output(
        self,
        http,
        chronos_id: str,
        api_key: str,
        get_session_logs_mod,
    ) -> dict | None:
        """Extract structured agent output from Chronos OTel logs."""
        import json as _json

        try:
            logs_resp = await get_session_logs_mod.asyncio(
                client=http,
                public_id=chronos_id,
                limit=10000,
                x_api_key=api_key,
            )
            logs = logs_resp.logs or []

            def _get(log, field):
                if isinstance(log, dict):
                    return log.get(field)
                return getattr(log, field, None)

            raw_output: str | None = None
            for log in reversed(logs):
                name = _get(log, "name")
                attrs = _get(log, "attributes")
                if name == "computer_use_session" and attrs:
                    result = attrs.get("computer_use.result")
                    if result:
                        raw_output = (
                            _json.dumps(result)
                            if isinstance(result, (dict, list))
                            else str(result)
                        )
                        break
            if raw_output is None:
                for log in reversed(logs):
                    name = _get(log, "name")
                    attrs = _get(log, "attributes")
                    if name == "session" and attrs:
                        result = attrs.get("atif.session.result")
                        if result:
                            raw_output = (
                                _json.dumps(result)
                                if isinstance(result, (dict, list))
                                else str(result)
                            )
                            break

            if raw_output is None:
                return None

            try:
                parsed = _json.loads(raw_output)
                if isinstance(parsed, dict):
                    return parsed
            except (ValueError, _json.JSONDecodeError):
                pass

            # Try extracting JSON from markdown code blocks or brace matching
            for pattern in [
                r"```(?:json)?\s*(\{.*?\})\s*```",
                r"(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})",
            ]:
                import re as _re

                match = _re.search(pattern, raw_output, _re.DOTALL)
                if match:
                    try:
                        parsed = _json.loads(match.group(1))
                        if isinstance(parsed, dict):
                            return parsed
                    except (ValueError, _json.JSONDecodeError):
                        continue

            return None

        except Exception as e:
            logger.warning(
                "Failed to extract output from session %s: %s", chronos_id, e
            )
            return None

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
                logger.info("Skipping %s: no testcases", vs.variant_key)
                continue
            tasks = self._all_tasks.get(vs.variant_key, [])
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

        all_attempt_records: list[dict] = []
        completed_count = 0
        total_work = len(work_items)

        async with httpx.AsyncClient(
            base_url=config.chronos_url, timeout=httpx.Timeout(120.0)
        ) as http:
            max_retries = 3

            async def _run_one(
                idx: int,
                vs: VariantStatus,
                tc_id: str,
                task: dict,
                task_name: str,
                session_num: int,
            ) -> None:
                nonlocal completed_count
                stagger_delay = idx * 2.0
                if stagger_delay > 0:
                    await asyncio.sleep(stagger_delay)
                async with sem:
                    session_label = (
                        f"{task_name} [{session_num}/{n_sessions}] (testcase {tc_id})"
                    )

                    attempts_for_item: list[dict] = []

                    for attempt in range(1, max_retries + 1):
                        logger.info(
                            "  Launching: %s (attempt %d/%d)",
                            session_label,
                            attempt,
                            max_retries,
                        )
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
                            result["attempt"] = attempt
                            result["skill_name"] = vs.skill_name
                            result["slug"] = vs.variant_key
                            result["outcome"] = self._classify_session_outcome(result)

                            session_status = result.get("status", "")
                            is_infra_failure = session_status in (
                                "failed",
                                "error",
                                "cancelled",
                            )

                            attempts_for_item.append(result)

                            logger.info(
                                "    %s | score=%.2f | chronos=%s | plato=%s",
                                result["outcome"],
                                result.get("score", 0),
                                result.get("chronos_url", ""),
                                result.get("plato_url", ""),
                            )

                            if is_infra_failure and attempt < max_retries:
                                logger.warning(
                                    "    Session %s ended with status=%s, retrying…",
                                    result.get("chronos_id", "?"),
                                    session_status,
                                )
                                continue

                            vs.chronos_session_ids.append(result.get("chronos_id", ""))
                            vs.plato_session_ids.append(result.get("plato_id", ""))
                            vs.task_results.append(result)
                            break

                        except Exception as e:
                            logger.error(
                                "    Failed to run testcase %s (attempt %d): %s",
                                tc_id,
                                attempt,
                                e,
                            )
                            err_result = {
                                "testcase_id": tc_id,
                                "task_name": task_name,
                                "session_num": session_num,
                                "attempt": attempt,
                                "status": "error",
                                "outcome": "ERROR",
                                "error": str(e),
                                "skill_name": vs.skill_name,
                                "slug": vs.variant_key,
                            }
                            attempts_for_item.append(err_result)
                            if attempt >= max_retries:
                                vs.task_results.append(err_result)

                    all_attempt_records.extend(attempts_for_item)
                    completed_count += 1
                    if completed_count % 5 == 0 or completed_count == total_work:
                        logger.info(
                            "RUN progress: %d/%d work items done",
                            completed_count,
                            total_work,
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

        self._log_run_summary(all_attempt_records)

    def _log_run_summary(self, all_attempts: list[dict]) -> None:
        """Log a structured summary of all RUN phase sessions and attempts."""
        total_attempts = len(all_attempts)
        retried_attempts = [a for a in all_attempts if a.get("attempt", 1) > 1]
        total_retries = len(retried_attempts)

        final_results: list[dict] = []
        for vs in self.state.variants:
            final_results.extend(vs.task_results)

        n_pass = sum(1 for r in final_results if r.get("outcome") == "PASS")
        n_fail = sum(1 for r in final_results if r.get("outcome") == "FAIL")
        n_error = sum(1 for r in final_results if r.get("outcome") == "ERROR")
        n_total = len(final_results)

        logger.info("=" * 70)
        logger.info("RUN PHASE SUMMARY")
        logger.info("=" * 70)
        logger.info(
            "Total sessions launched: %d  (retries: %d)",
            total_attempts,
            total_retries,
        )
        logger.info(
            "Final outcomes: %d PASS | %d FAIL | %d ERROR  (of %d work items)",
            n_pass,
            n_fail,
            n_error,
            n_total,
        )
        if n_total:
            effective_total = n_pass + n_fail
            if effective_total > 0:
                logger.info(
                    "Pass rate (excluding errors): %d/%d = %.1f%%",
                    n_pass,
                    effective_total,
                    n_pass / effective_total * 100,
                )
            else:
                logger.info("Pass rate: N/A (no completed sessions)")
        logger.info("-" * 70)

        attempts_by_slug: dict[str, list[dict]] = {}
        for a in all_attempts:
            slug_key = a.get("slug", "unknown")
            attempts_by_slug.setdefault(slug_key, []).append(a)

        for vs in self.state.variants:
            skill_name = vs.skill_name
            slug = vs.variant_key
            variant_attempts = attempts_by_slug.get(slug, [])
            skill_finals = [r for r in vs.task_results]

            s_pass = sum(1 for r in skill_finals if r.get("outcome") == "PASS")
            s_fail = sum(1 for r in skill_finals if r.get("outcome") == "FAIL")
            s_error = sum(1 for r in skill_finals if r.get("outcome") == "ERROR")
            s_retries = sum(1 for a in variant_attempts if a.get("attempt", 1) > 1)

            logger.info(
                "SKILL: %s  [%s]",
                skill_name,
                slug,
            )
            logger.info(
                "  Results: %d PASS | %d FAIL | %d ERROR | %d retries",
                s_pass,
                s_fail,
                s_error,
                s_retries,
            )

            tc_attempts: dict[str, list[dict]] = {}
            for a in variant_attempts:
                tc_key = a.get("testcase_id", "?")
                tc_attempts.setdefault(tc_key, []).append(a)

            for tc_id, tc_att_list in tc_attempts.items():
                task_name = tc_att_list[0].get("task_name", "?")
                final_for_tc = [
                    r for r in skill_finals if r.get("testcase_id") == tc_id
                ]
                final = final_for_tc[0] if final_for_tc else None
                outcome_str = final.get("outcome", "?") if final else "ALL_ERRORED"

                logger.info(
                    "  TESTCASE: %s  (%s)  → %s",
                    task_name,
                    tc_id,
                    outcome_str,
                )

                for att in sorted(tc_att_list, key=lambda x: x.get("attempt", 1)):
                    attempt_num = att.get("attempt", 1)
                    is_final = (
                        final is not None
                        and att.get("chronos_id") == final.get("chronos_id")
                        and att.get("chronos_id") is not None
                    )
                    marker = " (final)" if is_final else " (retried)"
                    chronos_url = att.get("chronos_url", "")
                    plato_url = att.get("plato_url", "")
                    logger.info(
                        "    attempt %d%s | %s | score=%.2f | chronos=%s | plato=%s",
                        attempt_num,
                        marker,
                        att.get("outcome", att.get("status", "?")),
                        att.get("score", 0),
                        chronos_url,
                        plato_url,
                    )
            logger.info("-" * 70)

        logger.info("=" * 70)

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
        elif (
            "anthropic" in config.eval_agent_model
            or "claude" in config.eval_agent_model
        ):
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
        global_passed = 0
        global_failed = 0
        global_errored = 0

        for vs in self.state.variants:
            skill_result = {
                "skill_name": vs.skill_name,
                "slug": vs.variant_key,
                "sim_name": vs.sim_name,
                "artifact_id": vs.artifact_id,
                "testcase_count": len(vs.testcase_ids),
                "tasks": [],
            }

            passed = 0
            failed = 0
            errored = 0
            for tr in vs.task_results:
                outcome = tr.get("outcome", "")
                is_error = outcome == "ERROR" or tr.get("status") in (
                    "failed",
                    "error",
                    "cancelled",
                )
                is_pass = tr.get("score", 0) > 0

                if is_error:
                    errored += 1
                elif is_pass:
                    passed += 1
                else:
                    failed += 1

                task_entry: dict = {
                    "task_name": tr.get("task_name", ""),
                    "testcase_id": tr.get("testcase_id", ""),
                    "status": tr.get("status", ""),
                    "outcome": "PASS" if is_pass else ("ERROR" if is_error else "FAIL"),
                    "score": tr.get("score", 0),
                    "pass": is_pass,
                    "chronos_url": tr.get("chronos_url", ""),
                    "plato_url": tr.get("plato_url", ""),
                    "error": tr.get("error"),
                    "attempt": tr.get("attempt", 1),
                }

                scoring_details = tr.get("scoring_details")
                if scoring_details:
                    task_entry["scoring_details"] = scoring_details

                skill_result["tasks"].append(task_entry)

            completed = passed + failed
            skill_result["passed"] = passed
            skill_result["failed"] = failed
            skill_result["errored"] = errored
            skill_result["pass_rate"] = f"{passed}/{completed}" if completed else "0/0"
            skill_result["pass_pct"] = (
                round(passed / completed * 100, 1) if completed else 0
            )
            final_results.append(skill_result)

            global_passed += passed
            global_failed += failed
            global_errored += errored

        (config.output / "final_results.json").write_text(
            json.dumps(final_results, indent=2)
        )

        global_completed = global_passed + global_failed
        global_pct = (
            round(global_passed / global_completed * 100, 1) if global_completed else 0
        )

        logger.info("=" * 70)
        logger.info("FINAL RESULTS")
        logger.info("=" * 70)
        logger.info(
            "Overall: %d PASS | %d FAIL | %d ERROR",
            global_passed,
            global_failed,
            global_errored,
        )
        logger.info(
            "Pass rate (excluding errors): %d/%d = %.1f%%",
            global_passed,
            global_completed,
            global_pct,
        )
        logger.info("-" * 70)

        for r in final_results:
            logger.info(
                "SKILL: %s — %s (%.1f%%)  [%d pass, %d fail, %d error]",
                r["skill_name"],
                r["pass_rate"],
                r["pass_pct"],
                r["passed"],
                r["failed"],
                r["errored"],
            )
            for t in r["tasks"]:
                logger.info(
                    "  [%s] %s | score=%.2f | attempt=%d | chronos=%s | plato=%s",
                    t["outcome"],
                    t["task_name"],
                    t["score"],
                    t.get("attempt", 1),
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
        logger.info("=" * 70)

    # ------------------------------------------------------------------
    # HILLCLIMB: iteratively increase testcase difficulty
    # ------------------------------------------------------------------

    async def _run_hillclimb(self) -> None:
        """Hillclimb stage: per-testcase difficulty tuning.

        Variants are processed in parallel. Testcases within a variant that
        exceed the target pass rate are hillclimbed sequentially.
        """
        config = self.config
        hc = config.hillclimb

        if not hc.enabled:
            logger.info("Skipping HILLCLIMB stage: not enabled")
            return

        if config.sessions_per_testcase <= 0:
            logger.info("Skipping HILLCLIMB stage: sessions_per_testcase is 0")
            return

        n_sessions = config.sessions_per_testcase
        target_max_pass_rate = (n_sessions - hc.total_failures) / n_sessions
        logger.info(
            "HILLCLIMB: target max pass rate = %.1f%% "
            "(sessions=%d, target_failures=%d, max_retries=%d)",
            target_max_pass_rate * 100,
            n_sessions,
            hc.total_failures,
            hc.max_retries,
        )

        if not self._all_tasks:
            self._load_tasks_from_disk()
        if not self._variant_specs:
            self._load_variant_specs_from_disk()

        # API fallback: fetch testcase data if local tasks.json missing (resume)
        missing = [
            vs
            for vs in self.state.variants
            if vs.testcase_ids and not self._all_tasks.get(vs.variant_key)
        ]
        if missing:
            logger.info(
                "HILLCLIMB: %d variants missing local tasks, fetching from API",
                len(missing),
            )
            await self._load_tasks_from_api()

        # Seed per-testcase state and variant-level iteration-0
        variants_with_work: list[VariantStatus] = []
        for vs in self.state.variants:
            if vs.stage != "evaluated" or not vs.task_results:
                continue

            # Group results by testcase_id → per-testcase pass rates
            tc_results: dict[str, list[dict]] = {}
            for r in vs.task_results:
                tc_id = r.get("testcase_id", "")
                if tc_id:
                    tc_results.setdefault(tc_id, []).append(r)

            has_candidates = False
            for tc_id, results in tc_results.items():
                if tc_id in vs.testcase_hillclimb_state:
                    if (
                        vs.testcase_hillclimb_state[tc_id].best_pass_rate
                        > target_max_pass_rate
                    ):
                        has_candidates = True
                    continue
                passed = sum(1 for r in results if r.get("outcome") == "PASS")
                failed = sum(1 for r in results if r.get("outcome") == "FAIL")
                completed = passed + failed
                rate = passed / completed if completed else 0.0
                vs.testcase_hillclimb_state[tc_id] = TestcaseHillclimbState(
                    testcase_id=tc_id,
                    original_pass_rate=rate,
                    best_pass_rate=rate,
                    best_testcase_id=tc_id,
                    best_iteration_idx=0,
                    iterations=[
                        TestcaseIterationRecord(
                            iteration=0,
                            testcase_id=tc_id,
                            artifact_id=vs.artifact_id,
                            task_results=results,
                            pass_rate=rate,
                        )
                    ],
                )
                if rate > target_max_pass_rate:
                    has_candidates = True

            # Variant-level iteration-0 for backward compat / summary
            if not vs.hillclimb_iterations:
                passed = sum(1 for r in vs.task_results if r.get("outcome") == "PASS")
                failed = sum(1 for r in vs.task_results if r.get("outcome") == "FAIL")
                completed = passed + failed
                rate = passed / completed if completed else 0.0
                vs.hillclimb_iterations.append(
                    IterationRecord(
                        iteration=0,
                        artifact_id=vs.artifact_id,
                        testcase_ids=list(vs.testcase_ids),
                        task_results=list(vs.task_results),
                        pass_rate=rate,
                    )
                )
                vs.best_iteration = 0

            if has_candidates:
                variants_with_work.append(vs)

        logger.info(
            "HILLCLIMB: %d / %d variants have testcases exceeding target (%.1f%%)",
            len(variants_with_work),
            len([v for v in self.state.variants if v.task_results]),
            target_max_pass_rate * 100,
        )

        for vs in variants_with_work:
            tc_list = []
            for tc_id, tc_state in vs.testcase_hillclimb_state.items():
                status = (
                    "NEEDS_WORK"
                    if tc_state.best_pass_rate > target_max_pass_rate
                    else "OK"
                )
                tc_list.append(
                    f"{tc_id[:12]} ({status}, {tc_state.best_pass_rate * 100:.0f}%)"
                )
            logger.info(
                "HILLCLIMB [%s] %d/%d testcases exceed target, processing sequentially",
                vs.variant_key,
                sum(
                    1
                    for s in vs.testcase_hillclimb_state.values()
                    if s.best_pass_rate > target_max_pass_rate
                ),
                len(vs.testcase_hillclimb_state),
            )
            for tc_info in tc_list:
                logger.info("  HILLCLIMB [%s]   %s", vs.variant_key, tc_info)

        if not variants_with_work:
            logger.info("HILLCLIMB: nothing to do — all testcases within target")
            self._log_hillclimb_summary(target_max_pass_rate)
            return

        sem = asyncio.Semaphore(config.vm_concurrency)

        if (
            config.coder_agent
            and config.coder_agent.max_parallel < config.vm_concurrency
        ):
            config.coder_agent.max_parallel = config.vm_concurrency

        async def _hillclimb_one(vs: VariantStatus) -> None:
            async with sem:
                await self._hillclimb_variant(vs, target_max_pass_rate, hc.max_retries)

        await asyncio.gather(*[_hillclimb_one(vs) for vs in variants_with_work])

        self._log_hillclimb_summary(target_max_pass_rate)

    async def _hillclimb_variant(
        self,
        vs: VariantStatus,
        target_max_pass_rate: float,
        max_retries: int,
    ) -> None:
        """Process testcases that exceed target sequentially within a variant."""
        config = self.config
        current_artifact_id = vs.artifact_id

        exceeding = [
            (tc_id, tc_state)
            for tc_id, tc_state in vs.testcase_hillclimb_state.items()
            if tc_state.best_pass_rate > target_max_pass_rate
        ]
        logger.info(
            "HILLCLIMB [%s] %d/%d testcases exceed target, processing sequentially",
            vs.variant_key,
            len(exceeding),
            len(vs.testcase_hillclimb_state),
        )

        for tc_id, tc_state in exceeding:
            tc_idx = next(
                (i for i, tid in enumerate(vs.testcase_ids) if tid == tc_id), -1
            )
            if tc_idx < 0:
                logger.warning(
                    "HILLCLIMB [%s] tc %s not found in testcase_ids, skipping",
                    vs.variant_key,
                    tc_id,
                )
                continue

            for retry in range(1, max_retries + 1):
                if tc_state.best_pass_rate <= target_max_pass_rate:
                    logger.info(
                        "HILLCLIMB [%s][tc-%03d] within target after %d iterations",
                        vs.variant_key,
                        tc_idx,
                        retry - 1,
                    )
                    break

                best_iter = tc_state.iterations[tc_state.best_iteration_idx]
                logger.info(
                    "HILLCLIMB [%s][tc-%03d] iteration %d/%d — pass_rate=%.1f%%",
                    vs.variant_key,
                    tc_idx,
                    retry,
                    max_retries,
                    tc_state.best_pass_rate * 100,
                )

                # (a) Fetch trajectories for this testcase's sessions only
                trajectory_data = await self._hc_fetch_trajectories(
                    vs, best_iter.task_results
                )

                # (b) Write workspace focused on this testcase
                workspace_dir = (
                    config.output
                    / "hillclimb"
                    / vs.variant_key
                    / f"tc-{tc_idx:03d}"
                    / f"iter-{retry}"
                )
                workspace_dir.mkdir(parents=True, exist_ok=True)
                await self._hc_write_workspace_for_testcase(
                    vs,
                    tc_idx,
                    tc_id,
                    best_iter.task_results,
                    trajectory_data,
                    workspace_dir,
                )

                # (c) Launch agent focused on this testcase
                edits = await self._hc_run_agent(
                    vs, workspace_dir, retry, target_tc_idx=tc_idx
                )
                if not edits:
                    logger.warning(
                        "HILLCLIMB [%s][tc-%03d] no edits.json, skipping iteration",
                        vs.variant_key,
                        tc_idx,
                    )
                    continue

                edits_summary = (
                    edits.get("iteration_summary") or edits.get("rationale", "")
                )[:500]
                edit_type = edits.get("edit_type", "testcase_only")
                sim_changed = edits.get("sim_changed", False) or edit_type in (
                    "sim_and_testcase",
                    "sim_only",
                )

                # (d) Apply edits
                new_artifact_id = current_artifact_id
                new_tc_id: str | None = None

                if sim_changed:
                    new_artifact_id = await self._hc_apply_sim_edits(
                        vs, workspace_dir, current_artifact_id, edits
                    )
                    if not new_artifact_id:
                        logger.error(
                            "HILLCLIMB [%s][tc-%03d] sim edit failed",
                            vs.variant_key,
                            tc_idx,
                        )
                        continue
                    current_artifact_id = new_artifact_id

                    # Sim changed → only republish the TARGET testcase against the new artifact
                    tc_file = workspace_dir / "testcases" / f"tc-{tc_idx:03d}.json"
                    if not tc_file.exists():
                        logger.error(
                            "HILLCLIMB [%s][tc-%03d] testcase file not found after sim change",
                            vs.variant_key,
                            tc_idx,
                        )
                        continue
                    target_task = self._task_dict_from_file(tc_file)
                    if not target_task:
                        logger.error(
                            "HILLCLIMB [%s][tc-%03d] could not parse testcase file",
                            vs.variant_key,
                            tc_idx,
                        )
                        continue

                    prev_tc_id = (
                        tc_state.iterations[-1].testcase_id
                        if tc_state.iterations
                        else None
                    )

                    published = await self._autoverify_and_publish(
                        vs, [target_task], new_artifact_id
                    )
                    if not published:
                        logger.error(
                            "HILLCLIMB [%s][tc-%03d] testcase publish failed "
                            "after sim change",
                            vs.variant_key,
                            tc_idx,
                        )
                        continue
                    new_tc_id = published[0]

                    # Archive the superseded testcase
                    if prev_tc_id and prev_tc_id != new_tc_id:
                        n = await self._archive_testcases([prev_tc_id])
                        if n:
                            logger.info(
                                "HILLCLIMB [%s][tc-%03d] archived superseded testcase %s",
                                vs.variant_key,
                                tc_idx,
                                prev_tc_id[:12],
                            )
                else:
                    # Testcase-only → autoverify + publish just the edited testcase
                    tc_file = workspace_dir / "testcases" / f"tc-{tc_idx:03d}.json"
                    if not tc_file.exists():
                        logger.error(
                            "HILLCLIMB [%s] testcase file %s not found",
                            vs.variant_key,
                            tc_file,
                        )
                        new_tc_id = None
                    else:
                        # Get previous testcase ID for this specific TC
                        prev_tc_id = (
                            tc_state.iterations[-1].testcase_id
                            if tc_state.iterations
                            else None
                        )

                        task = self._task_dict_from_file(tc_file)
                        if task:
                            published = await self._autoverify_and_publish(
                                vs, [task], current_artifact_id
                            )
                            new_tc_id = published[0] if published else None
                        else:
                            new_tc_id = None

                        # Archive the superseded testcase
                        if new_tc_id and prev_tc_id and prev_tc_id != new_tc_id:
                            n = await self._archive_testcases([prev_tc_id])
                            if n:
                                logger.info(
                                    "HILLCLIMB [%s][tc-%03d] archived superseded testcase %s",
                                    vs.variant_key,
                                    tc_idx,
                                    prev_tc_id[:12],
                                )

                if not new_tc_id:
                    logger.error(
                        "HILLCLIMB [%s][tc-%03d] no testcase ID after publish",
                        vs.variant_key,
                        tc_idx,
                    )
                    continue

                # (e) Rerun benchmark for THIS testcase only
                new_results = await self._hc_rerun_benchmark(vs, [new_tc_id])

                # (f) Score and compare
                passed = sum(1 for r in new_results if r.get("outcome") == "PASS")
                failed = sum(1 for r in new_results if r.get("outcome") == "FAIL")
                completed = passed + failed
                new_rate = passed / completed if completed else 0.0

                iter_record = TestcaseIterationRecord(
                    iteration=retry,
                    testcase_id=new_tc_id,
                    artifact_id=new_artifact_id,
                    task_results=new_results,
                    pass_rate=new_rate,
                    edits_summary=edits_summary,
                    edit_type=edit_type,
                )
                tc_state.iterations.append(iter_record)

                session_urls = [
                    r.get("chronos_url", r.get("plato_url", ""))
                    for r in new_results
                    if r.get("chronos_url") or r.get("plato_url")
                ]
                logger.info(
                    "HILLCLIMB [%s][tc-%03d] iteration %d: %.1f%% → %.1f%%  "
                    "tc=%s artifact=%s",
                    vs.variant_key,
                    tc_idx,
                    retry,
                    tc_state.best_pass_rate * 100,
                    new_rate * 100,
                    new_tc_id[:12],
                    new_artifact_id[:12] if new_artifact_id else "n/a",
                )
                for url in session_urls:
                    logger.info(
                        "  HILLCLIMB [%s][tc-%03d]   session: %s",
                        vs.variant_key,
                        tc_idx,
                        url,
                    )

                if new_rate < tc_state.best_pass_rate:
                    tc_state.best_iteration_idx = len(tc_state.iterations) - 1
                    tc_state.best_pass_rate = new_rate
                    tc_state.best_testcase_id = new_tc_id

                if new_rate <= target_max_pass_rate:
                    logger.info(
                        "HILLCLIMB [%s][tc-%03d] target reached",
                        vs.variant_key,
                        tc_idx,
                    )
                    break

    async def _hc_fetch_trajectories(
        self, vs: VariantStatus, task_results: list[dict]
    ) -> dict[str, dict]:
        """Fetch session trajectories from Chronos for given task results."""
        from plato.chronos.sdk import AsyncChronos

        config = self.config
        chronos = AsyncChronos(
            base_url=config.chronos_url, api_key=config.plato_api_key
        )

        trajectories: dict[str, dict] = {}
        for result in task_results:
            chronos_id = result.get("chronos_id", "")
            if not chronos_id:
                continue
            try:
                trajectory = await chronos.get_trajectory(chronos_id)
                trajectories[chronos_id] = {
                    "trajectory": trajectory.model_dump(mode="json"),
                    "testcase_id": result.get("testcase_id", ""),
                    "task_name": result.get("task_name", ""),
                    "outcome": result.get("outcome", ""),
                    "score": result.get("score", 0),
                }
            except Exception as e:
                logger.warning(
                    "HILLCLIMB [%s] failed to fetch trajectory for %s: %s",
                    vs.variant_key,
                    chronos_id,
                    e,
                )
        return trajectories

    async def _hc_write_workspace_for_testcase(
        self,
        vs: VariantStatus,
        target_tc_idx: int,
        target_tc_id: str,
        target_results: list[dict],
        trajectory_data: dict[str, dict],
        workspace_dir: Path,
    ) -> None:
        """Write workspace focused on a single target testcase for the agent."""
        spec = next(
            (s for s in self._variant_specs if s.get("slug") == vs.variant_key), {}
        )
        skill_def = next((s for s in self._skills if s.name == vs.skill_name), None)

        (workspace_dir / "skill.json").write_text(
            json.dumps(
                {
                    "name": vs.skill_name,
                    "short_name": vs.short_name,
                    "description": skill_def.description if skill_def else "",
                },
                indent=2,
            )
        )
        (workspace_dir / "spec.json").write_text(json.dumps(spec, indent=2))

        # Identify the target testcase for the agent
        (workspace_dir / "target.json").write_text(
            json.dumps(
                {
                    "target_testcase_index": target_tc_idx,
                    "target_testcase_id": target_tc_id,
                    "target_testcase_file": f"tc-{target_tc_idx:03d}.json",
                    "note": (
                        "Focus your edits on this testcase. "
                        "Other testcases are for context only."
                    ),
                },
                indent=2,
            )
        )

        # Results for the target testcase only
        results_list = [
            {
                "testcase_id": r.get("testcase_id", ""),
                "task_name": r.get("task_name", ""),
                "outcome": r.get("outcome", ""),
                "score": r.get("score", 0),
                "chronos_id": r.get("chronos_id", ""),
                "scoring_details": r.get("scoring_details"),
            }
            for r in target_results
        ]
        (workspace_dir / "results.json").write_text(json.dumps(results_list, indent=2))

        # Write ALL testcase configs (agent needs context if sim changes)
        tc_dir = workspace_dir / "testcases"
        tc_dir.mkdir(exist_ok=True)
        tasks = self._all_tasks.get(vs.variant_key, [])
        for i, tc_id in enumerate(vs.testcase_ids):
            task = tasks[i] if i < len(tasks) else {}
            (tc_dir / f"tc-{i:03d}.json").write_text(
                json.dumps(
                    {
                        "testcase_id": tc_id,
                        "name": task.get("name", ""),
                        "instruction": task.get("instruction", ""),
                        "hint": task.get("hint", ""),
                        "start_url": task.get("start_url", "/"),
                        "scoring_type": task.get("scoring_type", "output"),
                        "expected_output": task.get("expected_output"),
                        "output_schema": task.get("output_schema"),
                        "scoring_config": task.get("scoring_config"),
                        "is_target": tc_id == target_tc_id,
                    },
                    indent=2,
                )
            )

        # Prior iteration summaries (for subsequent retries)
        tc_state = vs.testcase_hillclimb_state.get(target_tc_id)
        if tc_state and len(tc_state.iterations) > 1:
            prior = []
            for it in tc_state.iterations:
                if it.iteration == 0:
                    prior.append(
                        {
                            "iteration": 0,
                            "pass_rate": it.pass_rate,
                            "note": "Original baseline — no edits applied.",
                        }
                    )
                else:
                    prior.append(
                        {
                            "iteration": it.iteration,
                            "pass_rate": it.pass_rate,
                            "edit_type": it.edit_type,
                            "edits_summary": it.edits_summary,
                        }
                    )
            (workspace_dir / "prior_iterations.json").write_text(
                json.dumps(prior, indent=2)
            )

        # Session trajectories (only for the target testcase)
        sess_dir = workspace_dir / "sessions"
        sess_dir.mkdir(exist_ok=True)
        for chronos_id, tdata in trajectory_data.items():
            safe_id = chronos_id[:12]
            sd = sess_dir / safe_id
            sd.mkdir(exist_ok=True)
            (sd / "metadata.json").write_text(
                json.dumps(
                    {
                        "chronos_id": chronos_id,
                        "testcase_id": tdata["testcase_id"],
                        "task_name": tdata["task_name"],
                        "outcome": tdata["outcome"],
                        "score": tdata["score"],
                    },
                    indent=2,
                )
            )
            (sd / "trajectory.json").write_text(
                json.dumps(tdata["trajectory"], indent=2, default=str)
            )

        # Copy sim source code
        sim_src = self._code_data_dir() / "variants" / vs.variant_key / "web"
        sim_dest = workspace_dir / "sim"
        if sim_src.is_dir():
            import shutil

            if sim_dest.exists():
                shutil.rmtree(sim_dest)
            shutil.copytree(
                sim_src,
                sim_dest,
                ignore=shutil.ignore_patterns(
                    "node_modules", ".next", ".next-*", ".turbo", "__pycache__"
                ),
            )
        elif not sim_dest.is_dir():
            self._download_sim_from_s3(vs.sim_name, sim_dest)

    async def _hc_run_agent(
        self,
        vs: VariantStatus,
        workspace_dir: Path,
        iteration: int,
        target_tc_idx: int = -1,
    ) -> dict | None:
        """Launch the hillclimb claude-code agent and return parsed edits.json."""
        from .prompts import HILLCLIMB_AGENT_PROMPT

        config = self.config

        rel_dir = workspace_dir.relative_to(config.output)
        mount_path = f"/workspace/output/{rel_dir}"

        tc_focus = ""
        if target_tc_idx >= 0:
            tc_focus = (
                f"- TARGET TESTCASE: tc-{target_tc_idx:03d}.json — "
                f"focus your edits on this testcase.\n"
                f"- Read target.json for details on which testcase to focus on.\n"
                f"- results.json and sessions/ contain data ONLY for this "
                f"target testcase.\n"
                f"- Other testcases in testcases/ are for context — only edit "
                f"them if you change the sim.\n"
            )

        prior_note = ""
        tc_state = vs.testcase_hillclimb_state.get(
            vs.testcase_ids[target_tc_idx] if target_tc_idx >= 0 else "", None
        )
        if tc_state and len(tc_state.iterations) > 1:
            prior_note = (
                f"- IMPORTANT: This is iteration {iteration}. Read "
                f"prior_iterations.json FIRST to see what previous "
                f"attempts tried and why they didn't work.\n"
            )

        instruction = (
            f"{HILLCLIMB_AGENT_PROMPT}\n\n"
            f"## Current State\n"
            f"- Skill: {vs.skill_name}\n"
            f"- Variant: {vs.variant_key}\n"
            f"- Iteration: {iteration}\n"
            f"- Working directory: {mount_path}\n"
            f"- First: cd {mount_path}\n"
            f"{tc_focus}"
            f"{prior_note}"
            f"- Read skill.json, spec.json, target.json, results.json, "
            f"testcases/, and sessions/ in that directory.\n"
            f"- Write your edits directly to testcases/ and/or sim/ "
            f"directories there.\n"
            f"- When done, write edits.json to {mount_path}/edits.json\n"
        )

        agent_config = config.coder_agent
        if not agent_config:
            from plato.worlds import AgentConfig

            agent_config = AgentConfig(package="claude-code:latest")

        display = f"hillclimb-{vs.variant_key}-i{iteration}"
        if target_tc_idx >= 0:
            display = f"hillclimb-{vs.variant_key}-tc{target_tc_idx:03d}-i{iteration}"

        try:
            runner = self.agent(
                agent_config,
                display_name=display,
                workspaces=[self.workspace("output")],
            )
            await runner.run(instruction=instruction)
        except Exception as e:
            logger.error("HILLCLIMB [%s] agent failed: %s", vs.variant_key, e)
            return None

        edits_path = workspace_dir / "edits.json"
        if not edits_path.exists():
            for candidate in workspace_dir.rglob("edits.json"):
                edits_path = candidate
                break
        if not edits_path.exists():
            for candidate in config.output.rglob("edits.json"):
                edits_path = candidate
                logger.info(
                    "HILLCLIMB [%s] found edits.json at %s", vs.variant_key, edits_path
                )
                break
        if not edits_path.exists():
            return None

        try:
            return json.loads(edits_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error(
                "HILLCLIMB [%s] failed to parse edits.json: %s", vs.variant_key, e
            )
            return None

    async def _hc_apply_sim_edits(
        self,
        vs: VariantStatus,
        workspace_dir: Path,
        current_artifact_id: str,
        edits: dict,
    ) -> str | None:
        """Boot from existing snapshot, apply sim edits, build, snapshot.

        The hillclimb agent already validated the build on its own VM
        (build_proof.json). We still build on the snapshot VM because
        the tarball has no .next/ directory, but skip the heavy
        fix-agent fallback — if it fails here the iteration is abandoned.

        Returns new artifact_id or None on failure.
        """
        from plato._generated.api.v2.sessions import (
            close as sessions_close,
            make as sessions_make,
        )
        from plato._generated.models import (
            CreateSessionFromEnvs,
            EnvFromResource,
            Envs,
            RunSessionSource,
        )

        config = self.config
        api_key = config.plato_api_key
        api_url = config.plato_api_url
        base_sim_name = vs.sim_name
        session_id: str | None = None

        sim_dir = workspace_dir / "sim"
        if not sim_dir.is_dir():
            logger.error("HILLCLIMB [%s] no sim/ directory for edits", vs.variant_key)
            return None

        build_proof = workspace_dir / "build_proof.json"
        agent_verified = build_proof.exists()
        if agent_verified:
            logger.info(
                "HILLCLIMB [%s] agent provided build_proof.json — "
                "skipping fix-agent fallback",
                vs.variant_key,
            )
        else:
            logger.warning(
                "HILLCLIMB [%s] no build_proof.json — agent did not "
                "self-verify; build may fail",
                vs.variant_key,
            )

        spec = next(
            (s for s in self._variant_specs if s.get("slug") == vs.variant_key), {}
        )
        api_routes = [r for r in spec.get("api_routes", []) if isinstance(r, dict)]

        async with httpx.AsyncClient(
            base_url=api_url,
            timeout=httpx.Timeout(300.0, connect=30.0),
        ) as http:
            try:
                from plato._generated.models import (
                    AppSchemasBuildModelsSimConfigCompute as SimConfigCompute,
                )

                env = EnvFromResource(
                    simulator=base_sim_name,
                    artifact=current_artifact_id,
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
                    client=http, body=body, x_api_key=api_key
                )
                session_id = resp.session_id
                logger.info(
                    "HILLCLIMB [%s] snapshot VM %s created", vs.variant_key, session_id
                )

                _exec = self._make_exec_fn(http, session_id, api_key)
                await self._poll_vm_ready(
                    http, session_id, api_key, f"hc-{vs.variant_key}"
                )

                app_dir = "/tmp/variant/sim"
                preamble = 'export PATH="/root/.bun/bin:/usr/local/bin:$PATH"'
                kill_port = (
                    "for p in $(ss -tlnp 2>/dev/null | grep ':3000 ' "
                    "| sed -n 's/.*pid=\\([0-9]*\\).*/\\1/p'); "
                    "do kill -9 $p 2>/dev/null; done; sleep 1"
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
                    f"{preamble} && "
                    "command -v node >/dev/null 2>&1 || "
                    'ln -sf "$(command -v bun)" /usr/local/bin/node',
                    timeout=10,
                )

                tarball = self._tar_variant(sim_dir.parent, f"hc-{vs.variant_key}")
                url = self._upload_to_s3(
                    tarball,
                    f"{S3_PREFIX}/{vs.sim_name}-hc-{len(vs.hillclimb_iterations)}.tar.gz",
                )

                await _exec(
                    f"curl -sfL '{url}' -o /tmp/hc-variant.tar.gz && "
                    "mkdir -p /tmp/variant && "
                    "tar xzf /tmp/hc-variant.tar.gz -C /tmp/variant --strip-components=0",
                    timeout=180,
                )

                # ── BUILD ────────────────────────────────────────────
                await _exec(kill_port, timeout=10)
                await _exec(f"rm -rf {app_dir}/.next", timeout=10)
                await _exec(
                    f"{preamble} && cd {app_dir} && bun install 2>&1 | tail -5",
                    timeout=300,
                )
                build_out, build_ok = await _exec(
                    f"{preamble} && cd {app_dir} && "
                    "NODE_ENV=production NEXT_DIST_DIR=.next "
                    "bun ./node_modules/next/dist/bin/next build 2>&1 | tail -20",
                    timeout=300,
                )
                if not build_ok:
                    logger.error(
                        "HILLCLIMB [%s] rebuild failed: %s",
                        vs.variant_key,
                        build_out[-500:],
                    )
                    return None

                # ── Start server + verify ────────────────────────────
                await _exec(kill_port, timeout=10)
                await _exec(
                    f"{preamble} && cd {app_dir} && mkdir -p /tmp/pglite-data && "
                    "NEXT_DIST_DIR=.next NODE_ENV=production PORT=3000 "
                    "nohup bun ./node_modules/next/dist/bin/next start "
                    "--hostname 0.0.0.0 -p 3000 > /tmp/dev.log 2>&1 &",
                    timeout=30,
                )

                verify_checks = await self._verify_sim_on_vm(
                    _exec, api_routes, f"hc-{vs.variant_key}"
                )

                if not all(c["pass"] for c in verify_checks):
                    failed_checks = [c for c in verify_checks if not c["pass"]]
                    failed_names = [c["name"] for c in failed_checks]
                    logger.error(
                        "HILLCLIMB [%s] verify failed (%s) — agent should "
                        "have caught this during self-validation",
                        vs.variant_key,
                        ", ".join(failed_names),
                    )
                    for fc in failed_checks:
                        logger.error(
                            "HILLCLIMB [%s] check '%s' error: %s",
                            vs.variant_key,
                            fc["name"],
                            fc.get("error", "")[:1000],
                        )
                    return None

                # ── Seed + Snapshot (new artifact on existing sim) ────
                await self._seed_api_routes(_exec, api_routes, f"hc-{vs.variant_key}")
                flows_yaml = self._build_flows_yaml(vs.variant_key)
                try:
                    new_artifact_id = await self._take_snapshot(
                        http,
                        session_id,
                        api_key,
                        base_sim_name,
                        flows_yaml,
                        f"hc-{vs.variant_key}",
                    )
                except RuntimeError as e:
                    logger.error("HILLCLIMB [%s] %s", vs.variant_key, e)
                    return None

                logger.info(
                    "HILLCLIMB [%s] new artifact %s on sim '%s'",
                    vs.variant_key,
                    new_artifact_id,
                    base_sim_name,
                )
                return new_artifact_id

            finally:
                if session_id:
                    try:
                        await sessions_close.asyncio(
                            client=http,
                            session_id=session_id,
                            x_api_key=api_key,
                        )
                    except Exception:
                        pass

    async def _hc_rerun_benchmark(
        self, vs: VariantStatus, testcase_ids: list[str]
    ) -> list[dict]:
        """Re-run sessions for the new testcases and return results."""
        from plato.chronos.api.jobs import launch_job
        from plato.chronos.api.sessions import get_session, get_session_status
        from plato.chronos.models import (
            LaunchJobRequest,
            VMResources,
            WorldConfigInput,
            WorldRuntimeConfig,
        )

        config = self.config
        n_sessions = config.sessions_per_testcase
        sem = asyncio.Semaphore(config.run_concurrency)
        results: list[dict] = []
        results_lock = asyncio.Lock()

        async with httpx.AsyncClient(
            base_url=config.chronos_url, timeout=httpx.Timeout(120.0)
        ) as http:

            async def _run_one(tc_id: str, session_num: int, idx: int) -> None:
                await asyncio.sleep(idx * 2.0)
                async with sem:
                    for attempt in range(1, 4):
                        try:
                            result = await self._launch_and_wait(
                                http,
                                config,
                                tc_id,
                                f"hc-{vs.variant_key}",
                                launch_job,
                                get_session_status,
                                get_session,
                                LaunchJobRequest,
                                WorldConfigInput,
                                WorldRuntimeConfig,
                                VMResources,
                            )
                            result["session_num"] = session_num
                            result["attempt"] = attempt

                            result["outcome"] = self._classify_session_outcome(result)
                            is_infra = result.get("status", "") in (
                                "failed",
                                "error",
                                "cancelled",
                            )

                            if is_infra and attempt < 3:
                                continue

                            async with results_lock:
                                results.append(result)
                            break

                        except Exception as e:
                            logger.error(
                                "HILLCLIMB [%s] session error (attempt %d): %s",
                                vs.variant_key,
                                attempt,
                                e,
                            )
                            if attempt >= 3:
                                async with results_lock:
                                    results.append(
                                        {
                                            "testcase_id": tc_id,
                                            "outcome": "ERROR",
                                            "error": str(e),
                                        }
                                    )

            tasks = []
            idx = 0
            for tc_id in testcase_ids:
                for sn in range(1, n_sessions + 1):
                    tasks.append(_run_one(tc_id, sn, idx))
                    idx += 1

            logger.info(
                "HILLCLIMB [%s] launching %d sessions for %d testcases",
                vs.variant_key,
                len(tasks),
                len(testcase_ids),
            )
            await asyncio.gather(*tasks)

        return results

    def _log_hillclimb_summary(self, target: float) -> None:
        """Log a structured per-testcase summary of hillclimb results."""
        logger.info("=" * 70)
        logger.info("HILLCLIMB SUMMARY (per-testcase)")
        logger.info("=" * 70)
        logger.info("Target max pass rate: %.1f%%", target * 100)
        logger.info("-" * 70)

        for vs in self.state.variants:
            if not vs.testcase_hillclimb_state:
                continue

            total_tc = len(vs.testcase_hillclimb_state)
            met_count = sum(
                1
                for s in vs.testcase_hillclimb_state.values()
                if s.best_pass_rate <= target
            )
            logger.info(
                "VARIANT: %s  [%d/%d testcases at target]",
                vs.variant_key,
                met_count,
                total_tc,
            )

            for tc_id, tc_state in vs.testcase_hillclimb_state.items():
                met = "OK" if tc_state.best_pass_rate <= target else "OVER"
                n_iters = len(tc_state.iterations) - 1
                logger.info(
                    "  TC %s [%s] %.1f%% → %.1f%% (%d iterations)",
                    tc_id[:12],
                    met,
                    tc_state.original_pass_rate * 100,
                    tc_state.best_pass_rate * 100,
                    n_iters,
                )
                for idx, it in enumerate(tc_state.iterations):
                    marker = " <- best" if idx == tc_state.best_iteration_idx else ""
                    edits = f"  edits: {it.edits_summary}" if it.edits_summary else ""
                    logger.info(
                        "    iter %d: %.1f%% (tc=%s, artifact=%s)%s%s",
                        it.iteration,
                        it.pass_rate * 100,
                        it.testcase_id[:12],
                        it.artifact_id[:12] if it.artifact_id else "n/a",
                        marker,
                        edits,
                    )
        logger.info("=" * 70)

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

    async def _load_tasks_from_api(self) -> None:
        """Fetch testcase details from Plato API for variants missing local tasks.

        Fallback for resume_variants where tasks.json doesn't exist on disk.
        """
        from plato._generated.api.v1.testcases import get_testcases

        config = self.config
        if not config.plato_api_key:
            return

        async with httpx.AsyncClient(
            base_url=config.plato_api_url,
            timeout=httpx.Timeout(30.0),
        ) as http:
            for vs in self.state.variants:
                if (
                    vs.variant_key in self._all_tasks
                    and self._all_tasks[vs.variant_key]
                ):
                    continue
                if not vs.testcase_ids:
                    continue

                tasks: list[dict] = []
                for tc_id in vs.testcase_ids:
                    try:
                        resp = await get_testcases.asyncio(
                            client=http,
                            test_case_public_id=tc_id,
                            x_api_key=config.plato_api_key,
                            page_size=1,
                        )
                        testcases = resp.get("testcases", [])
                        if not testcases:
                            tasks.append({"_testcase_id": tc_id})
                            continue
                        tc = testcases[0]
                        scoring_config = tc.get("defaultScoringConfig", {})
                        v2_config = tc.get("v2ScoringConfig")
                        if v2_config:
                            scoring_config["v2_scoring_config"] = v2_config
                        tasks.append(
                            {
                                "_testcase_id": tc_id,
                                "name": tc.get("name", ""),
                                "instruction": tc.get("prompt", ""),
                                "start_url": tc.get("startUrl", "/"),
                                "scoring_type": (
                                    tc.get("scoringTypes", ["output"])[0]
                                    if tc.get("scoringTypes")
                                    else "output"
                                ),
                                "expected_output": tc.get("expectedOutput"),
                                "output_schema": tc.get("outputSchema"),
                                "scoring_config": scoring_config,
                            }
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to fetch testcase %s from API: %s", tc_id, e
                        )
                        tasks.append({"_testcase_id": tc_id})

                if tasks:
                    self._all_tasks[vs.variant_key] = tasks
                    logger.info(
                        "Loaded %d tasks from API for %s", len(tasks), vs.variant_key
                    )

    # ------------------------------------------------------------------
    # DRY RUN CLEANUP: archive testcases + set sims out of service
    # ------------------------------------------------------------------

    async def _cleanup_dry_run(self) -> None:
        """Archive all testcases and set sims out of service for this run.

        Collects every artifact_id, testcase_id, and sim_name produced during
        the pipeline (including hillclimb iterations), then tears everything
        down via the Plato API so the run leaves no active resources behind.
        """
        config = self.config
        variants = self.state.variants

        all_sim_names: set[str] = set()
        all_testcase_public_ids: set[str] = set()
        all_artifact_ids: set[str] = set()

        for vs in variants:
            if vs.sim_name:
                all_sim_names.add(vs.sim_name)
            if vs.artifact_id:
                all_artifact_ids.add(vs.artifact_id)
            for tc_id in vs.testcase_ids:
                if tc_id:
                    all_testcase_public_ids.add(tc_id)

            for tc_state in vs.testcase_hillclimb_state.values():
                if tc_state.best_testcase_id:
                    all_testcase_public_ids.add(tc_state.best_testcase_id)
                for it in tc_state.iterations:
                    if it.testcase_id:
                        all_testcase_public_ids.add(it.testcase_id)
                    if it.artifact_id:
                        all_artifact_ids.add(it.artifact_id)

        logger.info("=" * 60)
        logger.info("DRY RUN CLEANUP")
        logger.info("=" * 60)
        logger.info(
            "Resources to clean up: %d sims, %d testcase IDs, %d artifact IDs",
            len(all_sim_names),
            len(all_testcase_public_ids),
            len(all_artifact_ids),
        )
        for name in sorted(all_sim_names):
            logger.info("  sim: %s", name)

        if not all_sim_names:
            logger.info("No sims to clean up, skipping dry-run cleanup")
            return

        async with httpx.AsyncClient(
            base_url=config.plato_api_url,
            timeout=httpx.Timeout(30.0),
            headers={"X-API-Key": config.plato_api_key},
        ) as http:
            # Step 1: Resolve sim names → numeric sim IDs
            sim_name_to_id: dict[str, int] = {}
            try:
                resp = await http.get("/api/v1/simulator/list")
                resp.raise_for_status()
                for s in resp.json():
                    if s.get("name") in all_sim_names:
                        sim_name_to_id[s["name"]] = s["id"]
            except Exception as e:
                logger.error("Failed to fetch simulator list: %s", e)
                return

            logger.info(
                "Resolved %d/%d sim names to IDs",
                len(sim_name_to_id),
                len(all_sim_names),
            )
            for name, sid in sorted(sim_name_to_id.items()):
                logger.info("  %s -> ID %d", name, sid)

            unresolved = all_sim_names - set(sim_name_to_id.keys())
            if unresolved:
                logger.warning("Could not resolve sims: %s", sorted(unresolved))

            # Step 2: Find testcase numeric IDs linked to our sims
            sim_id_set = set(sim_name_to_id.values())
            tc_numeric_ids: list[int] = []
            page = 1
            page_size = 200
            total_pages = None

            while sim_id_set:
                try:
                    resp = await http.get(
                        "/api/v1/testcases",
                        params={
                            "simulator_name": config.sim_name_prefix,
                            "page": page,
                            "page_size": page_size,
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    logger.error("Failed to fetch testcases page %d: %s", page, e)
                    break

                pagination = data.get("pagination", {})
                if total_pages is None:
                    total_pages = pagination.get("total_pages", 1)

                for tc in data.get("testcases", []):
                    tc_sim_ids = set(tc.get("testcaseSimulatorIds") or [])
                    tc_public_id = tc.get("publicId") or tc.get("public_id", "")
                    if (tc_sim_ids & sim_id_set) or (
                        tc_public_id in all_testcase_public_ids
                    ):
                        tc_numeric_ids.append(tc["id"])

                if page >= (total_pages or 1):
                    break
                page += 1

            logger.info("Found %d testcases to archive", len(tc_numeric_ids))

            # Step 3: Bulk archive testcases
            batch_size = 50
            archived_ok = 0
            archived_fail = 0
            for i in range(0, len(tc_numeric_ids), batch_size):
                batch = tc_numeric_ids[i : i + batch_size]
                try:
                    resp = await http.post(
                        "/api/v1/testcases/bulk-archive",
                        json={"test_case_ids": batch},
                    )
                    resp.raise_for_status()
                    archived_ok += len(batch)
                except Exception as e:
                    logger.error(
                        "Failed to archive batch %d-%d: %s", i, i + len(batch), e
                    )
                    archived_fail += len(batch)

            logger.info(
                "Archived testcases: %d ok, %d failed", archived_ok, archived_fail
            )

            # Step 4: Set sims to out_of_service
            oos_ok = 0
            oos_fail = 0
            for sim_name, sim_id in sorted(sim_name_to_id.items()):
                try:
                    resp = await http.post(
                        f"/api/v1/simulator/{sim_id}/status",
                        json={"status": "out_of_service"},
                    )
                    resp.raise_for_status()
                    oos_ok += 1
                    logger.info(
                        "  Set sim '%s' (ID %d) to out_of_service", sim_name, sim_id
                    )
                except Exception as e:
                    logger.warning(
                        "  Failed to set sim '%s' (ID %d) OOS: %s",
                        sim_name,
                        sim_id,
                        e,
                    )
                    oos_fail += 1

            logger.info("Sim status updates: %d ok, %d failed", oos_ok, oos_fail)

        logger.info("=" * 60)
        logger.info(
            "DRY RUN CLEANUP COMPLETE — %d testcases archived, %d sims set OOS",
            archived_ok,
            oos_ok,
        )
        logger.info("Tracked artifact IDs: %s", sorted(all_artifact_ids))
        logger.info("=" * 60)

    def _build_summary(self) -> str:
        lines = [
            "=" * 72,
            "SKILL TEST GENERATOR — FINAL SUMMARY",
            "=" * 72,
            f"Skills loaded: {self.state.skills_loaded}",
            f"Total variants: {len(self.state.variants)}",
            "",
        ]

        # Group variants by skill
        from collections import defaultdict

        skill_variants: dict[str, list] = defaultdict(list)
        for vs in self.state.variants:
            skill_variants[vs.skill_name].append(vs)

        overall_pass = 0
        overall_fail = 0
        overall_error = 0
        finalized_sims = 0

        for skill_name, variants in skill_variants.items():
            lines.append(f"SKILL: {skill_name}")
            lines.append("-" * 72)

            for vs in variants:
                # Compute pass rate for this variant
                v_pass = sum(1 for t in vs.task_results if t.get("outcome") == "PASS")
                v_fail = sum(1 for t in vs.task_results if t.get("outcome") == "FAIL")
                v_error = sum(
                    1
                    for t in vs.task_results
                    if t.get("outcome") == "ERROR"
                    or t.get("status") in ("failed", "error", "cancelled")
                )
                v_completed = v_pass + v_fail
                v_rate = (
                    f"{v_pass}/{v_completed} ({v_pass / v_completed * 100:.0f}%)"
                    if v_completed
                    else "N/A"
                )

                lines.append(
                    f"  SPEC: {vs.variant_key}  "
                    f"[sim: {vs.sim_name or 'N/A'}]  "
                    f"pass_rate={v_rate}" + (f"  ({v_error} errors)" if v_error else "")
                )

                if v_completed > 0:
                    finalized_sims += 1

                overall_pass += v_pass
                overall_fail += v_fail
                overall_error += v_error

                # Per-testcase breakdown
                tc_results: dict[str, list[dict]] = defaultdict(list)
                for tr in vs.task_results:
                    tc_key = tr.get("testcase_id", tr.get("task_name", "unknown"))
                    tc_results[tc_key].append(tr)

                for tc_id, results in tc_results.items():
                    task_name = results[0].get("task_name", "unknown")
                    tc_pass = sum(1 for r in results if r.get("outcome") == "PASS")
                    tc_fail = sum(1 for r in results if r.get("outcome") == "FAIL")
                    tc_err = sum(
                        1
                        for r in results
                        if r.get("outcome") == "ERROR"
                        or r.get("status") in ("failed", "error", "cancelled")
                    )
                    tc_done = tc_pass + tc_fail
                    tc_rate = f"{tc_pass}/{tc_done}" if tc_done else "N/A"

                    lines.append(
                        f"    TESTCASE: {task_name}  [{tc_id[:16]}]  "
                        f"pass={tc_rate}" + (f"  ({tc_err} errors)" if tc_err else "")
                    )

                    for r in results:
                        outcome = r.get("outcome", "?")
                        attempt = r.get("attempt", 1)
                        is_retry = attempt > 1 or outcome == "ERROR"
                        chronos = r.get("chronos_url", "")
                        plato = r.get("plato_url", "")
                        if is_retry and outcome == "ERROR":
                            lines.append(
                                f"      [attempt {attempt}] {outcome}  "
                                f"chronos={chronos}"
                            )
                        else:
                            lines.append(
                                f"      [attempt {attempt}] {outcome}  "
                                f"chronos={chronos}  plato={plato}"
                            )

                # Hillclimb iterations
                if vs.testcase_hillclimb_state:
                    lines.append("    HILLCLIMB ITERATIONS:")
                    for hc_tc_id, tc_state in vs.testcase_hillclimb_state.items():
                        lines.append(
                            f"      TC {hc_tc_id[:16]}: "
                            f"{tc_state.original_pass_rate * 100:.0f}% → "
                            f"{tc_state.best_pass_rate * 100:.0f}% "
                            f"({len(tc_state.iterations) - 1} retries)"
                        )
                        for idx, it in enumerate(tc_state.iterations):
                            best = (
                                " ← BEST" if idx == tc_state.best_iteration_idx else ""
                            )
                            summary = (
                                f" | {it.edits_summary[:80]}"
                                if it.edits_summary
                                else ""
                            )
                            lines.append(
                                f"        iter {it.iteration}: "
                                f"{it.pass_rate * 100:.0f}% "
                                f"(tc={it.testcase_id[:12]}, "
                                f"artifact={it.artifact_id[:12] if it.artifact_id else 'n/a'})"
                                f"{best}{summary}"
                            )
                            for hr in it.task_results:
                                h_outcome = hr.get("outcome", "?")
                                h_chronos = hr.get("chronos_url", "")
                                h_plato = hr.get("plato_url", "")
                                lines.append(
                                    f"          [{h_outcome}] "
                                    f"chronos={h_chronos}  plato={h_plato}"
                                )

            lines.append("")

        # Overall summary
        lines.append("=" * 72)
        lines.append("OVERALL")
        lines.append("=" * 72)
        total_completed = overall_pass + overall_fail
        if total_completed:
            lines.append(
                f"Pass rate (excl. errors): {overall_pass}/{total_completed} "
                f"= {overall_pass / total_completed * 100:.1f}%"
            )
        lines.append(
            f"Total: {overall_pass} PASS | {overall_fail} FAIL | {overall_error} ERROR"
        )
        lines.append(f"Finalized sims (with completed sessions): {finalized_sims}")
        lines.append("=" * 72)

        return "\n".join(lines)

    async def close(self) -> None:
        await super().close()
