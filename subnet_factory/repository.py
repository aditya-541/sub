"""Safe, durable assembly of accepted Factory artifacts into project folders."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .protocol import AggregateRequest
from .scoring import aggregate_accepted_files

PROJECT_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{2,79}$")
MAX_PROJECT_BYTES = 20 * 1024 * 1024


class RepositoryAssemblyError(RuntimeError):
    pass


def assemble_project(project_id: str, request: AggregateRequest, root: Path) -> dict[str, object]:
    if not PROJECT_ID.fullmatch(project_id):
        raise RepositoryAssemblyError(
            "Project ID must be 3–80 safe letters, digits, hyphens, or underscores."
        )
    try:
        files = aggregate_accepted_files(request.contributions, request.reports)
    except ValueError as exc:
        raise RepositoryAssemblyError(str(exc)) from exc
    if not files:
        raise RepositoryAssemblyError("No validator-approved files are available to assemble.")
    if "factory-manifest.json" in files:
        raise RepositoryAssemblyError(
            "factory-manifest.json is reserved for Factory build metadata."
        )
    total_bytes = sum(len(content.encode("utf-8")) for content in files.values())
    if total_bytes > MAX_PROJECT_BYTES:
        raise RepositoryAssemblyError("Accepted artifacts exceed the 20 MiB project limit.")

    root.mkdir(parents=True, exist_ok=True)
    safe_root = root.resolve(strict=True)
    project_root = safe_root / project_id
    if project_root.exists() or project_root.is_symlink():
        raise RepositoryAssemblyError("Project directory already exists; choose a new project ID.")
    project_root.mkdir(mode=0o750)

    manifest: dict[str, object] = {
        "project_id": project_id,
        "task_ids": sorted({item.task_id for item in request.contributions}),
        "accepted_contribution_ids": sorted(
            report.contribution_id for report in request.reports if report.accepted
        ),
        "files": {
            path: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for path, content in sorted(files.items())
        },
    }
    output_paths: list[str] = []
    try:
        for relative, content in sorted(files.items()):
            destination = project_root / relative
            parent = destination.parent
            parent.mkdir(parents=True, exist_ok=True)
            if not parent.resolve(strict=True).is_relative_to(project_root.resolve(strict=True)):
                raise RepositoryAssemblyError("Artifact path escapes its project directory.")
            if destination.exists() or destination.is_symlink():
                raise RepositoryAssemblyError(f"Artifact already exists: {relative}")
            descriptor, temporary = tempfile.mkstemp(prefix=".factory-", dir=parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            output_paths.append(relative)

        manifest_path = project_root / "factory-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return {
            "project_id": project_id,
            "path": str(project_root),
            "file_count": len(output_paths),
            "total_bytes": total_bytes,
            "files": output_paths,
            "manifest": manifest,
            "status": "assembled-locally",
        }
    except Exception:
        # Incomplete repositories must never be presented as successful artifacts.
        for current in sorted(project_root.rglob("*"), reverse=True):
            if current.is_file() or current.is_symlink():
                current.unlink(missing_ok=True)
            elif current.is_dir():
                current.rmdir()
        project_root.rmdir()
        raise
