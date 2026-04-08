"""Local runner for the skill-test-generator world."""

import asyncio
import configparser
import json
import logging
import os
import re
import sys
from pathlib import Path

for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ.pop(key, None)

_aws_cfg = configparser.ConfigParser()
_aws_cfg.read(os.path.expanduser("~/.aws/credentials"))
_AWS_AK = _aws_cfg.get("default", "aws_access_key_id", fallback="")
_AWS_SK = _aws_cfg.get("default", "aws_secret_access_key", fallback="")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("run_local")


def resolve_env_vars(data):
    """Recursively resolve ${VAR} references in config values."""
    if isinstance(data, str):

        def _replace(m):
            return os.environ.get(m.group(1), "")

        return re.sub(r"\$\{(\w+)\}", _replace, data)
    if isinstance(data, dict):
        return {k: resolve_env_vars(v) for k, v in data.items()}
    if isinstance(data, list):
        return [resolve_env_vars(v) for v in data]
    return data


async def main():
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("run-config.json")
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)

    with open(config_path) as f:
        raw_config = json.load(f)

    from skill_test_generator.config import SkillTestGeneratorConfig

    merged = SkillTestGeneratorConfig.model_fields
    for field_name, field_info in merged.items():
        default = field_info.default
        if isinstance(default, str) and "${" in default:
            var = default.strip("${}")
            env_val = os.environ.get(var, "")
            if env_val:
                raw_config.setdefault(field_name, env_val)

    raw_config = resolve_env_vars(raw_config)

    code_dir = Path("/tmp/skill-test-gen/code")
    output_dir = Path("/tmp/skill-test-gen/output")
    code_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_config["code"] = str(code_dir)
    raw_config["output"] = str(output_dir)

    if _AWS_AK and "aws_access_key_id" not in raw_config:
        raw_config["aws_access_key_id"] = _AWS_AK
    if _AWS_SK and "aws_secret_access_key" not in raw_config:
        raw_config["aws_secret_access_key"] = _AWS_SK

    run_config = SkillTestGeneratorConfig.model_validate(raw_config)

    logger.info(
        "Config loaded: %d skills, local_preview_base_port=%s",
        len(run_config.skills),
        run_config.local_preview_base_port,
    )
    logger.info(
        "eval_agent=%s (RUN/EVALUATE will %s)",
        run_config.eval_agent,
        "run" if run_config.eval_agent else "be skipped",
    )

    from skill_test_generator import SkillTestGeneratorWorld
    from plato.worlds.config import SessionConfig

    world = SkillTestGeneratorWorld()
    world.config = run_config
    world.session = SessionConfig()

    logger.info("=" * 60)
    logger.info("RESETTING WORLD")
    logger.info("=" * 60)
    obs = await world.reset()
    logger.info("Reset result: %s", obs.text if obs else "OK")

    logger.info("=" * 60)
    logger.info("RUNNING PIPELINE")
    logger.info("=" * 60)
    result = await world.step()

    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE")
    logger.info("=" * 60)
    logger.info(
        "Result: %s",
        result.observation.text[:2000] if result.observation else "No observation",
    )

    if run_config.local_preview_base_port:
        logger.info("Preview servers should be running. Press Ctrl+C to stop.")
        try:
            while True:
                await asyncio.sleep(60)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    asyncio.run(main())
