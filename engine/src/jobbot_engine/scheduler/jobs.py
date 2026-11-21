"""The job loop: the work a person waits for in the app, a preview of their rules or a check of a link.

Every 10 seconds it asks the app for the next job, runs it with a 30-second limit, and answers done with the
result or failed in plain words. One job failing never stops the loop.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as JobTimeout
from datetime import datetime
from typing import Any

from jobbot_engine.api_client.client import ApiClient, ApiError, ContractError
from jobbot_engine.scheduler import check_source, preview
from jobbot_engine.schema import validate
from jobbot_engine.store.db import Store

log = logging.getLogger(__name__)

TOO_LONG = {
    "check_source": "Checking that link took too long. Try again.",
    "preview_rules": "The preview took too long. Try again.",
}
WENT_WRONG = {"check_source": "Checking that link went wrong", "preview_rules": "The preview went wrong"}
UNKNOWN_PERSON = "jobbot doesn't have this person's settings yet. Try again in a few minutes."


class PlainError(Exception):
    """A job failed for a reason its message already says in plain words."""


def _person(store: Store, user_id: str) -> Mapping[str, Any]:
    config: Any = json.loads(store.get_meta("last_config") or "null")
    users: list[Mapping[str, Any]] = config["users"] if config else []
    for user in users:
        if user["user_id"] == user_id:
            return user
    raise PlainError(UNKNOWN_PERSON)


def _do(job: Mapping[str, Any], store: Store, http: Any, now: datetime) -> dict[str, Any]:
    user = _person(store, job["user_id"])
    if job["kind"] == "check_source":
        return check_source.run(job, http, user["settings"], now=now)
    follows = [follow["company"] for follow in user["follows"]]
    sources = {use["source_id"] for use in user["sources"] if not use["paused"]}
    return preview.run(job, store, user["settings"], follows, now, sources=sources)


def answer(
    job: Mapping[str, Any], store: Store, http: Any, now: Callable[[], datetime], timeout: float
) -> dict[str, Any]:
    """The answer to one job: done with its result, or failed in plain words."""
    kind = job["kind"]
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        result = pool.submit(_do, job, store, http, now()).result(timeout=timeout)
    except JobTimeout:
        return {"status": "failed", "result": None, "error": TOO_LONG[kind]}
    except PlainError as error:
        return {"status": "failed", "result": None, "error": str(error)[:500]}
    except Exception as error:  # one job failing never stops the loop
        log.exception("job %s failed", job["id"])
        message = f"{WENT_WRONG[kind]}: {type(error).__name__}: {error}"
        return {"status": "failed", "result": None, "error": message[:500]}
    finally:
        pool.shutdown(wait=False)
    errors = validate("job", {**job, "status": "done", "result": result})
    if errors:
        log.error("job %s result doesn't match the contract: %s", job["id"], errors[:5])
        return {"status": "failed", "result": None, "error": f"{WENT_WRONG[kind]}: its answer was malformed"}
    return {"status": "done", "result": result, "error": None}


def serve(
    api: ApiClient,
    store: Store,
    http: Any,
    now: Callable[[], datetime],
    poll_seconds: float = 10,
    stop: threading.Event | None = None,
    job_timeout: float = 30,
) -> None:
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            job = api.next_job()
        except (ApiError, ContractError) as error:
            log.warning("couldn't ask the app for a job: %s", error)
            stop.wait(poll_seconds)
            continue
        if job is None:
            stop.wait(poll_seconds)
            continue
        reply = answer(job, store, http, now, job_timeout)
        try:
            api.answer_job(job["id"], reply)
        except (ApiError, ContractError) as error:
            log.warning("couldn't answer job %s: %s", job["id"], error)
