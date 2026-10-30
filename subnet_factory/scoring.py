"""Reproducible baseline validator checks for Factory contributions."""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

from .protocol import BuildContribution, BuildTask, TaskKind, ValidationReport

SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|secret|password)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    re.compile(r"(?i)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
SUSPICIOUS_CODE = (
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    re.compile(r"subprocess\.(?:run|Popen|call)\s*\("),
)


def _safe_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _check_python_syntax(path: str, content: str) -> bool:
    if not path.endswith(".py"):
        return True
    try:
        ast.parse(content, filename=path)
        return True
    except SyntaxError:
        return False


def validate_contribution(task: BuildTask, contribution: BuildContribution) -> ValidationReport:
    findings: list[str] = []
    checks = {
        "task_matches": contribution.task_id == task.task_id,
        "has_output": bool(
            contribution.files or contribution.suggestions or contribution.architecture
        ),
        "safe_paths": all(_safe_path(item.path) for item in contribution.files),
        "unique_paths": len({item.path for item in contribution.files}) == len(contribution.files),
        "python_syntax": all(_check_python_syntax(f.path, f.content) for f in contribution.files),
        "required_paths": set(task.required_paths).issubset({f.path for f in contribution.files}),
        "no_embedded_secret_patterns": not any(
            pattern.search(file.content)
            for file in contribution.files
            for pattern in SECRET_PATTERNS
        ),
    }
    if task.kind == TaskKind.DISCOVER:
        names = [item.name.casefold() for item in contribution.suggestions]
        commodities = [item.commodity.casefold() for item in contribution.suggestions]
        checks["has_discovery_suggestions"] = len(contribution.suggestions) >= 3
        checks["discovery_suggestions_are_distinct"] = len(set(names)) == len(names) and len(
            set(commodities)
        ) == len(commodities)
    elif task.kind == TaskKind.ARCHITECTURE:
        checks["has_architecture"] = bool(contribution.architecture)
    elif task.kind == TaskKind.GENERATE:
        checks["has_requested_files"] = bool(contribution.files)

    for file in contribution.files:
        if file.path.endswith(".py") and any(p.search(file.content) for p in SUSPICIOUS_CODE):
            findings.append(f"Review risky dynamic execution or subprocess use in {file.path}.")
    for key, passed in checks.items():
        if not passed:
            findings.append(f"Failed check: {key}.")

    # Do not reward longer output. The initial score is a transparent count of passed checks.
    score = sum(checks.values()) / max(len(checks), 1)
    accepted = all(checks.values()) and not findings
    return ValidationReport(
        task_id=task.task_id,
        contribution_id=contribution.contribution_id,
        accepted=accepted,
        score=score,
        checks=checks,
        findings=findings,
    )


def aggregate_accepted_files(
    contributions: list[BuildContribution], reports: list[ValidationReport]
) -> dict[str, str]:
    """Aggregate unanimous validator-approved files; conflicting content is rejected."""
    accepted_ids = {(r.task_id, r.contribution_id) for r in reports if r.accepted}
    paths: dict[str, str] = {}
    for contribution in contributions:
        if (contribution.task_id, contribution.contribution_id) not in accepted_ids:
            continue
        for file in contribution.files:
            if not _safe_path(file.path):
                raise ValueError(f"Unsafe project path: {file.path}")
            existing = paths.get(file.path)
            if existing is not None and existing != file.content:
                raise ValueError(f"Conflicting accepted contributions for {file.path}")
            paths[file.path] = file.content
    return paths
