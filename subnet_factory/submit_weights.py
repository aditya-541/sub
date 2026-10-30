"""Interactive operator-only CLI for submitting a reviewed Factory weight plan."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .protocol import WeightPlan


def confirmation_phrase(network: str, netuid: int, hotkey: str) -> str:
    return f"SUBMIT WEIGHTS NETWORK={network} NETUID={netuid} HOTKEY={hotkey}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="JSON response saved from POST /weights/plan")
    parser.add_argument("--network", default="test", help="Bittensor network name or endpoint")
    parser.add_argument("--wallet", default="default", help="Local wallet name")
    parser.add_argument("--hotkey", default="default", help="Local validator hotkey name")
    parser.add_argument("--confirm", required=True, help="Exact phrase shown after plan review")
    args = parser.parse_args(argv)

    try:
        plan = WeightPlan.model_validate_json(args.plan.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        parser.error(f"Could not load a valid weight plan: {exc}")
    if not plan.weights:
        parser.error("The plan contains no positive miner weights.")

    try:
        import bittensor as bt
    except ImportError:
        parser.error("Install the pinned Bittensor SDK before submitting weights.")

    wallet = bt.Wallet(name=args.wallet, hotkey=args.hotkey)
    validator_hotkey = wallet.hotkey.ss58_address
    phrase = confirmation_phrase(args.network, plan.netuid, validator_hotkey)
    if args.confirm != phrase:
        parser.error(f"Confirmation does not match this plan. Required: {phrase}")

    print(
        json.dumps(
            {
                "network": args.network,
                "netuid": plan.netuid,
                "validator_hotkey": validator_hotkey,
                "miners": [item.model_dump() for item in plan.weights],
                "status": "preflight",
            },
            indent=2,
        )
    )
    if not sys.stdin.isatty():
        print("Refusing to sign without an interactive confirmation.", file=sys.stderr)
        return 2
    typed = input(f"Type exactly '{phrase}' to submit these weights: ")
    if typed != phrase:
        print("Confirmation did not match; no transaction was submitted.", file=sys.stderr)
        return 2

    weights = {item.uid: item.weight for item in plan.weights}
    intent = bt.SetWeights(netuid=plan.netuid, weights=weights)
    subtensor = bt.Subtensor(network=args.network)
    preview = subtensor.plan(intent, wallet)
    print(f"SDK preflight: {preview}")
    result = subtensor.execute(intent, wallet)
    if not result.success:
        error = result.error
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": str(getattr(error, "code", "unknown")),
                    "remediation": getattr(error, "remediation", None),
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": "submitted",
                "block_hash": str(getattr(result, "block_hash", "")),
                "extrinsic_id": str(getattr(result, "extrinsic_id", "")),
                "explorer_url": getattr(result, "explorer_url", None),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
