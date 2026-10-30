# Subnet build test record

## Live CreateOS build attempt — 2026-10-30

- Deployment: `https://production-subnet-factory-a541.tyzo.nodeops.app`
- Task ID: `generate-98be984976b443dbad7a8a43aab427a4`
- Request: build a minimal Bittensor subnet for deterministic Python code evaluation, including miner, validator, protocol, scoring, README, and project configuration files.
- API submission: HTTP `202 Accepted`; the job was persisted and picked up by the worker.
- Result: **failed before miner dispatch** with `FACTORY_NETUID must be configured.` No miner contributions were returned and no generated repository or files were assembled.

## Interpretation

The live endpoint, authentication, queue submission, and worker status update worked. This was not a successful subnet build and does not establish that the deployed Factory can generate subnets. The Production deployment has no Factory netuid or validator hotkey, and no active Factory miners. It also has no miner generation backend configured. Registration and validator setup require an authorized Bittensor transaction; miner operators must separately start and register their miner services with working generation backends.

The local automated suite passes, including a build-flow test with a stubbed miner contribution. That test verifies queue, validation, assembly, and persistence mechanics; it does not replace a live miner build.
