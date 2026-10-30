"""Local development API for the Factory task protocol and validator checks."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.requests import Request

from .dispatch import dispatch_task
from .generator import GeneratorError, generate
from .network import BittensorNetwork, NetworkConfigurationError, verify_peer_request
from .protocol import (
    AggregateRequest,
    BuildContribution,
    BuildTask,
    DispatchResult,
    TaskKind,
    ValidationReport,
    WeightPlan,
)
from .repository import RepositoryAssemblyError, assemble_project
from .scoring import aggregate_accepted_files, validate_contribution
from .store import BuildQueueFullError, TaskStore
from .weights import build_weight_plan

app = FastAPI(title="Subnet Factory Miner/Validator API", version="0.2.0")


@app.middleware("http")
async def cors_for_tensor_studio(request: Request, call_next):
    """Allow explicitly configured browser apps to call the bearer-token API."""
    allowed = {
        origin.strip()
        for origin in os.getenv("FACTORY_CORS_ORIGINS", "").split(",")
        if origin.strip()
    }
    origin = request.headers.get("origin")
    if request.method == "OPTIONS" and origin in allowed:
        from starlette.responses import Response

        response = Response(status_code=204)
    else:
        response = await call_next(request)
    if origin in allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    return response


class PromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=12, max_length=12_000)
    ]


class BuildRequest(PromptRequest):
    model_config = ConfigDict(extra="forbid")
    project_id: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=3,
            max_length=80,
            pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{2,79}$",
        ),
    ]
    specification: dict[str, object] = Field(default_factory=dict)
    required_paths: list[str] = Field(default_factory=list, max_length=100)
    sdk_target: str = "bittensor-v11"


def _authorize_client(request: Request) -> None:
    if os.getenv("FACTORY_LOCAL_MODE") == "1":
        return
    expected = os.getenv("FACTORY_API_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=503, detail="Factory client authentication is not configured."
        )
    supplied = request.headers.get("authorization", "")
    if not supplied.startswith("Bearer ") or not hmac.compare_digest(supplied[7:], expected):
        raise HTTPException(status_code=401, detail="Invalid Factory client credentials.")


@app.get("/health")
def health() -> dict[str, str]:
    local = os.getenv("FACTORY_LOCAL_MODE") == "1"
    return {
        "status": "ok",
        "mode": "local-development" if local else "chain",
        "chain": "not-connected" if local else "not-verified",
    }


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def homepage() -> str:
    """Give visitors a clear entry point instead of FastAPI's default 404."""
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#10151d">
    <title>Subnet Factory API</title>
    <style>
      :root { color-scheme: dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #10151d; color: #edf2f7; }
      body { margin: 0; min-height: 100vh; display: grid; place-items: center; }
      main { width: min(640px, calc(100% - 40px)); padding: 36px; border: 1px solid #293342; border-radius: 14px; background: #171f2a; box-sizing: border-box; }
      h1 { margin: 0 0 12px; font-size: 28px; letter-spacing: -.03em; }
      p { color: #b2bfce; line-height: 1.6; }
      nav { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 24px; }
      a { color: #dceaff; text-decoration: none; border: 1px solid #3b4a5e; border-radius: 8px; padding: 10px 14px; }
      a:hover { background: #223044; }
      small { color: #8796a8; }
    </style>
  </head>
  <body>
    <main>
      <h1>Subnet Factory API</h1>
      <p>This service coordinates subnet architecture, miner contributions, validation, and project builds for connected applications.</p>
      <p>It is an API service, not the Tensor Studio web interface. Factory client endpoints require a server-side bearer token.</p>
      <nav>
        <a href="/docs">API documentation</a>
        <a href="/health">Service health</a>
      </nav>
      <p><small>Subnet Factory · Bittensor SDK 11</small></p>
    </main>
  </body>
</html>"""


async def _authenticate_miner_request(request: Request, body: bytes) -> str | None:
    if os.getenv("FACTORY_LOCAL_MODE") == "1":
        return "local-development"
    try:
        chain = BittensorNetwork()
        receiver = chain.hotkey_ss58
        raw_target = request.scope["raw_path"].decode()
        if request.scope["query_string"]:
            raw_target += "?" + request.scope["query_string"].decode()
        caller = verify_peer_request(
            request.headers,
            body,
            method=request.method,
            path=raw_target,
            self_hotkey_ss58=receiver,
        )
    except NetworkConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Bittensor signed request.") from exc
    try:
        eligible = await asyncio.to_thread(chain.eligible_validator, caller)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Could not verify validator eligibility."
        ) from exc
    if not eligible:
        raise HTTPException(status_code=403, detail="Caller is not an eligible Factory validator.")
    return caller


@app.post("/tasks/execute", response_model=BuildContribution)
async def execute_task(request: Request) -> BuildContribution:
    raw_body = await request.body()
    await _authenticate_miner_request(request, raw_body)
    try:
        task = BuildTask.model_validate_json(raw_body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid Factory task payload.") from exc
    try:
        return await asyncio.to_thread(generate, task)
    except GeneratorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/validate", response_model=ValidationReport)
def validate(
    task: BuildTask, contribution: BuildContribution, request: Request
) -> ValidationReport:
    _authorize_client(request)
    return validate_contribution(task, contribution)


@app.post("/aggregate")
def aggregate(payload: AggregateRequest, request: Request) -> dict[str, object]:
    _authorize_client(request)
    try:
        files = aggregate_accepted_files(payload.contributions, payload.reports)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"files": files, "file_count": len(files), "status": "locally-aggregated"}


@app.post("/projects/{project_id}/assemble")
def assemble(project_id: str, payload: AggregateRequest, request: Request) -> dict[str, object]:
    _authorize_client(request)
    root = Path(os.getenv("FACTORY_ARTIFACT_ROOT", "./artifacts"))
    try:
        return assemble_project(project_id, payload, root)
    except RepositoryAssemblyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/projects/{project_id}/files")
def get_project_files(project_id: str, request: Request) -> dict[str, object]:
    """Return only manifest-listed artifact files for the browser workspace."""
    _authorize_client(request)
    root = Path(os.getenv("FACTORY_ARTIFACT_ROOT", "./artifacts"))
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{2,79}", project_id):
        raise HTTPException(status_code=404, detail="Factory project not found.")
    project_root = root / project_id
    manifest_path = project_root / "factory-manifest.json"
    try:
        resolved_root = root.resolve(strict=True)
        resolved_project = project_root.resolve(strict=True)
        if not resolved_project.is_relative_to(resolved_root):
            raise ValueError("Unsafe project directory")
        if not manifest_path.resolve(strict=True).is_relative_to(resolved_project):
            raise ValueError("Unsafe project manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        file_hashes = manifest["files"]
        if not isinstance(file_hashes, dict):
            raise TypeError("Invalid manifest files field")
        files = {}
        for relative in file_hashes:
            if not isinstance(relative, str) or ".." in Path(relative).parts:
                raise ValueError("Unsafe manifest path")
            candidate = project_root / relative
            if not candidate.resolve(strict=True).is_relative_to(resolved_root):
                raise ValueError("Unsafe manifest path")
            files[relative] = candidate.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Factory project not found.") from exc
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise HTTPException(status_code=500, detail="Factory project manifest is invalid.") from exc
    return {"project_id": project_id, "manifest": manifest, "files": files}


@app.post("/weights/plan", response_model=WeightPlan)
async def plan_weights(payload: AggregateRequest, request: Request) -> WeightPlan:
    _authorize_client(request)
    if os.getenv("FACTORY_LOCAL_MODE") == "1":
        raise HTTPException(status_code=409, detail="Weight plans require a chain metagraph.")
    try:
        chain = BittensorNetwork()
        if not await asyncio.to_thread(chain.eligible_validator, chain.hotkey_ss58):
            raise HTTPException(
                status_code=403, detail="This hotkey has no Factory validator permit."
            )
        metagraph = await asyncio.to_thread(chain.metagraph)
        return build_weight_plan(
            chain.netuid,
            payload.contributions,
            payload.reports,
            metagraph.neurons,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not build weight plan: {exc}") from exc


@app.post("/tasks/architecture", response_model=BuildTask)
def architecture_task(payload: PromptRequest, request: Request) -> BuildTask:
    """Create a deterministic task envelope; miners do architecture and suggestion work."""
    _authorize_client(request)
    task = BuildTask(
        task_id=f"architecture-{uuid4().hex}",
        kind=TaskKind.ARCHITECTURE,
        prompt=payload.prompt,
        specification={"requested_outputs": ["subnet_suggestions", "architecture"]},
    )
    TaskStore().save(task, status="queued")
    return task


def _discovery_task(request: PromptRequest) -> BuildTask:
    return BuildTask(
        task_id=f"discovery-{uuid4().hex}",
        kind=TaskKind.DISCOVER,
        prompt=request.prompt,
        specification={
            "requested_outputs": ["multiple_distinct_subnet_ideas"],
            "minimum_suggestions": 3,
            "must_include": ["commodity", "users", "verification", "risks"],
        },
    )


@app.post("/tasks/dispatch", response_model=DispatchResult)
async def run_task(task: BuildTask, request: Request) -> DispatchResult:
    _authorize_client(request)
    store = TaskStore()
    store.save(task, status="running")
    try:
        chain = BittensorNetwork()
        if os.getenv("FACTORY_LOCAL_MODE") != "1" and not await asyncio.to_thread(
            chain.eligible_validator, chain.hotkey_ss58
        ):
            raise HTTPException(
                status_code=403,
                detail=f"This hotkey has no Factory validator permit. Task ID: {task.task_id}",
            )
        peers = await asyncio.to_thread(chain.peers)
        contributions, reports = await dispatch_task(task, network=chain)
    except HTTPException as exc:
        store.save(task, status="failed", error=str(exc.detail))
        raise
    except NetworkConfigurationError as exc:
        store.save(task, status="failed", error=str(exc))
        raise HTTPException(status_code=503, detail=f"{exc} Task ID: {task.task_id}") from exc
    except Exception as exc:
        store.save(task, status="failed", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail=f"Factory miner dispatch failed: {exc}. Task ID: {task.task_id}",
        ) from exc
    accepted = {(report.task_id, report.contribution_id) for report in reports if report.accepted}
    suggestions = [
        suggestion
        for contribution in contributions
        if (contribution.task_id, contribution.contribution_id) in accepted
        for suggestion in contribution.suggestions
    ]
    result = DispatchResult(
        task_id=task.task_id,
        attempted_peers=len(peers),
        contribution_count=len(contributions),
        accepted_count=len(accepted),
        suggestions=suggestions,
        contributions=contributions,
        reports=reports,
    )
    final_status = (
        "completed"
        if result.accepted_count
        else "partial"
        if result.contribution_count
        else "failed"
    )
    store.save(task, status=final_status, outcome=result.model_dump(mode="json"))
    return result


async def _execute_project_build(task: BuildTask, project_id: str) -> None:
    store = TaskStore()
    await asyncio.to_thread(store.save, task, status="running")
    await asyncio.to_thread(
        store.record_event, task.task_id, "phase", {"phase": "checking-validator"}
    )
    try:
        chain = BittensorNetwork()
        if os.getenv("FACTORY_LOCAL_MODE") != "1" and not await asyncio.to_thread(
            chain.eligible_validator, chain.hotkey_ss58
        ):
            raise PermissionError("Factory validator permit is missing for this hotkey.")
        peers = await asyncio.to_thread(chain.peers)
        await asyncio.to_thread(
            store.record_event,
            task.task_id,
            "phase",
            {"phase": "dispatching-to-miners", "miner_count": len(peers)},
        )

        async def on_peer_event(peer, status):
            await asyncio.to_thread(
                store.record_event,
                task.task_id,
                "miner",
                {"hotkey": peer.hotkey, "status": status},
            )

        contributions, reports = await dispatch_task(
            task, network=chain, on_peer_event=on_peer_event
        )
        await asyncio.to_thread(
            store.record_event,
            task.task_id,
            "phase",
            {"phase": "validating-contributions", "contribution_count": len(contributions)},
        )
        accepted_count = sum(report.accepted for report in reports)
        assembled = None
        if accepted_count:
            await asyncio.to_thread(
                store.record_event, task.task_id, "phase", {"phase": "assembling-project"}
            )
            aggregate = AggregateRequest(contributions=contributions, reports=reports)
            root = Path(os.getenv("FACTORY_ARTIFACT_ROOT", "./artifacts"))
            assembled = await asyncio.to_thread(assemble_project, project_id, aggregate, root)
        result_status = "assembled" if assembled else "no-accepted-contributions"
        outcome = {
            "status": result_status,
            "attempted_peers": len(peers),
            "contribution_count": len(contributions),
            "accepted_count": accepted_count,
            "contributions": [item.model_dump(mode="json") for item in contributions],
            "reports": [item.model_dump(mode="json") for item in reports],
            "project": assembled,
        }
        await asyncio.to_thread(
            store.save, task, status="completed" if assembled else "partial", outcome=outcome
        )
        await asyncio.to_thread(
            store.record_event,
            task.task_id,
            "complete",
            {"status": result_status, "accepted_count": accepted_count, "project_id": project_id},
        )
    except Exception as exc:  # noqa: BLE001 - persist failures from external chain/miner calls
        message = str(exc)[:1000]
        await asyncio.to_thread(store.save, task, status="failed", error=message)
        await asyncio.to_thread(store.record_event, task.task_id, "failed", {"message": message})
    finally:
        await asyncio.to_thread(store.release_build_job, task.task_id)


async def process_next_build_job() -> bool:
    """Claim and execute one persistent build job; shared by API tests and the worker process."""
    store = TaskStore()
    job = await asyncio.to_thread(store.claim_build_job)
    if job is None:
        return False
    await _execute_project_build(job["task"], job["project_id"])
    return True


@app.post("/projects/build", status_code=202)
async def build_project(
    payload: BuildRequest,
    request: Request,
) -> dict[str, str]:
    """Queue generation and return immediately; stream durable phase/miner events to clients."""
    _authorize_client(request)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{2,79}", payload.project_id):
        raise HTTPException(status_code=422, detail="Invalid project ID.")
    task = BuildTask(
        task_id=f"generate-{uuid4().hex}",
        kind=TaskKind.GENERATE,
        prompt=payload.prompt,
        specification=payload.specification,
        required_paths=payload.required_paths,
        sdk_target=payload.sdk_target,
    )
    try:
        await asyncio.to_thread(TaskStore().enqueue_build, task, payload.project_id)
    except BuildQueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {
        "task_id": task.task_id,
        "status": "queued",
        "task_url": f"/tasks/{task.task_id}",
        "events_url": f"/tasks/{task.task_id}/events",
    }


@app.post("/suggestions", response_model=DispatchResult)
async def suggestion_task(payload: PromptRequest, request: Request) -> DispatchResult:
    return await run_task(_discovery_task(payload), request)


@app.get("/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request, after: int = 0) -> StreamingResponse:
    """Replay and stream persisted build progress as Server-Sent Events."""
    _authorize_client(request)
    store = TaskStore()
    if await asyncio.to_thread(store.get, task_id) is None:
        raise HTTPException(status_code=404, detail="Factory task not found.")

    async def event_stream():
        cursor = max(after, 0)
        while not await request.is_disconnected():
            events = await asyncio.to_thread(store.events_after, task_id, cursor)
            for event in events:
                cursor = event["event_id"]
                yield (
                    f"id: {cursor}\nevent: {event['event_type']}\n"
                    f"data: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
                )
            record = await asyncio.to_thread(store.get, task_id)
            if record is None or (
                record["status"] in {"completed", "partial", "failed"} and not events
            ):
                break
            if not events:
                await asyncio.sleep(0.5)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request) -> dict[str, object]:
    _authorize_client(request)
    task = TaskStore().get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Factory task not found.")
    return task
