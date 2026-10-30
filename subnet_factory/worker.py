"""Single-worker durable queue runner for Factory project builds."""

from __future__ import annotations

import asyncio
import logging

from .api import process_next_build_job
from .store import TaskStore

logger = logging.getLogger("subnet_factory.worker")


async def run_worker() -> None:
    store = TaskStore()
    recovered = await asyncio.to_thread(store.recover_running_build_jobs)
    if recovered:
        logger.warning("Requeued interrupted Factory builds", extra={"count": recovered})
    while True:
        if not await process_next_build_job():
            await asyncio.sleep(0.5)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("Factory build worker stopped")


if __name__ == "__main__":
    main()
