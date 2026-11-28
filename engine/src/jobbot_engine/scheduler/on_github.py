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


def take(api: ApiClient, path: Path) -> int:
    """Put the state GitHub's last run kept at `path`, once, when an instance moves to its server (production
    on 2026-11-21: Workday turns GitHub's runners away). What was at `path` is set aside as
    `<name>.before-take`, with its WAL; from here the server keeps its own state and gives none back. With no
    state kept, or none to be had, nothing here changes: 2, as a run with no state says. It happens once: run
    again, it would put the app's old copy over what the server has found since, so it refuses."""
    aside = path.with_name(path.name + ".before-take")
    if aside.exists() or path.with_name(path.name + ".taken").exists():
        log.error("the state was already taken once (%s), so nothing changed", path.parent)
        return NO_STATE
    try:
        fetched = api.fetch_state()
    except ApiError as error:
        log.error("the engine's state couldn't be fetched, so nothing changed: %s", error)
        return NO_STATE
    if fetched is None:
        log.error("the app keeps no state yet, so nothing changed")
        return NO_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    incoming = path.with_name(path.name + ".taking")
    incoming.write_bytes(gzip.decompress(fetched[0]))
    for suffix in ("", "-wal", "-shm"):
        old = path.with_name(path.name + suffix)
        if old.exists():
            old.replace(aside.with_name(aside.name + suffix))
    incoming.replace(path)
    path.with_name(path.name + ".taken").touch()
    log.info("took the state the app kept (%d bytes gzipped)", len(fetched[0]))
    return FINISHED


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
