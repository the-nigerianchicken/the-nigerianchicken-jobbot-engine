"""A run on GitHub's runners (reset item 12), where nothing is left between runs: the state the last run kept
comes from the app first, jobs are answered while the run goes, and the state goes back to the app after,
kept only over the state this run fetched, so a run that ends late can't undo a newer one.

Its exit says how it went, and the public run's page shows only that (infra/actions/engine.yml):
0 the run finished and its state was kept, 1 it couldn't start, 2 there was no state to start from, 3 its
state wasn't kept.
"""

from __future__ import annotations

import gzip
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from jobbot_engine.api_client.client import ApiClient, ApiError
from jobbot_engine.scheduler.jobs import serve
from jobbot_engine.scheduler.run import run_once
from jobbot_engine.sources.http import Http
from jobbot_engine.store.db import Store

log = logging.getLogger(__name__)

FINISHED, NOT_STARTED, NO_STATE, NOT_KEPT = 0, 1, 2, 3
# A job being answered as the run ends still gets its 30 seconds.
JOBS_GRACE_SECONDS = 35


def run(new_api: Callable[[], ApiClient], path: Path, now: Callable[[], datetime]) -> int:
    api = new_api()
    try:
        fetched = api.fetch_state()
    except ApiError as error:
        log.error("the engine's state couldn't be fetched, so there's no run: %s", error)
        return NO_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    for stale in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        stale.unlink(missing_ok=True)
    if fetched is not None:
        path.write_bytes(gzip.decompress(fetched[0]))
    store = Store(path)
    stop = threading.Event()
    # The jobs get their own client: one requests session isn't shared between threads.
    jobs = threading.Thread(target=serve, args=(new_api(), store, Http(), now), kwargs={"stop": stop})
    jobs.start()
    try:
        summary = run_once(api, store, Http(), now)
    finally:
        stop.set()
        jobs.join(JOBS_GRACE_SECONDS)
        store.pragma("wal_checkpoint(TRUNCATE)")
        store.close()
    try:
        api.keep_state(gzip.compress(path.read_bytes(), compresslevel=6), fetched[1] if fetched else None)
    except ApiError as error:
        log.error("this run's state wasn't kept: %s", error)
        return NOT_KEPT
    return FINISHED if summary is not None else NOT_STARTED
