# Run the Subnet Factory on localnet

This is an operator guide for an isolated development chain. It does not create a subnet on testnet or mainnet. The Bittensor docs describe the localnet Docker image and lifecycle commands; see [Local development](https://www.bittensor.com/docs/guides/local-development).

## 1. Start an isolated Subtensor node

In terminal 1:

```sh
sh scripts/run_localnet.sh
```

The localnet exposes WebSocket RPC at `ws://127.0.0.1:9944`. It includes subnet 1; registering a new subnet with a local owner typically returns netuid 2. State is discarded when the container exits with the provided script.

## 2. Install and prepare local wallets

In terminal 2:

```sh
uv venv
. .venv/bin/activate
uv pip install -e '.[dev]'
```

Create local-only owner, validator, and miner wallets. The Alice key is a public development key funded only by localnet; never use it on any public network.

```sh
btcli wallet regen-coldkey -w alice --no-password \
  --seed 0xe5be9a5092b81bca64be81d212e7f2f9eba183bb7a90954f7b76361f6edb5c0a
btcli wallet create -w factory-owner -H default --no-password
btcli wallet create -w factory-validator -H default --no-password
btcli wallet create -w factory-miner -H default --no-password
```

Fund the local wallets and register a subnet. These commands mutate only the isolated localnet:

```sh
btcli tx transfer --dest factory-owner --amount-tao 2000 -w alice --network local
btcli tx transfer --dest factory-validator --amount-tao 100 -w alice --network local
btcli tx transfer --dest factory-miner --amount-tao 100 -w alice --network local
btcli tx register-subnet -w factory-owner --network local
```

Read the returned netuid and use it below instead of `2` if it differs. Only activate after the miner and validator are operating:

```sh
btcli tx burned-register --netuid 2 -w factory-validator --network local
btcli tx burned-register --netuid 2 -w factory-miner --network local
```

## 3. Start a Factory miner

Each miner operator configures their own generation backend. The command receives one JSON `BuildTask` on stdin and returns one JSON `BuildContribution` on stdout. See the Pydantic contracts in `subnet_factory/protocol.py`. Run the model/tool adapter with its own least-privileged OS user and an isolated worker; the current command adapter is not a sandbox.

For the included adapter, run Ollama or another OpenAI-compatible model server on the operator's host, set `OLLAMA_MODEL`, and use `OLLAMA_BASE_URL=http://host.docker.internal:11434/v1` for a containerized miner. The adapter follows Ollama's documented OpenAI-compatible chat endpoint. Model choice and hardware are operator decisions.

Set miner environment and start its HTTP service:

```sh
export BT_NETWORK=local
export FACTORY_NETUID=2
export BT_WALLET=factory-miner
export BT_WALLET_HOTKEY=default
export FACTORY_PORT=8001
export FACTORY_GENERATOR_COMMAND='python /path/to/operator_generator.py'
sh scripts/run_service.sh
```

In a separate terminal, publish the miner endpoint to the local chain:

```sh
btcli tx serve-axon --netuid 2 --ip 127.0.0.1 --port 8001 \
  -w factory-miner -H default --network local --dry-run
```

Review the dry run, then repeat without `--dry-run` to publish the endpoint.

## 4. Start the validator API and request suggestions

In another terminal, start the validator service with its own wallet. Use a long random API token for the client-facing route:

```sh
export BT_NETWORK=local
export FACTORY_NETUID=2
export BT_WALLET=factory-validator
export BT_WALLET_HOTKEY=default
export FACTORY_API_TOKEN='replace-with-a-long-random-secret'
export FACTORY_PORT=8000
sh scripts/run_service.sh
```

After confirming both processes are reachable, activate the local subnet so emissions begin:

```sh
btcli tx start-call --netuid 2 -w factory-owner --network local
```

Submit a discovery request through the validator. The validator uses its hotkey to sign each miner request; the miner checks that the caller has a validator permit on this netuid.

```sh
curl http://127.0.0.1:8000/suggestions \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer replace-with-a-long-random-secret' \
  -d '{"prompt":"Suggest three subnet opportunities for reproducible AI code evaluation."}'
```

The response contains each miner contribution, deterministic validation reports, and the accepted suggestion list. The generated task is also persisted in SQLite and can be fetched at `/tasks/{task_id}`.

## 5. Plan and explicitly submit weights

Save the response contribution and report arrays as a JSON `AggregateRequest`, then call `/weights/plan` with the validator API token. The plan is a preview only. To submit it, save the returned plan JSON and invoke the operator CLI interactively:

```sh
factory-submit-weights weight-plan.json \
  --network local \
  --wallet factory-validator \
  --hotkey default \
  --confirm 'SUBMIT WEIGHTS NETWORK=local NETUID=2 HOTKEY=<validator-ss58>'
```

The CLI displays the recipients, runs SDK preflight, and then requires the operator to type the exact confirmation phrase interactively before signing. It refuses non-interactive submission. Do not use this local development key or localnet flow on mainnet.

## Limits of this milestone

- This local setup validates protocol and process wiring; it does not prove the incentive design is economically robust.
- Generated code still needs isolated execution, richer tests, dependency pinning, static analysis, and security review before public miners execute it.
- The first version has no central task queue across multiple validator replicas, nor distributed artifact storage.
- Mainnet subnet registration, activation, infrastructure provisioning, GitHub publishing, and Tensor Studio integration are separate, explicitly approved workflows.
