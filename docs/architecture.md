# Subnet Factory architecture

## Product boundary

Subnet Factory is itself intended to be a native Bittensor subnet. It is independent of Tensor Studio, which will later provide its user-facing workspace. Generated subnet repositories are separate build artifacts and never become the Factory's own codebase.

## Work flow

```text
User idea (later via Tensor Studio)
  -> task specification
  -> validator broadcasts a bounded task to eligible Factory miners
  -> miners independently return proposals or implementation artifacts
  -> validators run deterministic schema, quality, safety, and reproducibility checks
  -> validator scores become Bittensor weights through the subnet's on-chain mechanism
  -> accepted, compatible artifacts are aggregated into a versioned project
```

The validator API writes project builds to a persistent SQLite queue. A separate single worker claims one job at a time, dispatches tasks to miners, records phase/miner events, validates outputs, and assembles accepted artifacts. The API streams those events to clients as SSE while remaining available. A restarted worker requeues unfinished jobs. This SQLite queue is single-host/single-worker; use a shared production queue and database before horizontal scaling.

The coordinator handles task assignment and aggregation. It must not silently substitute centralized generation for miner contributions. Optional model tools run under each miner's own operator configuration.

## Participant responsibilities

| Role | Work |
| --- | --- |
| Factory miner | Suggest subnet opportunities; produce architecture, protocol, scoring, code, tests, and deployment artifacts for assigned tasks. |
| Factory validator | Create/evaluate tasks; independently check submissions; score correctness, completeness, security signals, and reproducibility; submit weights. |
| Coordinator | Dispatch requests, deduplicate and aggregate validator-approved artifacts, and expose progress to clients. Task status, events, and queued builds are stored in SQLite. |
| Tensor Studio | Separate future client for conversation, project UI, approvals, GitHub, and deployment workflows. |

## Task phases

1. **Discover:** miners propose distinct subnet opportunities matching a user goal, with users, commodity, measurable demand, verification plan, and risks.
2. **Design:** miners return typed architecture sections with explicit assumptions and interfaces.
3. **Generate:** tasks request specific files or components against an approved specification and pinned SDK target.
4. **Validate:** miners may provide independent test plans; validators execute controlled checks and score artifacts.
5. **Assemble:** only compatible, accepted files enter an isolated generated repository.

## Trust boundaries and open work

- Treat task text, miner output, generated code, dependencies, and model output as untrusted.
- Execute generated code only in disposable, resource-limited workers with network access disabled by default.
- Validator scoring should be deterministic where possible and must not rely solely on an LLM judge.
- The code includes a chain adapter for metagraph discovery, hotkey-signed requests, validator-permit checks, a normalized weight-plan preview, and a separate interactive weight submission CLI. It has not been exercised against a live or local Subtensor node. Automated validator cadence, multi-host queueing, and economic parameters remain before network launch.
- Never let an agent/miner trigger wallet signing, registration spending, cloud provisioning, or mainnet activation.
