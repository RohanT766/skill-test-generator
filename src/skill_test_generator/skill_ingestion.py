"""Load and deduplicate skill categories from benchmark-review S3 data or config."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

import boto3

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

from .config import SkillDefinition

logger = logging.getLogger(__name__)

S3_BUCKET = "plato-browser-session-data-prod"


def _slugify(name: str) -> str:
    """Convert a skill name to a URL-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "-", slug)
    return slug[:60].rstrip("-")


def _s3_client_kwargs(
    aws_access_key_id: str,
    aws_secret_access_key: str,
    aws_session_token: str = "",
) -> dict:
    kwargs: dict = {}
    if aws_access_key_id and aws_secret_access_key:
        kwargs["aws_access_key_id"] = aws_access_key_id
        kwargs["aws_secret_access_key"] = aws_secret_access_key
        if aws_session_token:
            kwargs["aws_session_token"] = aws_session_token
    return kwargs


def load_skills_from_s3(
    sims: list[str],
    aws_access_key_id: str,
    aws_secret_access_key: str,
    aws_session_token: str = "",
    min_sessions: int = 2,
) -> list[SkillDefinition]:
    """Load skill_categories across sims from benchmark-review S3 pipeline data.

    Deduplicates skills by name, merges session_ids and sim_sources,
    and filters by testable + min_sessions.
    """
    client = boto3.client(
        "s3",
        **_s3_client_kwargs(aws_access_key_id, aws_secret_access_key, aws_session_token),
    )

    merged: dict[str, SkillDefinition] = {}

    for sim in sims:
        key = f"benchmark-analysis/{sim}.json"
        try:
            raw = client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
            pipeline = json.loads(raw)
        except Exception:
            logger.warning("Could not load benchmark data for sim '%s'", sim)
            continue

        skill_cats = pipeline.get("skill_categories", {})
        for _model, categories in skill_cats.items():
            if not isinstance(categories, list):
                continue
            for cat in categories:
                name = cat.get("name", "").strip()
                if not name:
                    continue
                testable = cat.get("testable", True)
                sids = cat.get("session_ids", [])

                if name in merged:
                    existing = merged[name]
                    existing.session_ids = list(
                        dict.fromkeys(existing.session_ids + sids)
                    )
                    if sim not in existing.sim_sources:
                        existing.sim_sources.append(sim)
                    if not existing.short_name and cat.get("short_name"):
                        existing.short_name = cat["short_name"]
                else:
                    merged[name] = SkillDefinition(
                        name=name,
                        short_name=cat.get("short_name"),
                        description=cat.get("description", ""),
                        testable=testable,
                        session_ids=sids,
                        sim_sources=[sim],
                    )

    skills = [
        s for s in merged.values() if s.testable and len(s.session_ids) >= min_sessions
    ]
    skills.sort(key=lambda s: -len(s.session_ids))

    logger.info(
        "Loaded %d testable skills (%d total, %d filtered) from %d sim(s)",
        len(skills),
        len(merged),
        len(merged) - len(skills),
        len(sims),
    )
    return skills


def _load_all_s3_skills(
    aws_access_key_id: str,
    aws_secret_access_key: str,
    aws_session_token: str = "",
) -> dict[str, SkillDefinition]:
    """Load all skills from all benchmark-analysis files in S3."""
    client = boto3.client(
        "s3",
        **_s3_client_kwargs(aws_access_key_id, aws_secret_access_key, aws_session_token),
    )

    merged: dict[str, SkillDefinition] = {}
    prefix = "benchmark-analysis/"

    try:
        resp = client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
        keys = [
            obj["Key"]
            for obj in resp.get("Contents", [])
            if obj["Key"].endswith(".json")
        ]
    except Exception as exc:
        logger.warning(
            "Could not list benchmark-analysis files in S3: %s: %s",
            type(exc).__name__,
            exc,
        )
        return merged

    for key in keys:
        sim = key.removeprefix(prefix).removesuffix(".json")
        try:
            raw = client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
            pipeline = json.loads(raw)
        except Exception:
            continue

        skill_cats = pipeline.get("skill_categories", {})
        for _model, categories in skill_cats.items():
            if not isinstance(categories, list):
                continue
            for cat in categories:
                name = cat.get("name", "").strip()
                if not name:
                    continue
                sids = cat.get("session_ids", [])

                if name in merged:
                    existing = merged[name]
                    existing.session_ids = list(
                        dict.fromkeys(existing.session_ids + sids)
                    )
                    if sim not in existing.sim_sources:
                        existing.sim_sources.append(sim)
                    if not existing.short_name and cat.get("short_name"):
                        existing.short_name = cat["short_name"]
                else:
                    merged[name] = SkillDefinition(
                        name=name,
                        short_name=cat.get("short_name"),
                        description=cat.get("description", ""),
                        testable=cat.get("testable", True),
                        session_ids=sids,
                        sim_sources=[sim],
                    )

    return merged


def _resolve_s3_skill_names(
    names: list[str],
    aws_access_key_id: str,
    aws_secret_access_key: str,
    aws_session_token: str = "",
) -> list[SkillDefinition]:
    """Look up skill names in S3 benchmark data. Drops names not found."""
    if not names:
        return []

    logger.info("Looking up %d skill(s) by name in S3 benchmark data", len(names))
    all_s3 = _load_all_s3_skills(
        aws_access_key_id, aws_secret_access_key, aws_session_token
    )
    lookup_lower = {k.lower(): v for k, v in all_s3.items()}

    resolved = []
    for name in names:
        match = all_s3.get(name) or lookup_lower.get(name.lower())
        if match:
            logger.info(
                "  Found '%s' in S3 (%d sessions, sims: %s)",
                name,
                len(match.session_ids),
                match.sim_sources,
            )
            resolved.append(match)
        else:
            logger.warning("  Skill '%s' not found in S3 benchmark data, skipping", name)

    return resolved


def prepare_skills(
    s3_skill_names: list[str] | None,
    custom_skills: list[SkillDefinition] | None,
    aws_access_key_id: str,
    aws_secret_access_key: str,
    aws_session_token: str = "",
    max_skills: int = 20,
) -> list[SkillDefinition]:
    """Resolve skills from S3 names and/or custom definitions."""
    skills: list[SkillDefinition] = []

    if s3_skill_names:
        s3_resolved = _resolve_s3_skill_names(
            s3_skill_names,
            aws_access_key_id,
            aws_secret_access_key,
            aws_session_token,
        )
        skills.extend(s for s in s3_resolved if s.testable)
        logger.info("Resolved %d S3 skills (of %d names)", len(skills), len(s3_skill_names))

    if custom_skills:
        custom_valid = [s for s in custom_skills if s.name and s.description]
        skipped = len(custom_skills) - len(custom_valid)
        if skipped:
            logger.warning(
                "Skipped %d custom skill(s) missing name or description", skipped
            )
        skills.extend(custom_valid)
        logger.info("Added %d custom skills", len(custom_valid))

    if not skills:
        logger.warning("No skills resolved. Nothing to generate.")
        return []

    if len(skills) > max_skills:
        logger.info("Limiting to top %d skills (of %d)", max_skills, len(skills))
        skills = skills[:max_skills]

    return skills


async def generate_short_names(
    skills: list[SkillDefinition],
    anthropic_api_key: str,
) -> None:
    """Use Anthropic to generate 2-3 word abbreviated names for skills missing short_name."""
    missing = [s for s in skills if not s.short_name]
    if not missing:
        logger.info("All skills already have short_name, skipping generation")
        return

    logger.info("Generating short_name for %d skill(s) via Anthropic", len(missing))

    import anthropic

    numbered = "\n".join(f'{i+1}. "{s.name}"' for i, s in enumerate(missing))
    prompt = (
        "For each skill name below, produce a concise 2-3 word abbreviated name "
        "(lowercase, hyphenated, no quotes). It should capture the core action/concept.\n\n"
        f"{numbered}\n\n"
        'Respond with ONLY a JSON array: [{"name": "...", "short_name": "..."}]'
    )

    client = anthropic.Anthropic(api_key=anthropic_api_key)
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = resp.content[0].text.strip()
    json_match = re.search(r"\[.*\]", raw_text, re.DOTALL)
    if not json_match:
        logger.error("Could not parse short_name response from Anthropic: %s", raw_text)
        return

    results: list[dict] = json.loads(json_match.group())
    lookup = {r["name"]: r["short_name"] for r in results if r.get("short_name")}

    for s in missing:
        sn = lookup.get(s.name)
        if sn:
            s.short_name = sn
            logger.info("  %s -> %s", s.name, sn)
        else:
            logger.warning("  No short_name generated for '%s'", s.name)


def persist_short_names_to_s3(
    skills: list[SkillDefinition],
    aws_access_key_id: str,
    aws_secret_access_key: str,
    aws_session_token: str = "",
) -> None:
    """Write short_name back to the S3 benchmark-analysis JSON files."""
    to_persist = [s for s in skills if s.short_name]
    if not to_persist:
        return

    name_to_short: dict[str, str] = {s.name: s.short_name for s in to_persist}
    all_sims: set[str] = set()
    for s in to_persist:
        all_sims.update(s.sim_sources)

    if not all_sims:
        logger.warning("No sim_sources to update in S3")
        return

    client = boto3.client(
        "s3",
        **_s3_client_kwargs(aws_access_key_id, aws_secret_access_key, aws_session_token),
    )

    updated_files = 0
    for sim in all_sims:
        key = f"benchmark-analysis/{sim}.json"
        try:
            raw = client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
            data = json.loads(raw)
        except Exception:
            logger.warning("Could not load S3 file for sim '%s', skipping", sim)
            continue

        modified = False
        skill_cats = data.get("skill_categories", {})
        for _model, categories in skill_cats.items():
            if not isinstance(categories, list):
                continue
            for cat in categories:
                cat_name = cat.get("name", "").strip()
                if cat_name in name_to_short and cat.get("short_name") != name_to_short[cat_name]:
                    cat["short_name"] = name_to_short[cat_name]
                    modified = True

        if modified:
            client.put_object(
                Bucket=S3_BUCKET,
                Key=key,
                Body=json.dumps(data, indent=2).encode(),
                ContentType="application/json",
            )
            updated_files += 1
            logger.info("Updated short_name in S3: %s", key)

    logger.info("Persisted short_name to %d S3 file(s)", updated_files)
