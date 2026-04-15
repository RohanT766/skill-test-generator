"""Configuration and state for the skill-test-generator world."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field

from plato.markers import Agent

try:
    from plato.markers import WorkspaceMarker
except ImportError:

    class WorkspaceMarker:  # type: ignore[no-redef]
        def __init__(self, **kwargs: object):
            pass


from plato.worlds import AgentConfig, Secret
from plato.worlds.config import RunConfig


class Stage(str, Enum):
    INGEST = "ingest"
    DESIGN = "design"
    CODEGEN = "codegen"
    TASKS = "tasks"
    PUBLISH = "publish"
    RUN = "run"
    EVALUATE = "evaluate"


class SkillDefinition(BaseModel):
    """A single testable skill gap identified by benchmark-review."""

    name: str = Field(
        description="Capability name, e.g. 'Navigate paginated list exhaustively to find global extremum'"
    )
    short_name: str | None = Field(
        default=None,
        description="2-3 word abbreviated name for compact slugs, e.g. 'paginate-find-extremum'",
    )
    description: str = Field(
        default="",
        description="One-sentence minimal UI reproducer describing the reasoning challenge",
    )
    testable: bool = Field(
        default=True,
        description="Whether a targeted simulator can isolate this failure",
    )
    session_ids: list[str] = Field(
        default_factory=list,
        description="Original failing session IDs from benchmark-review",
    )
    sim_sources: list[str] = Field(
        default_factory=list,
        description="Simulator names where this skill was observed",
    )


class VariantStatus(BaseModel):
    """Tracks generation progress for a single skill variant."""

    skill_name: str
    short_name: str = ""
    slug: str = ""
    stage: str = "pending"
    sim_name: str = ""
    app_port: int = 0
    error: str = ""
    task_count: int = 0
    eval_results: dict = Field(default_factory=dict)
    artifact_id: str = ""
    testcase_ids: list[str] = Field(default_factory=list)
    chronos_session_ids: list[str] = Field(default_factory=list)
    plato_session_ids: list[str] = Field(default_factory=list)
    task_results: list[dict] = Field(default_factory=list)


class AgentModel(str, Enum):
    NOVA_ACT = "amazon/nova-act"
    CLAUDE = "anthropic/claude-sonnet-4-5-20250929"


class SkillTestGeneratorConfig(RunConfig):
    """Configuration for the skill-test-generator world.

    Provide a list of skill capabilities to test. The world will:
    1. Generate a specialized web app for each skill
    2. Build, publish it as a Plato simulator, and create testcases
    3. Launch CUA benchmark sessions via Chronos to evaluate the agent
    4. Report pass/fail per testcase with Chronos + Plato session links
    """

    s3_skills: list[str] = Field(
        default_factory=list,
        description=(
            "Skill names to look up in S3 benchmark-review data. "
            "Each name must match a skill_category in "
            "s3://plato-browser-session-data-prod/benchmark-analysis/*.json. "
            "Skills not found in S3 are skipped with a warning."
        ),
    )
    custom_skills: list[SkillDefinition] = Field(
        default_factory=list,
        description=(
            "Fully user-defined skills. Each entry must have 'name' and "
            "'description'. These skip S3 lookup entirely."
        ),
    )

    max_skills: int = Field(default=20, ge=1)
    specs_per_skill: int = Field(
        default=1,
        ge=1,
        description=(
            "Number of distinct app specifications to generate per skill. "
            "Each spec produces a different realistic scenario (e.g. an order "
            "management dashboard vs. a contact directory) that tests the same "
            "underlying skill. Set to 2+ for broader coverage."
        ),
    )
    output_tasks_per_variant: int = Field(
        default=3,
        ge=0,
        description=(
            "Number of OUTPUT-scored tasks to generate per variant. "
            "Output tasks require the agent to return a JSON answer."
        ),
    )
    mutation_tasks_per_variant: int = Field(
        default=3,
        ge=0,
        description=(
            "Number of MUTATION-scored tasks to generate per variant. "
            "Mutation tasks require the agent to modify data via the UI. "
            "Set to 0 if the app has no write endpoints."
        ),
    )
    sessions_per_testcase: int = Field(
        default=1,
        ge=0,
        description=(
            "Number of agent sessions to run per testcase in the RUN stage. "
            "Set to 0 to skip the RUN stage entirely. "
            "Higher values give more statistical confidence but cost more."
        ),
    )
    run_concurrency: int = Field(
        default=30,
        ge=1,
        description=(
            "Max concurrent CUA benchmark sessions during the RUN stage. "
            "Acts as a semaphore to prevent overwhelming Chronos."
        ),
    )

    stage: Stage | None = Field(
        default=None,
        description="Run only a specific stage. None runs all stages sequentially.",
    )
    resume_variants: list[dict] | None = Field(
        default=None,
        description=(
            "Pre-existing variant data to resume from. Each dict needs "
            "skill_name, slug, sim_name, artifact_id, testcase_ids. "
            "When set, skips INGEST/DESIGN/CODEGEN and runs RUN+EVALUATE."
        ),
    )
    design_concurrency: int = Field(
        default=10,
        ge=1,
        description="Max concurrent LLM API calls during the DESIGN stage.",
    )

    template_name: str = Field(default="sohan")
    sim_name_prefix: str = Field(default="skill-test")
    design_model: str = Field(default="claude-opus-4-6")

    codegen_verify_port: int = Field(
        default=4000,
        description="Port the claude-code agent uses to run the dev server for verification.",
    )
    vm_concurrency: int = Field(
        default=10,
        ge=1,
        description=(
            "Max concurrent per-variant pipeline VMs. Each variant's "
            "verify + build + publish runs on its own isolated VM."
        ),
    )
    pipeline_vm_cpus: int = Field(
        default=2,
        ge=1,
        le=8,
        description="CPUs allocated to each per-variant pipeline VM (max 8).",
    )
    pipeline_vm_memory: int = Field(
        default=4096,
        ge=512,
        le=16384,
        description="Memory in MB for each per-variant pipeline VM (max 16384).",
    )
    eval_agent_model: str = Field(
        default="amazon/nova-act",
        description="LLM model the eval agent uses internally (e.g. amazon/nova-act, anthropic/claude-sonnet-4-5-20250929).",
    )
    eval_max_turns: int = Field(default=99)
    eval_display_width: int = Field(default=1600)
    eval_display_height: int = Field(default=813)
    record_sessions: bool = Field(default=True)

    coder_agent: Annotated[
        AgentConfig | None,
        Agent(
            description="Claude Code agent for finishing/fixing pre-generated variant code on Chronos VMs",
            required=False,
        ),
    ] = Field(
        default_factory=lambda: AgentConfig(
            package="claude-code:latest",
            config={
                "model_name": "claude-opus-4-6",
                "max_turns": 75,
            },
        )
    )

    code: Annotated[
        Path,
        WorkspaceMarker(
            description="Generated variant code workspace",
            tracked=False,
            mount_path="/workspace/code",
        ),
    ] = Path("/workspace/code")
    output: Annotated[
        Path,
        WorkspaceMarker(
            description="Output workspace: skills, specs, tasks, eval results",
            tracked=False,
            mount_path="/workspace/output",
        ),
    ] = Path("/workspace/output")

    plato_api_key: Annotated[
        str, Secret(description="Plato API key", required=True)
    ] = "${PLATO_API_KEY}"
    anthropic_api_key: Annotated[
        str, Secret(description="Anthropic API key", required=True)
    ] = "${ANTHROPIC_API_KEY}"
    aws_access_key_id: Annotated[str, Secret(description="AWS access key")] = ""
    aws_secret_access_key: Annotated[str, Secret(description="AWS secret key")] = ""
    aws_session_token: Annotated[str, Secret(description="AWS session token")] = ""
    nova_act_api_key: Annotated[str, Secret(description="Nova Act API key")] = ""
    nova_act_workflow_name: str = Field(
        default="plato-cua-benchmark",
        description="Nova Act workflow name for IAM-based auth (used when nova_act_api_key is empty).",
    )

    base_artifact_id: str = Field(
        default="",
        description=(
            "Artifact ID of a working base sim to boot from. "
            "New variants are deployed by replacing code inside this base. "
            "If empty, creates from a blank VM (slower, may not have warm supply)."
        ),
    )
    plato_api_url: str = Field(default="https://plato.so")
    chronos_url: str = Field(default="https://chronos.plato.so")

    cua_world_package: str = Field(
        default="plato-world-cua-benchmark:3.1.58",
        description="CUA benchmark world package for agent evaluation.",
    )
    cua_agent_package: str = Field(
        default="computer-use-agent:3.2.48",
        description="Computer-use agent package.",
    )


class SkillTestGeneratorState(BaseModel):
    """Tracks pipeline progress across steps."""

    current_stage: Stage = Stage.INGEST
    skills_loaded: int = 0
    variants: list[VariantStatus] = Field(default_factory=list)
    stage_completed: dict[str, bool] = Field(default_factory=dict)
