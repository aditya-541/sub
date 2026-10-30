"""Validator fan-out to registered Factory miners and result validation."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

import httpx

from .network import BittensorNetwork, HotkeyAuth, NetworkConfigurationError, Peer
from .protocol import BuildContribution, BuildTask, ValidationReport
from .scoring import validate_contribution

logger = logging.getLogger(__name__)


async def dispatch_task(
    task: BuildTask,
    *,
    network: BittensorNetwork | None = None,
    max_peers: int = 32,
    on_peer_event: Callable[[Peer, str], Awaitable[None]] | None = None,
) -> tuple[list[BuildContribution], list[ValidationReport]]:
    """Ask eligible registered miners independently; failed peers produce no score."""
    chain = network or BittensorNetwork()
    local_mode = os.getenv("FACTORY_LOCAL_MODE") == "1"
    try:
        peers = await asyncio.to_thread(chain.peers)
        wallet = None if local_mode else chain.wallet
    except Exception as exc:
        raise NetworkConfigurationError(f"Could not discover Factory miners: {exc}") from exc
    if not peers:
        return [], []

    semaphore = asyncio.Semaphore(max_peers)

    async def query(peer):
        async with semaphore:
            status = "failed"

            async def emit_peer_event(event_status: str) -> None:
                if on_peer_event is None:
                    return
                try:
                    await on_peer_event(peer, event_status)
                except Exception as exc:  # noqa: BLE001 - progress events cannot fail dispatch
                    logger.warning(
                        "Factory task progress callback failed",
                        extra={"error_type": type(exc).__name__},
                    )

            await emit_peer_event("started")
            try:
                async with httpx.AsyncClient(
                    base_url=f"http://{peer.host}",
                    timeout=task.deadline_seconds,
                    follow_redirects=False,
                ) as client:
                    response = await client.post(
                        "/tasks/execute",
                        content=task.model_dump_json(),
                        headers={"content-type": "application/json"},
                        auth=None if local_mode else HotkeyAuth(wallet, receiver_ss58=peer.hotkey),
                    )
                    response.raise_for_status()
                contribution = BuildContribution.model_validate(response.json())
                contribution.miner_hotkey = peer.hotkey
                status = "returned"
                return contribution
            except Exception as exc:  # noqa: BLE001 - isolate one faulty miner from the fan-out.
                logger.warning(
                    "Factory miner request failed",
                    extra={"miner_hotkey": peer.hotkey, "error_type": type(exc).__name__},
                )
                return None
            finally:
                await emit_peer_event(status)

    results = await asyncio.gather(*(query(peer) for peer in peers))
    contributions = [result for result in results if result is not None]
    reports = [validate_contribution(task, result) for result in contributions]
    return contributions, reports
