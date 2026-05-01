# Skill Test Generator

An autonomous pipeline that mass-produces realistic interactive environments for evaluating targeted reasoning failures in frontier AI agents.

The core problem: frontier AI agents (models that operate computers by seeing screenshots and taking actions) have specific, recurring reasoning failures — they skip pagination and only check the first page, they misread truncated text in table cells, they confuse similarly-named entities in dropdown menus. These failures are hard to test for because each one requires a purpose-built application with carefully structured data designed to expose the specific gap. This pipeline generates those applications at scale.

Given a list of targeted skill gaps, the system:

1. **Generates full-stack web applications** — each one a realistic business tool (CRM, analytics dashboard, inventory system) with adversarially structured data that punishes agents lacking the targeted skill
2. **Deploys and snapshots them** as restorable VM environments that agents interact with through a browser
3. **Creates testcases** — specific questions whose correct answers require exercising the skill (e.g., "What product has the lowest inventory count?" when the answer is on page 3 of a paginated table, and a decoy with a slightly higher count sits on page 1)
4. **Verifies solvability** by running separate verification agents with hints to confirm the task is actually possible
5. **Benchmarks difficulty** by running evaluation agents against each testcase and measuring pass rates
6. **Iteratively tunes difficulty** — a coding agent analyzes why evaluation agents succeed, then modifies the application to prevent that strategy, rebuilds, and re-benchmarks until agents fail reliably

The entire pipeline runs end-to-end without human intervention. A single launch can produce dozens of environments in parallel, each targeting a different reasoning failure.

This project is built on [Plato's](https://plato.so) infrastructure for VM orchestration, environment snapshotting, and benchmark execution.

## How It Works

The system runs as a single orchestrator VM that coordinates dozens of parallel sub-pipelines. Each skill gap spawns one or more **variants** — independent web applications that test the same underlying reasoning failure in different real-world contexts (e.g., an order management dashboard, an HR directory, and a financial ledger all testing "paginate before aggregating"). Every variant flows through six stages:

```
INGEST → DESIGN → CODEGEN → RUN → EVALUATE → HILLCLIMB
```

All variants run in parallel. Once a variant finishes code generation, it immediately flows into benchmarking and difficulty tuning without waiting for other variants.

### Compute Resources

The pipeline orchestrates four types of VMs simultaneously:

- **Orchestrator VM**: Runs this pipeline — coordinates everything
- **Pipeline VMs**: Per-variant VMs that build, verify, and snapshot each generated web application
- **Coding Agent VMs**: Isolated VMs running Claude Code for code generation, automatic bug fixing, and hillclimb modifications
- **Benchmark VMs**: Pairs of VMs for each evaluation session — one restores the application from a frozen snapshot, the other runs the evaluation agent (Amazon Nova Act) that interacts with it through a browser

At peak concurrency, a single run can have 20+ VMs active simultaneously across different stages.

## Pipeline Stages

### Stage 1: Ingest

Skills are loaded from two sources:

**Upstream analysis data**: A separate benchmark-review pipeline analyzes agent failures across hundreds of sessions and clusters them into named skill categories stored in S3. This stage loads those categories, doing case-insensitive name matching and deduplication across all analysis files. Each skill comes with the name, description, and IDs of the original failing sessions that identified the gap.

**Custom definitions**: Skills defined directly in the launch config with a name and description.

For any skill missing a compact identifier, the pipeline generates a `short_name` via an LLM call (e.g., "Exhaustively paginate list before identifying extremum record" → `paginate-find-extremum`) and persists it for future runs.

Once skills are loaded, variants are created: with `specs_per_skill=6`, one skill produces six independently developed applications, each in a different industry and visual style but all targeting the same reasoning failure.

### Stage 2: Design

Each variant gets a single LLM call (Claude Opus) that produces a complete application specification. The design prompt frames the app as an adversarial trap — it must actively exploit the skill gap, not just vaguely relate to it.

**Reference screenshots**: The pipeline ships with a curated library of real application screenshots. Before the design call, a filtering LLM call selects relevant content types and industries for the skill being tested. A matching screenshot is attached to the design prompt as the primary visual blueprint — the LLM replicates its navigation chrome, color scheme, typography density, and spacing. This produces environments that look like real products rather than synthetic test fixtures.

**Adversarial data design**: The design prompt enforces specific principles:

- **Decoy records**: Seed data is structured so that an agent lacking the skill will confidently return a wrong answer. The correct answer is placed where the skill demands looking — behind pagination, inside truncated cells, under ambiguous navigation tabs — never where a shortcut would find it. The gap between the decoy and the correct value is kept small enough that the agent cannot reason its way out without actually exercising the skill.
- **Critical UI details**: Specific interface requirements that must be present for the skill test to work (e.g., "table must truncate cells at 15 characters").
- **Forbidden features**: UI elements that must not exist because they would let the agent bypass the skill entirely (e.g., "no sort-by-value button" for an aggregation skill, "no search bar" for a pagination skill).

The design LLM returns a complete application specification: `app_name`, `title`, `description`, `scenario`, `skill_tested`, `db_schema`, `seed_data` (20–30 deterministic records), `pages`, `api_routes`, `critical_ui_details`, `forbidden_features`, `edit_capabilities`, `icon_svg`, `color_palette`, and `chrome_description`. When generating multiple variants for the same skill, prior designs are included in the prompt to ensure completely different industries, layouts, and color schemes.

### Stage 3: Codegen

The most complex stage. For each variant, the pipeline generates a working Next.js web application, deploys it on a VM, verifies it, takes a frozen snapshot, generates testcases, runs solvability verification, and publishes everything.

#### Code Generation

Starting from a pre-built Next.js scaffold (Drizzle ORM + PGlite, shadcn/ui, TanStack Query, mutation logging), a single LLM call generates all custom application code: database schema, seed data, migration SQL, page components, and API route handlers. The same reference screenshot from the design stage is included for visual fidelity.

Key constraints enforced by the prompt:

- Implement `critical_ui_details` and `forbidden_features` exactly — these are what make the environment adversarial
- Seed data values and placements must match the design spec precisely — the adversarial trap depends on specific data ordering
- Every database write route must call `logMutation()` after success — this powers scoring for write-operation tasks
- Migration SQL must use PGlite-compatible syntax with `statement-breakpoint` separators
- No hardcoded `localhost` URLs — all API calls go through relative-URL helpers

Generated code undergoes local validation before touching a VM: missing required files, schema/import mismatches, JSX issues, and mutation-logging presence are all checked. Validation errors are recorded and passed to the fix agent if needed.

#### Build, Verify, and Snapshot

A dedicated pipeline VM is created for each variant. The generated code is tarballed, uploaded to S3, and extracted on the VM. Then:

1. **Install + Build**: `bun install` → `next build` → `next start`
2. **Verify**: Health check, every API route returns 200 with non-empty data, no hardcoded localhost URLs in page HTML
3. **Fetch live data**: While the server is running, every list endpoint is paginated through (up to 20 pages, ~20k chars per route). This real JSON is used during testcase generation so testcases reference actual database values
4. **Register + Snapshot**: The environment is registered in the platform catalog, then a full VM snapshot captures the running state — compiled build, dependencies, seeded database, systemd service. This produces an **artifact ID** — a frozen reference point. Every future benchmark session restores this exact snapshot, so all agents face identical conditions
5. **Prefetch**: The snapshot is warmed into the VM pool for fast boot times

**Automatic code fixing**: If the build or verification fails with a code error (not infrastructure), a Claude Code agent is launched on its own VM. It receives the full spec, which files were generated, pre-VM validation errors, and exact check results from the VM (which API routes returned errors, what status codes, what responses). After the agent produces fixes, the full build/verify/snapshot process retries. Code fix agents run at most once per variant; infrastructure failures get up to 3 retries.

#### Testcase Generation

Once the snapshot exists, testcases are generated off-VM. Claude receives the variant spec and the live API data fetched during verification. Each testcase is a specific question that an agent must answer by interacting with the application through a browser:

**Output tasks**: The agent must navigate the app and return a specific JSON answer. The generation prompt is adversarial: the LLM picks the data point where a naive agent — one that takes the obvious shortcut instead of exercising the skill — will fail. It also verifies there is no bypass path in the UI that would leak the answer.

Each testcase includes:
- **`instruction`**: The shortest unambiguous question requiring the skill. Never mentions the mechanism (pagination, scrolling, hidden content) — the agent must discover it
- **`hint`**: Navigation strategy and trap description without revealing expected values. Used during solvability verification so that verification agents can succeed consistently
- **`expected_output`**: The correct answer, computed from live API data

#### Solvability Verification

When enabled, each testcase is validated before publishing. Multiple independent evaluation sessions run the task with the instruction and hint. The pipeline checks for multi-session consensus: enough sessions must return an identical non-null output (up to 3, scaled down for smaller session counts). If they agree, that consensus output becomes the scoring configuration. If they disagree, the testcase is dropped — the task is either ambiguous or the environment has a bug. This gate ensures every published testcase is actually solvable and has a deterministic correct answer.

#### Publish

Surviving testcases are published to the platform. Each stores the instruction, hint, start URL, artifact ID, scoring configuration, and skill tags. Testcase names are deduplicated — if a name collision is detected across any environment, `-v2`, `-v3`, etc. suffixes are appended.

### Stage 4: Run

For every published testcase, independent evaluation sessions are launched. Each session restores the environment from its snapshot — the exact same compiled build, seeded database, and systemd service — and runs an evaluation agent that interacts with the application through a browser. Sessions are staggered and retry on infrastructure failures. Each session creates two VMs: one for the environment, one for the agent.

### Stage 5: Evaluate

Pass rates are computed from evaluation results. A session is PASS if the agent returned the correct answer; ERROR outcomes (infrastructure failures) are excluded from the denominator. This is pure computation — no VMs are involved.

### Stage 6: Hillclimb

The difficulty-tuning loop. Testcases where agents pass too easily are iteratively made harder until they reach a target failure rate.

The threshold is derived from the configuration: with `sessions_per_testcase=4` and `total_failures=1`, the target max pass rate is 75% — at least 1 out of 4 sessions must fail. Testcases above this threshold are processed sequentially within each variant, with up to `max_retries` iterations per testcase.

Each iteration:

1. **Fetch trajectories**: Full step-by-step agent session logs are downloaded — every action the agent took, every page it navigated, every value it extracted
2. **Assemble workspace**: A directory is built with the environment source code, testcase definitions, evaluation results, agent trajectories, and summaries of any prior failed iterations
3. **Run coding agent**: A Claude Code agent analyzes why the evaluation agent succeeded and modifies the environment to prevent that strategy. The primary lever is structural — adding more pages for pagination skills, introducing near-identical entities for disambiguation skills, scattering data across more tabs for aggregation skills. Prompt-only changes (making instructions vaguer or more convoluted) are explicitly discouraged — the goal is to make the task genuinely harder, not unfairly obscure. When environment code is changed, the agent must successfully build and verify it before submitting; testcase-only edits skip the build step
4. **Rebuild + Snapshot**: If the environment code changed, a new pipeline VM boots from the current artifact, applies the edits, rebuilds, verifies, and produces a new snapshot
5. **Verify + Publish**: The modified testcase goes through the same solvability verification to confirm the task is still possible with the hint, then is published. The old testcase is archived
6. **Re-benchmark**: Fresh evaluation sessions are launched against the new snapshot
7. **Score + Decide**: If the new pass rate is at or below the target, the testcase is done. If still too high, the next iteration begins using the updated testcase and snapshot as the new baseline. Prior iteration summaries are passed to the next coding agent so it avoids repeating failed strategies

## Project Structure

```
├── src/skill_test_generator/
│   ├── world.py              # Pipeline orchestrator (~4400 lines)
│   ├── config.py             # Pydantic configuration and state tracking models
│   ├── variant_generator.py  # LLM-driven app design + code generation
│   ├── task_generator.py     # Adversarial testcase generation from live data
│   ├── skill_ingestion.py    # Skill loading, deduplication, short name generation
│   ├── codegen_agent.py      # Fix-agent instruction builder for failed builds
│   ├── prompts.py            # System prompts for design, codegen, and hillclimb
│   ├── json_utils.py         # Robust JSON extraction from LLM output
│   └── schema.json           # Configuration JSON schema
├── templates/
│   ├── sohan/                 # Next.js application scaffold (Drizzle + shadcn + PGlite)
│   └── reference-screenshots/ # Curated app screenshots for visual design grounding
├── pyproject.toml
├── Dockerfile
└── launch.sh
```

## Configuration

The pipeline is launched via a JSON configuration file:

```json
{
  "world": {
    "package": "plato-world-skill-test-generator:0.4.72",
    "runtime": {
      "type": "vm",
      "vm": { "cpus": 2, "memory": 4096, "disk": 20480, "timeout": 43200 }
    },
    "config": {
      "s3_skills": [
        "Resolve truncated text in data grid cells before using value",
        "Exhaustively paginate list before identifying extremum record"
      ],
      "specs_per_skill": 6,
      "sessions_per_testcase": 4,
      "vm_concurrency": 20,
      "autoverify": true,
      "hillclimb": {
        "enabled": true,
        "total_failures": 1,
        "max_retries": 3
      }
    }
  }
}
```

This would generate 12 independent web applications (2 skills × 6 variants each), run 4 evaluation sessions per testcase, and iteratively tune difficulty until at least 1 in 4 agent runs fails on every testcase.

## Development History

This pipeline was built iteratively over three weeks. Each stage was developed, deployed, and tested against live infrastructure — bugs surfaced through real production runs, not synthetic test suites.

**Week 1 — Core pipeline (Apr 8–13)**: Initial pipeline with INGEST → DESIGN → CODEGEN → RUN → EVALUATE. Early runs exposed infrastructure issues: PGlite data directory creation failures on snapshot restore, Bun incompatibilities with Next.js production builds (switched to Node), hardcoded `localhost` URLs breaking under the platform's reverse proxy, systemd service required for apps to survive snapshot restore, and evaluation agent credential forwarding. Each issue was caught from a failed production run and fixed in the next deploy.

**Week 2 — Solvability verification and hints (Apr 14–20)**: Added reference screenshot pipeline for visual design grounding. Introduced agent hints — navigation strategies passed to verification agents to confirm task solvability without revealing answers. Built the solvability verification gate: initial implementation called a server-side endpoint, but this was replaced with client-side multi-session consensus after discovering the endpoint didn't support the scoring format needed. Iterated through 10+ fixes in a single day to get verification working reliably: parallelizing sessions, fixing URL construction, removing stale variables, switching agent models, and tuning agreement thresholds.

**Week 3 — Hillclimb and hardening (Apr 20–30)**: Added the hillclimb difficulty-tuning loop. Key iterations: ensuring only the target testcase is published per iteration (not all testcases), archiving superseded testcases, propagating hints through the hillclimb workspace, falling back to spec-only task generation when live API data is unavailable. Added retry logic for transient HTTP 502/503/504 errors. Fixed testcase name collisions where the LLM would generate duplicate names across different environments — added `-v2`/`-v3` suffix deduplication. Fixed duplicate skill slug prefixes in testcase names.

**Production results**: The pipeline has generated 500+ testcases across 130+ environments. Hillclimb has successfully reduced pass rates from 100% to under 75% on dozens of testcases, with some dropping to 0% — the agent cannot solve them at all after the environment modifications.

## Dependencies

- **[Plato SDK](https://plato.so)** — VM orchestration, environment snapshotting, session management, benchmark execution
- **Anthropic SDK** — LLM calls for design, code generation, testcase generation, and solvability verification
- **boto3** — S3 access for skill data and environment source upload
- **httpx** — Async HTTP for platform API calls
- **Pydantic** — Configuration validation and state management
