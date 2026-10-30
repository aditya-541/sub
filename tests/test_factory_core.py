from fastapi.testclient import TestClient

from subnet_factory.api import app
from subnet_factory.protocol import (
    AggregateRequest,
    BuildContribution,
    BuildTask,
    ProjectFile,
    SubnetSuggestion,
    TaskKind,
    ValidationReport,
)
from subnet_factory.repository import RepositoryAssemblyError, assemble_project
from subnet_factory.scoring import aggregate_accepted_files, validate_contribution
from subnet_factory.weights import build_weight_plan


def test_health_reports_missing_chain_configuration(monkeypatch):
    monkeypatch.delenv("FACTORY_LOCAL_MODE", raising=False)
    monkeypatch.delenv("FACTORY_NETUID", raising=False)

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "mode": "chain",
        "chain": "not-configured",
    }


def discovery_suggestions(prefix):
    return [
        SubnetSuggestion(
            name=f"{prefix}-{index}",
            problem=f"Problem {index}: teams need reliable, repeatable research computation.",
            commodity=f"Distinct verifiable research output type {index}.",
            users="Research labs and model developers.",
            verification="Validators rerun seeded workloads in isolated environments.",
            risks=["Hardware differences can affect runtime scores."],
            rationale=f"A shared benchmark {index} produces independently checked results.",
        )
        for index in range(1, 4)
    ]


def test_discovery_contribution_is_scored_deterministically():
    task = BuildTask(
        task_id="discovery-123456",
        kind=TaskKind.DISCOVER,
        prompt="Suggest useful subnet opportunities for researchers.",
    )
    contribution = BuildContribution(
        task_id=task.task_id,
        summary="A measurable opportunity for research workflows.",
        suggestions=discovery_suggestions("ReproBench"),
    )
    report = validate_contribution(task, contribution)
    assert report.accepted
    assert report.score == 1.0


def test_generation_rejects_missing_requested_file():
    task = BuildTask(
        task_id="generate-123456",
        kind=TaskKind.GENERATE,
        prompt="Implement a deterministic example component.",
        required_paths=["scoring.py"],
    )
    contribution = BuildContribution(
        task_id=task.task_id,
        summary="A response that omitted the requested artifact.",
    )
    report = validate_contribution(task, contribution)
    assert not report.accepted
    assert not report.checks["required_paths"]


def test_aggregation_skips_unaccepted_and_rejects_traversal():
    good = BuildContribution(
        task_id="generate-123456",
        summary="A safe, minimal implementation file.",
        files=[ProjectFile(path="src/scoring.py", content="score = 1\n")],
    )
    bad = BuildContribution(
        task_id="generate-654321",
        summary="An invalid path must never escape the project root.",
        files=[ProjectFile.model_construct(path="../escape.py", content="bad")],
    )
    files = aggregate_accepted_files(
        [good, bad],
        [
            ValidationReport(
                task_id="generate-123456",
                contribution_id=good.contribution_id,
                accepted=True,
                score=1,
                checks={},
                findings=[],
            ),
            ValidationReport(
                task_id="generate-654321",
                contribution_id=bad.contribution_id,
                accepted=False,
                score=0,
                checks={},
                findings=[],
            ),
        ],
    )
    assert files == {"src/scoring.py": "score = 1\n"}


def test_aggregation_rejects_conflicting_accepted_artifacts():
    contributions = [
        BuildContribution(
            task_id=f"generate-12345{i}",
            summary="Independent miner proposal for a shared artifact.",
            files=[ProjectFile(path="protocol.py", content=content)],
        )
        for i, content in enumerate(("version = 1", "version = 2"))
    ]
    reports = [
        ValidationReport(
            task_id=item.task_id,
            contribution_id=item.contribution_id,
            accepted=True,
            score=1,
            checks={},
            findings=[],
        )
        for item in contributions
    ]
    try:
        aggregate_accepted_files(contributions, reports)
    except ValueError as exc:
        assert "Conflicting" in str(exc)
    else:
        raise AssertionError("Conflicting artifacts must fail aggregation")


def test_discovery_api_returns_accepted_miner_suggestions(monkeypatch, tmp_path):
    import asyncio

    import httpx

    from subnet_factory import api
    from subnet_factory.network import Peer

    task = BuildTask(
        task_id="discovery-live-123",
        kind=TaskKind.DISCOVER,
        prompt="Find useful subnet opportunities for research teams.",
    )
    contribution = BuildContribution(
        task_id=task.task_id,
        summary="A measurable research subnet opportunity.",
        suggestions=discovery_suggestions("ProofBench"),
    )
    report = validate_contribution(task, contribution)

    class FakeNetwork:
        netuid = 42

        @property
        def hotkey_ss58(self):
            return "validator-hotkey"

        def eligible_validator(self, hotkey):
            return True

        def peers(self):
            return [Peer(hotkey="miner-hotkey", host="127.0.0.1:9001", validator_permit=False)]

    monkeypatch.setenv("FACTORY_LOCAL_MODE", "1")
    monkeypatch.setenv("FACTORY_STATE_DB", str(tmp_path / "factory.sqlite3"))
    monkeypatch.setattr(api, "BittensorNetwork", FakeNetwork)

    async def fake_dispatch(received_task, *, network):
        assert received_task.kind == TaskKind.DISCOVER
        assert isinstance(network, FakeNetwork)
        return [contribution], [report]

    monkeypatch.setattr(api, "dispatch_task", fake_dispatch)

    async def exercise_api():
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            result = await client.post(
                "/suggestions",
                json={"prompt": "Find useful subnet opportunities for research teams."},
            )
            record = await client.get(f"/tasks/{result.json()['task_id']}")
            return result, record

    response, task_record = asyncio.run(exercise_api())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["attempted_peers"] == 1
    assert payload["accepted_count"] == 1
    assert payload["suggestions"][0]["name"] == "ProofBench-1"
    assert task_record.status_code == 200
    assert task_record.json()["status"] == "completed"
    assert task_record.json()["outcome"]["accepted_count"] == 1


def test_chain_mode_requires_client_authentication(monkeypatch):
    import asyncio

    import httpx

    from subnet_factory.api import app

    monkeypatch.setenv("FACTORY_LOCAL_MODE", "0")
    monkeypatch.delenv("FACTORY_API_TOKEN", raising=False)

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/suggestions",
                json={"prompt": "Find useful subnet opportunities for research teams."},
            )

    response = asyncio.run(request())
    assert response.status_code == 503
    assert "authentication is not configured" in response.json()["detail"]


def test_assembly_writes_accepted_project_and_manifest(tmp_path):
    artifact = BuildContribution(
        task_id="generate-assemble-123",
        summary="A small project artifact for assembly.",
        files=[ProjectFile(path="src/main.py", content="print('generated')\n")],
    )
    report = ValidationReport(
        task_id=artifact.task_id,
        contribution_id=artifact.contribution_id,
        accepted=True,
        score=1,
        checks={"syntax": True},
        findings=[],
    )
    outcome = assemble_project(
        "project-one",
        AggregateRequest(contributions=[artifact], reports=[report]),
        tmp_path,
    )
    project = tmp_path / "project-one"
    assert outcome["status"] == "assembled-locally"
    assert (project / "src/main.py").read_text() == "print('generated')\n"
    assert (project / "factory-manifest.json").exists()
    try:
        assemble_project(
            "project-one", AggregateRequest(contributions=[artifact], reports=[report]), tmp_path
        )
    except RepositoryAssemblyError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("Assembly must never overwrite an existing project")


def test_build_api_dispatches_validates_assembles_and_persists(monkeypatch, tmp_path):
    import asyncio

    import httpx

    from subnet_factory import api
    from subnet_factory.network import Peer

    monkeypatch.setenv("FACTORY_LOCAL_MODE", "1")
    monkeypatch.setenv("FACTORY_STATE_DB", str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setenv("FACTORY_ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    class FakeNetwork:
        netuid = 0

        def peers(self):
            return [Peer(hotkey="miner-a", host="127.0.0.1:8001", validator_permit=False)]

    contribution = BuildContribution(
        task_id="placeholder-task-id",
        summary="A runnable generated subnet project file.",
        files=[ProjectFile(path="README.md", content="# Generated subnet\n")],
    )

    async def fake_dispatch(task, *, network, on_peer_event=None):
        assert task.kind == TaskKind.GENERATE
        assert task.required_paths == ["README.md"]
        if on_peer_event:
            await on_peer_event(
                Peer(hotkey="miner-a", host="127.0.0.1:8001", validator_permit=False),
                "returned",
            )
        contribution.task_id = task.task_id
        return [contribution], [validate_contribution(task, contribution)]

    monkeypatch.setattr(api, "BittensorNetwork", FakeNetwork)
    monkeypatch.setattr(api, "dispatch_task", fake_dispatch)

    async def request():
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            queued = await client.post(
                "/projects/build",
                json={
                    "project_id": "code-eval-subnet",
                    "prompt": "Build a small code evaluation subnet project.",
                    "required_paths": ["README.md"],
                },
            )
            task_id = queued.json()["task_id"]
            assert queued.status_code == 202
            assert queued.json()["status"] == "queued"
            assert await api.process_next_build_job()
            task_status = await client.get(f"/tasks/{task_id}")
            preview = await client.get("/projects/code-eval-subnet/files")
            events = await client.get(f"/tasks/{task_id}/events")
            return queued, task_status, preview, events

    response, task_status, preview, events = asyncio.run(request())
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"
    assert task_status.json()["status"] == "completed"
    assert task_status.json()["outcome"]["accepted_count"] == 1
    assert task_status.json()["outcome"]["project"]["status"] == "assembled-locally"
    assert (
        tmp_path / "artifacts" / "code-eval-subnet" / "README.md"
    ).read_text() == "# Generated subnet\n"
    assert preview.status_code == 200
    assert preview.json()["files"]["README.md"] == "# Generated subnet\n"
    assert "event: queued" in events.text
    assert '"phase": "dispatching-to-miners"' in events.text
    assert "event: miner" in events.text
    assert "event: complete" in events.text


def test_cors_only_allows_configured_tensor_studio_origins(monkeypatch):
    import asyncio

    import httpx

    from subnet_factory.api import app

    monkeypatch.setenv("FACTORY_CORS_ORIGINS", "https://studio.example")

    async def request(origin):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.options(
                "/projects/build",
                headers={
                    "origin": origin,
                    "access-control-request-method": "POST",
                    "access-control-request-headers": "authorization,content-type",
                },
            )

    allowed, denied = (
        asyncio.run(request("https://studio.example")),
        asyncio.run(request("https://attacker.example")),
    )
    assert allowed.status_code == 204
    assert allowed.headers["access-control-allow-origin"] == "https://studio.example"
    assert "access-control-allow-origin" not in denied.headers


def test_openai_compatible_generator_falls_back_without_json_mode(monkeypatch):
    import io

    from examples import openai_compatible_generator

    task = BuildTask(
        task_id="generate-json-fallback-123",
        kind=TaskKind.GENERATE,
        prompt="Generate one valid scoring module for a code evaluation subnet.",
        specification={"max_output_tokens": 1000},
        required_paths=["scoring.py"],
    )
    contribution = BuildContribution(
        task_id=task.task_id,
        summary="A deterministic scoring implementation for submissions.",
        files=[ProjectFile(path="scoring.py", content="def score(value):\n    return value\n")],
    )
    calls = []

    class FakeResponse:
        def __init__(self, status_code, text="", body=None):
            self.status_code = status_code
            self.text = text
            self.body = body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return self.body

    def fake_post(url, *, json, headers, timeout):
        calls.append(dict(json))
        if len(calls) == 1:
            return FakeResponse(501, "structured output is unavailable")
        return FakeResponse(
            200,
            body={"choices": [{"message": {"content": contribution.model_dump_json()}}]},
        )

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("OLLAMA_MODEL", "factory-coder")
    monkeypatch.setattr(openai_compatible_generator.httpx, "post", fake_post)
    monkeypatch.setattr(
        openai_compatible_generator.sys, "stdin", io.StringIO(task.model_dump_json())
    )
    output = io.StringIO()
    monkeypatch.setattr(openai_compatible_generator.sys, "stdout", output)

    assert openai_compatible_generator.main() == 0
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == 1000
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]
    result = BuildContribution.model_validate_json(output.getvalue())
    assert result.task_id == task.task_id
    assert result.generator == "openai-compatible:factory-coder"


def test_weight_plan_averages_per_task_and_normalizes_by_miner():
    from types import SimpleNamespace

    contributions = [
        BuildContribution(
            task_id=task_id,
            miner_hotkey=hotkey,
            summary="A miner response included for validator weighting.",
        )
        for task_id, hotkey in (
            ("task-a", "miner-a"),
            ("task-a", "miner-a"),
            ("task-b", "miner-a"),
            ("task-a", "miner-b"),
        )
    ]
    scores = (0.6, 0.9, 0.3, 0.3)
    reports = [
        ValidationReport(
            task_id=item.task_id,
            contribution_id=item.contribution_id,
            accepted=True,
            score=score,
            checks={},
            findings=[],
        )
        for item, score in zip(contributions, scores, strict=True)
    ]
    plan = build_weight_plan(
        42,
        contributions,
        reports,
        [SimpleNamespace(hotkey="miner-a", uid=4), SimpleNamespace(hotkey="miner-b", uid=9)],
    )
    assert plan.status == "plan-only"
    assert [item.uid for item in plan.weights] == [4, 9]
    assert plan.weights[0].raw_score == 0.6
    assert abs(plan.weights[0].weight - 2 / 3) < 1e-12
    assert abs(sum(item.weight for item in plan.weights) - 1) < 1e-12


def test_task_store_survives_reopening(tmp_path):
    from subnet_factory.store import TaskStore

    task = BuildTask(
        task_id="architecture-persist-123",
        kind=TaskKind.ARCHITECTURE,
        prompt="Design a reliable code evaluation subnet architecture.",
    )
    first = TaskStore(tmp_path / "tasks.sqlite3")
    first.save(task, status="running")
    first.save(task, status="completed", outcome={"accepted_count": 2})
    reopened = TaskStore(tmp_path / "tasks.sqlite3")
    record = reopened.get(task.task_id)
    assert record is not None
    assert record["status"] == "completed"
    assert record["outcome"] == {"accepted_count": 2}


def test_build_queue_requeues_interrupted_jobs_after_restart(tmp_path):
    from subnet_factory.store import TaskStore

    database = tmp_path / "queue.sqlite3"
    task = BuildTask(
        task_id="generate-queue-recovery-123",
        kind=TaskKind.GENERATE,
        prompt="Build a deterministic subnet scoring component.",
        required_paths=["scoring.py"],
    )
    first = TaskStore(database)
    first.enqueue_build(task, "recovery-project")
    claim = first.claim_build_job()
    assert claim == {"task": task, "project_id": "recovery-project"}
    first.save(task, status="running")

    reopened = TaskStore(database)
    assert reopened.recover_running_build_jobs() == 1
    record = reopened.get(task.task_id)
    assert record is not None and record["status"] == "queued"
    assert reopened.events_after(task.task_id)[-1]["event_type"] == "requeued"
    assert reopened.claim_build_job() == {"task": task, "project_id": "recovery-project"}


def test_build_queue_applies_backpressure(monkeypatch, tmp_path):
    from subnet_factory.store import BuildQueueFullError, TaskStore

    monkeypatch.setenv("FACTORY_BUILD_QUEUE_LIMIT", "1")
    store = TaskStore(tmp_path / "bounded-queue.sqlite3")
    first = BuildTask(
        task_id="generate-queue-first-123",
        kind=TaskKind.GENERATE,
        prompt="Build a first deterministic scoring module.",
    )
    second = BuildTask(
        task_id="generate-queue-second-123",
        kind=TaskKind.GENERATE,
        prompt="Build a second deterministic scoring module.",
    )
    store.enqueue_build(first, "first-project")
    try:
        store.enqueue_build(second, "second-project")
    except BuildQueueFullError:
        pass
    else:
        raise AssertionError("The bounded queue must reject excess work.")
    assert store.get(second.task_id) is None


def test_chain_peer_discovery_allows_private_axons_only_on_localnet():
    from types import SimpleNamespace

    from subnet_factory.network import BittensorNetwork

    neurons = [
        SimpleNamespace(hotkey="local-miner", uid=1, axon="127.0.0.1:8001", validator_permit=False),
        SimpleNamespace(hotkey="public-miner", uid=2, axon="8.8.8.8:8002", validator_permit=False),
        SimpleNamespace(hotkey="validator", uid=3, axon="8.8.4.4:8003", validator_permit=True),
    ]
    local = object.__new__(BittensorNetwork)
    local.network = "local"
    local.local_mode = False
    local.metagraph = lambda **kwargs: SimpleNamespace(neurons=neurons)
    assert [peer.hotkey for peer in local.peers()] == ["local-miner", "public-miner"]

    public = object.__new__(BittensorNetwork)
    public.network = "finney"
    public.local_mode = False
    public.metagraph = lambda **kwargs: SimpleNamespace(neurons=neurons)
    assert [peer.hotkey for peer in public.peers()] == ["public-miner"]


def test_bittensor_11_signed_request_authenticates_exact_body_and_receiver():
    import time

    import bittensor as bt
    from bittensor.keyfiles import Keypair

    validator = Keypair.create_from_uri("//Alice")
    miner = Keypair.create_from_uri("//Bob")
    body = b'{"task_id":"factory-test"}'
    path = "/tasks/execute"
    nonce = time.time_ns()
    headers = bt.http_auth.sign(
        validator,
        method="POST",
        path=path,
        body=body,
        receiver_ss58=miner.ss58_address,
        nonce_ns=nonce,
    )
    caller = bt.http_auth.verify(
        headers,
        body,
        method="POST",
        path=path,
        self_hotkey_ss58=miner.ss58_address,
        now_ns=nonce,
    )
    assert caller.hotkey_ss58 == validator.ss58_address


def test_bittensor_11_weight_intent_accepts_normalized_uid_mapping():
    import bittensor as bt

    intent = bt.SetWeights(netuid=42, weights={4: 0.7, 9: 0.3})
    assert intent.netuid == 42
    assert intent.uids == [4, 9]
    assert intent.weights == [0.7, 0.3]


def test_dispatch_signs_miner_request_and_validates_returned_contribution(monkeypatch):
    import asyncio

    import bittensor as bt
    import httpx
    from bittensor.keyfiles import Keypair

    from subnet_factory import dispatch
    from subnet_factory.network import Peer

    validator = Keypair.create_from_uri("//Alice")
    miner = Keypair.create_from_uri("//Bob")
    task = BuildTask(
        task_id="signed-dispatch-123",
        kind=TaskKind.DISCOVER,
        prompt="Suggest three subnet ideas for verifiable computation.",
    )
    contribution = BuildContribution(
        task_id=task.task_id,
        miner_hotkey=miner.ss58_address,
        summary="Three distinct ideas returned from an authenticated miner.",
        suggestions=discovery_suggestions("SignedIdea"),
    )

    class FakeNetwork:
        wallet = validator

        def peers(self):
            return [Peer(hotkey=miner.ss58_address, host="8.8.8.8:9000", validator_permit=False)]

    def handler(request):
        caller = bt.http_auth.verify(
            request.headers,
            request.content,
            method=request.method,
            path=request.url.raw_path.decode(),
            self_hotkey_ss58=miner.ss58_address,
        )
        assert caller.hotkey_ss58 == validator.ss58_address
        return httpx.Response(200, json=contribution.model_dump(mode="json"))

    real_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(dispatch.httpx, "AsyncClient", mock_client)
    peer_events = []

    async def record_peer_event(peer, status):
        peer_events.append((peer.hotkey, status))

    results, reports = asyncio.run(
        dispatch.dispatch_task(task, network=FakeNetwork(), on_peer_event=record_peer_event)
    )
    assert len(results) == 1
    assert reports[0].accepted
    assert reports[0].score == 1.0
    assert [status for _, status in peer_events] == ["started", "returned"]


def test_miner_generator_receives_only_allowlisted_environment(monkeypatch, tmp_path):
    import sys

    from subnet_factory.generator import generate

    script = tmp_path / "generator.py"
    script.write_text(
        "import json, os, sys\n"
        "task = json.load(sys.stdin)\n"
        "print(json.dumps({'task_id': task['task_id'], 'summary': "
        "'allowed=' + os.getenv('FACTORY_TEST_ALLOWED', '') + ';secret=' + "
        "os.getenv('FACTORY_TEST_SECRET', 'absent')}))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FACTORY_GENERATOR_COMMAND", f"{sys.executable} {script}")
    monkeypatch.setenv("FACTORY_GENERATOR_ENV", "FACTORY_TEST_ALLOWED")
    monkeypatch.setenv("FACTORY_TEST_ALLOWED", "visible")
    monkeypatch.setenv("FACTORY_TEST_SECRET", "must-not-leak")
    task = BuildTask(
        task_id="generator-env-123",
        kind=TaskKind.ARCHITECTURE,
        prompt="Design a simple, reproducible subnet architecture.",
    )
    result = generate(task)
    assert result.summary == "allowed=visible;secret=absent"
