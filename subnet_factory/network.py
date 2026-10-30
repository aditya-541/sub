"""Current Bittensor SDK adapter for metagraph reads and signed peer HTTP."""

from __future__ import annotations

import ipaddress
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class NetworkConfigurationError(RuntimeError):
    pass


def _bittensor() -> Any:
    try:
        import bittensor as bt
    except ImportError as exc:
        raise NetworkConfigurationError(
            "Install the pinned Bittensor SDK before enabling chain mode."
        ) from exc
    return bt


@dataclass(frozen=True)
class Peer:
    hotkey: str
    host: str
    validator_permit: bool


class BittensorNetwork:
    """Small lazy adapter; no financial operations are exposed by this class."""

    def __init__(self) -> None:
        self.local_mode = os.getenv("FACTORY_LOCAL_MODE") == "1"
        try:
            self.netuid = int(os.getenv("FACTORY_NETUID", "0" if self.local_mode else ""))
        except ValueError as exc:
            raise NetworkConfigurationError("FACTORY_NETUID must be configured.") from exc
        self.network = os.getenv("BT_NETWORK", "finney")
        self.wallet_name = os.getenv("BT_WALLET", "default")
        self.hotkey_name = os.getenv("BT_WALLET_HOTKEY", "default")
        self.wallet_path = os.getenv("BT_WALLET_PATH", str(Path.home() / ".bittensor" / "wallets"))
        self._cached_at = 0.0
        self._metagraph: Any = None
        self._subtensor: Any = None
        self._wallet: Any = None

    @property
    def wallet(self) -> Any:
        if self._wallet is None:
            bt = _bittensor()
            self._wallet = bt.Wallet(
                name=self.wallet_name, hotkey=self.hotkey_name, path=self.wallet_path
            )
        return self._wallet

    @property
    def hotkey_ss58(self) -> str:
        return self.wallet.hotkey.ss58_address

    def metagraph(self, *, refresh_seconds: int = 30) -> Any:
        if self._metagraph is not None and time.monotonic() - self._cached_at < refresh_seconds:
            return self._metagraph
        bt = _bittensor()
        if self._subtensor is None:
            self._subtensor = bt.Subtensor(network=self.network)
        self._metagraph = self._subtensor.subnets.metagraph(netuid=self.netuid)
        self._cached_at = time.monotonic()
        return self._metagraph

    def eligible_validator(self, hotkey: str) -> bool:
        neuron = self.metagraph().by_hotkey(hotkey)
        return neuron is not None and bool(neuron.validator_permit)

    def peers(self, *, miners_only: bool = True) -> list[Peer]:
        if self.local_mode:
            peers = []
            for index, raw_host in enumerate(os.getenv("FACTORY_LOCAL_PEERS", "").split(",")):
                host = raw_host.strip().removeprefix("http://").removeprefix("https://")
                if host and ":" in host:
                    peers.append(
                        Peer(hotkey=f"local-miner-{index}", host=host, validator_permit=False)
                    )
            return peers
        peers: list[Peer] = []
        for neuron in self.metagraph().neurons:
            axon = getattr(neuron, "axon", None)
            if isinstance(axon, str) and ":" in axon:
                endpoint, raw_port = axon.rsplit(":", 1)
                try:
                    port = int(raw_port)
                except ValueError:
                    continue
            else:
                endpoint = getattr(axon, "ip", None)
                port = getattr(axon, "port", None)
            if not endpoint or not port or endpoint == "0.0.0.0":
                continue
            try:
                parsed_ip = ipaddress.ip_address(str(endpoint))
                if not parsed_ip.is_global and not (
                    self.network == "local" and (parsed_ip.is_loopback or parsed_ip.is_private)
                ):
                    continue
            except ValueError:
                continue
            if miners_only and bool(getattr(neuron, "validator_permit", False)):
                continue
            address = getattr(neuron, "hotkey", None)
            if address:
                peers.append(
                    Peer(
                        hotkey=str(address),
                        host=f"{endpoint}:{int(port)}",
                        validator_permit=bool(getattr(neuron, "validator_permit", False)),
                    )
                )
        return peers


class HotkeyAuth(httpx.Auth):
    """Sign each outgoing request for its exact path, body, and receiving hotkey."""

    requires_request_body = True

    def __init__(self, wallet: Any, receiver_ss58: str) -> None:
        self.wallet = wallet
        self.receiver_ss58 = receiver_ss58

    def auth_flow(self, request: httpx.Request):
        bt = _bittensor()
        request.headers.update(
            bt.http_auth.sign(
                self.wallet,
                method=request.method,
                path=request.url.raw_path.decode(),
                body=request.content,
                receiver_ss58=self.receiver_ss58,
            )
        )
        yield request


def verify_peer_request(
    headers: Any,
    body: bytes,
    *,
    method: str,
    path: str,
    self_hotkey_ss58: str,
) -> str:
    bt = _bittensor()
    caller = bt.http_auth.verify(
        headers,
        body,
        method=method,
        path=path,
        self_hotkey_ss58=self_hotkey_ss58,
    )
    return caller.hotkey_ss58
