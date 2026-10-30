# Subnet Factory

Subnet Factory is a standalone Bittensor subnet project. It is the decentralized build engine Tensor Studio will connect to later; Tensor Studio is a separate product and is intentionally not included here.

The Factory is designed to help people discover, design, generate, validate, and assemble new subnet projects. Factory miners perform bounded engineering tasks using their own configured generation backend. Validators score the returned proposals and artifacts using reproducible checks before they are eligible for aggregation. The coordinator only assigns work and aggregates accepted contributions; it is not the generator of record.

## First milestone

This repository starts with the Factory's core domain, peer protocol, and local workflow:

- typed, versioned task and contribution contracts;
- subnet idea suggestions and structured architecture tasks;
- a miner service with a pluggable local generation command;
- deterministic artifact checks and validator scoring;
- safe, durable repository assembly that rejects path traversal and conflicting files;
- Bittensor metagraph peer discovery, signed validator-to-miner requests, and validator-permit checks;
- a local HTTP API for exercising the workflow before chain registration;
- a SQLite task journal that survives API restarts;
- normalized per-miner weight plan previews, with no automatic chain submission.

This is not yet registered on a Bittensor network, and the chain adapter has not yet been exercised against a live or local Subtensor node. The current SDK uses hotkey-signed HTTP (`bittensor.http_auth`) for miner-validator requests rather than the legacy Axon/Dendrite/Synapse networking stack. The API only creates normalized weight plans; an operator can submit one with the separate interactive `factory-submit-weights` CLI. There is no automatic weight transaction loop.

## Run locally

Requires Python 3.10–3.14. Install with `uv`:

```sh
uv venv
source .venv/bin/activate
uv pip install -e '.[dev]'
uvicorn subnet_factory.api:app --reload
```

The API exposes `/health`, `/suggestions`, `/tasks/architecture`, `/tasks/dispatch`, `/tasks/{task_id}`, `/tasks/{task_id}/events`, `/projects/build`, `/validate`, `/aggregate`, `/projects/{project_id}/assemble`, and `/weights/plan`. `/projects/build` returns HTTP 202 immediately and enqueues the build in SQLite (bounded by `FACTORY_BUILD_QUEUE_LIMIT`, default 16). The separate validator-profile worker dispatches generation to registered Factory miners, validates contributions, assembles accepted files under `FACTORY_ARTIFACT_ROOT`, and persists task state. `GET /tasks/{task_id}/events` streams phase and miner-result events as Server-Sent Events; `GET /tasks/{task_id}` returns the latest persisted status and outcome. A restarted worker requeues interrupted jobs. This SQLite queue is for one host and one worker; public multi-host scaling requires a shared production queue/database. For local trusted development, set `FACTORY_LOCAL_MODE=1` and `FACTORY_LOCAL_PEERS=127.0.0.1:8001` (comma-separated `host:port` values). Local mode disables request signatures and must not be exposed publicly. Task state is stored in SQLite at `FACTORY_STATE_DB` (default `./data/factory.sqlite3`). See `docs/architecture.md` for the work split and trust boundaries.

Tensor Studio or another application backend can call `/projects/build` with `POST`, a bearer token, and JSON like `{"project_id":"code-eval-subnet","prompt":"Build an AI code evaluation subnet","required_paths":["README.md","neurons/miner.py","neurons/validator.py"]}`. The immediate response contains a task ID and event URL. Read the SSE URL for phase/miner progress, poll `/tasks/{task_id}` to recover status after browser refresh, and fetch previewable files from `GET /projects/{project_id}/files`. Configure `FACTORY_CORS_ORIGINS` as a comma-separated list of exact browser origins (for example `https://studio.example`); wildcard origins are not enabled. Store the API token server-side in the application backend and proxy requests from the browser rather than shipping the shared token to client JavaScript. The Tensor Studio source repository is not present alongside this Factory project, so a direct UI patch cannot be applied from this checkout; these endpoints are ready for that separate application to integrate.

The server-side TypeScript helper in [`examples/tensor_studio_factory_client.ts`](examples/tensor_studio_factory_client.ts) implements build submission, status lookup, event streaming, and artifact preview. Configure `SUBNET_FACTORY_URL` and `SUBNET_FACTORY_TOKEN` only in the Studio server environment.

Configure a miner's generation command with `FACTORY_GENERATOR_COMMAND`. The command receives a JSON task on stdin and must return a JSON contribution on stdout. `examples/openai_compatible_generator.py` is a reference backend for a local Ollama server or another operator-managed OpenAI-compatible endpoint; Ollama documents its `/v1/chat/completions` compatibility [here](https://docs.ollama.com/api/openai-compatibility). Set `FACTORY_GENERATOR_ENV` to the exact variables the child process may receive; secrets are not inherited by default. Operators can instead provide any local/self-hosted adapter, so the Factory does not depend on one centralized LLM provider. Do not run untrusted generator commands in a privileged environment.

### Free local model setup

For a free local experiment, use Ollama with Qwen3.5 2B. Its model card lists Apache-2.0, and prompts stay on the local machine. However, a real test on an 8 GB Mac showed significant memory pressure: Ollama reported about 1.4 GB free while the model was loaded, and a six-file generation task timed out at 600 seconds. Do not use this configuration for interactive or production builds on that hardware. Production miners need independently provisioned compute sized for the model and task; the Factory does not supply that compute itself.

Install Ollama from [ollama.com](https://ollama.com), then run:

```sh
ollama pull qwen3.5:2b
ollama create factory-coder -f examples/FactoryModelfile
```

The Modelfile caps context at 4,096 tokens to reduce memory use and sets a conservative temperature. For local experiments, keep each generation task small and set `specification.max_output_tokens` to a low value (for example 512–1,024); large multi-file tasks are likely to be slow or time out on this Mac. Copy `.env.miner.example` to `.env.miner` (or export the same variables) and use:

```dotenv
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
OLLAMA_MODEL=factory-coder
OPENAI_COMPATIBLE_API_KEY=
```

When the miner runs in Docker Compose on macOS, use `http://host.docker.internal:11434/v1` for `OLLAMA_BASE_URL`, as in `.env.miner.example`. The model is an inference backend, not a trusted code executor: generated artifacts remain untrusted and must pass validation. For a responsive production service, use miner operators with stronger GPUs or a better-performing operator-managed OpenAI-compatible model; never put provider keys in prompts or commit them to the repository.

For the local Bittensor chain setup, wallet registration, serving miner endpoints, and a first discovery request, follow [`docs/localnet.md`](docs/localnet.md).

For container builds, copy `.env.validator.example` to `.env.validator` and `.env.miner.example` to `.env.miner`, replace the placeholder validator API token, create the role's Bittensor hotkey on the host, and then start the roles as separate Compose projects. Compose mounts only the role's hotkey directory read-only; it does not mount the coldkey. Set `FACTORY_HOTKEYS_HOST_DIR` if your host stores that hotkey outside `~/.bittensor/wallets/<BT_WALLET>/hotkeys`. The API port binds to loopback by default; expose it to the internet only through a TLS reverse proxy with application-side authentication.

```sh
FACTORY_ROLE_ENV_FILE=.env.validator docker-compose --env-file .env.validator -p factory-validator --profile validator up --build -d
FACTORY_ROLE_ENV_FILE=.env.miner docker-compose --env-file .env.miner -p factory-miner up --build -d
```

Set a private API token and a miner generation command in the matching local env file first. Do not reuse a validator hotkey as a miner identity.

### Public HTTPS deployment

The optional `public` Compose profile runs Caddy in front of the API. On a Linux VPS, point a DNS name at the host, allow inbound ports 80 and 443, set `FACTORY_DOMAIN` and the exact Studio origin in `FACTORY_CORS_ORIGINS`, set a strong private `FACTORY_API_TOKEN`, and configure the validator hotkey directory. Then run:

```sh
FACTORY_ROLE_ENV_FILE=.env.validator docker-compose --env-file .env.validator -p factory-validator --profile validator --profile public up --build -d
```

Caddy obtains and renews HTTPS certificates when the domain resolves to the host and its ports are reachable. Keep the Factory token in the Studio server environment and proxy requests through Studio's backend; do not distribute it to browsers. This deploys the API and single-host worker, but build jobs still need a registered Factory subnet and reachable miners. The public profile does not register a subnet, provision miners, or make the local Mac model a suitable production generator.

### Render deployment

`render.yaml` is a Render Blueprint for a single Docker web service that starts both the API and one build worker. It uses a persistent disk for the SQLite task queue and generated artifacts, so it requires Render's paid Starter service and a persistent disk; the free service tier cannot provide durable storage or a continuously running worker. Connect this repository in Render and deploy the Blueprint to receive a platform `onrender.com` URL; no custom domain is required. Render generates `FACTORY_API_TOKEN` and keeps it server-side. Copy that token from the Render service environment into the Tensor Studio server environment as `SUBNET_FACTORY_TOKEN`. Configure `FACTORY_CORS_ORIGINS` to the exact Studio origin before browser access. Do not expose the token in client-side code.

This deployment runs one worker and a single-host SQLite queue. It is suitable for an initial accessible deployment, not horizontal scaling. It also does not register or activate the Factory on Bittensor; actual subnet generation requires registered Factory miners and validators to be online.

### Current CreateOS deployment

The current free-plan deployment is available at <https://production-subnet-factory-a541.tyzo.nodeops.app>. Its `/health` endpoint is public; Factory client routes require the `FACTORY_API_TOKEN` configured in the CreateOS Production environment. Set `SUBNET_FACTORY_URL` and `SUBNET_FACTORY_TOKEN` in the Tensor Studio **server** environment, never in browser code.

This is an initial hosted API smoke-test deployment, not a durable production service. It currently runs at 250 millicores and 512 MB RAM. Task state and generated files use `/tmp`, so a restart loses them. CreateOS bills compute by the second; the Free plan does not mean unlimited always-on compute. The current deployment has no registered Factory validator hotkey, configured netuid, or connected miners. Health, API authentication, and architecture-task persistence were verified, but generated subnet builds are not yet available until the Bittensor Factory participants are configured and online. A production rollout also needs durable task/artifact storage and a verified spend limit.

## Safety and status

- No transaction is submitted automatically. Registration and activation require manual `btcli` operations; weight submission requires the separate interactive CLI and exact confirmation. The CreateOS API deployment is separate from GitHub OAuth and Tensor Studio integration. Chain mode reads the metagraph and uses an operator's local hotkey to sign peer requests.
- The local generator is a development interface, not a sandbox. Production execution needs isolated worker containers, resource limits, egress controls, and artifact scanning.
- Validation scores are only a starting benchmark. They do not establish that generated code is correct or secure in every runtime environment.
- Never describe a local task as a subnet registered or deployed on-chain.
