"""Miner-side adapter to an operator-configured generation process."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess

from .protocol import BuildContribution, BuildTask


class GeneratorError(RuntimeError):
    pass


def generate(task: BuildTask, miner_hotkey: str | None = None) -> BuildContribution:
    """Run this miner operator's generator; never falls back to centralized generation."""
    command = os.getenv("FACTORY_GENERATOR_COMMAND", "").strip()
    if not command:
        raise GeneratorError("FACTORY_GENERATOR_COMMAND is not configured on this miner.")
    allowed_names = os.getenv("FACTORY_GENERATOR_ENV", "OLLAMA_BASE_URL,OLLAMA_MODEL").split(",")
    child_env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}
    for name in allowed_names:
        name = name.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name):
            continue
        if name in os.environ:
            child_env[name] = os.environ[name]
    try:
        result = subprocess.run(
            shlex.split(command),
            input=task.model_dump_json(),
            text=True,
            capture_output=True,
            timeout=task.deadline_seconds,
            check=False,
            env=child_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GeneratorError(f"Configured generator failed: {exc}") from exc
    if result.returncode:
        raise GeneratorError(f"Configured generator exited with status {result.returncode}.")
    try:
        payload = json.loads(result.stdout)
        contribution = BuildContribution.model_validate(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        raise GeneratorError(f"Generator returned an invalid contribution: {exc}") from exc
    if contribution.task_id != task.task_id:
        raise GeneratorError("Generator returned a contribution for a different task.")
    contribution.miner_hotkey = miner_hotkey
    contribution.generator = "operator-configured-command"
    return contribution
