"""The only way the engine talks to the app: the routes in docs/engine-routes.md, each body checked."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

import requests

from jobbot_engine.schema import contract_digest, validate
from jobbot_engine.store.db import Store
from jobbot_engine.store.outbox import OutboxBatch

TIMEOUT = 15
# The state is some 12 MB gzipped.
STATE_TIMEOUT = 120


class ApiError(Exception):
    """The app couldn't be reached, or answered with something other than success. Said in plain words.
    `status` is the app's answer, or None when it couldn't be reached."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ContractError(Exception):
    """A body that doesn't match its schema, going out or coming in. Nothing invalid is sent or used."""

    def __init__(self, schema: str, errors: list[str]) -> None:
        super().__init__(f"A {schema} body doesn't match the contract: {'; '.join(errors[:5])}")
        self.schema = schema
        self.errors = errors


def _checked(schema: str, body: Any) -> Any:
    errors = validate(schema, body)
    if errors:
        raise ContractError(schema, errors)
    return body


class ApiClient:
    def __init__(self, base_url: str, token: str, session: requests.Session | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        # What the last config answered identifies as, under this contract (engine_config).
        self.config_digest: str | None = None

    def _answer(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = TIMEOUT,
    ) -> requests.Response:
        try:
            answer = self.session.request(
                method, self.base_url + path, json=body, data=data, headers=headers, timeout=timeout
            )
        except requests.RequestException as error:
            raise ApiError(f"The app couldn't be reached: {type(error).__name__}") from None
        if not 200 <= answer.status_code < 300:
            said = answer.text.strip()[:200]
            message = f"The app answered {answer.status_code} to {method} {path}"
            raise ApiError(f"{message}: {said}" if said else message, answer.status_code)
        return answer

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        answer = self._answer(method, path, body)
        if answer.status_code == 204 or not answer.content:
            return None
        try:
            return answer.json()
        except ValueError:
            raise ApiError(f"The app answered {method} {path} with something that isn't JSON") from None

    def engine_config(self, checked: str | None = None) -> dict[str, Any]:
        """The config, checked against the contract unless it's byte for byte the one whose digest is
        `checked`, which was checked when it came. Its own digest is left in `config_digest`."""
        answer = self._answer("GET", "/engine/config")
        digest = hashlib.sha256(contract_digest().encode() + answer.content).hexdigest()
        try:
            config = answer.json()
        except ValueError:
            raise ApiError("The app answered GET /engine/config with something that isn't JSON") from None
        self.config_digest = None
        if digest != checked:
            _checked("engine-config", config)
        self.config_digest = digest
        result: dict[str, Any] = config
        return result

    def send_postings(self, batch: dict[str, Any], *, checked: bool = False) -> None:
        self._call("POST", "/engine/postings", batch if checked else _checked("postings-batch", batch))

    def send_run(self, summary: dict[str, Any], *, checked: bool = False) -> None:
        self._call("POST", "/engine/runs", summary if checked else _checked("run-summary", summary))

    def send_coverage(self, report: dict[str, Any], *, checked: bool = False) -> None:
        self._call("POST", "/engine/coverage", report if checked else _checked("coverage-report", report))

    def add_sources(self, payload: dict[str, Any], *, checked: bool = False) -> None:
        self._call("POST", "/engine/sources", payload if checked else _checked("sources-add", payload))

    def next_job(self) -> dict[str, Any] | None:
        job: dict[str, Any] | None = _checked("next-job", self._call("GET", "/engine/jobs/next"))["job"]
        return job

    def answer_job(self, job_id: str, answer: dict[str, Any]) -> None:
        self._call("POST", f"/engine/jobs/{job_id}", _checked("job-answer", answer))

    def next_resume(self) -> dict[str, Any] | None:
        """The oldest resume the app has queued, with its posting and the person's kit, or None."""
        work: dict[str, Any] | None = _checked("resume-job", self._call("GET", "/engine/resumes/next"))[
            "resume"
        ]
        return work

    def answer_resume(self, resume_id: str, answer: dict[str, Any]) -> None:
        self._call("POST", f"/engine/resumes/{resume_id}", _checked("resume-answer", answer))

    # The engine's own state and log, kept by the app between runs on GitHub's runners. They're the engine's
    # alone, gzipped, so no schema checks them.

    def fetch_state(self) -> tuple[bytes, str] | None:
        """The state the last run kept and its version (ETag), or None before the first run kept any."""
        try:
            answer = self._answer("GET", "/engine/state", timeout=STATE_TIMEOUT)
        except ApiError as error:
            if error.status == 404:
                return None
            raise
        return answer.content, answer.headers.get("ETag", "")

    def keep_state(self, data: bytes, fetched: str | None) -> None:
        """Keep the state over the version this run fetched (None: there was none). An ApiError with status
        409 means another run kept its state first."""
        condition = {"If-Match": fetched} if fetched else {"If-None-Match": "*"}
        self._answer("PUT", "/engine/state", data=data, headers=condition, timeout=STATE_TIMEOUT)

    def send_log(self, run: str, data: bytes) -> None:
        self._answer("PUT", f"/engine/logs/{run}", data=data, timeout=STATE_TIMEOUT)


@dataclass(frozen=True)
class FlushResult:
    sent: int
    error: str | None
    # Records the app refused that were set aside so the rest still goes (Store.set_aside_news names them).
    set_aside: int = 0
    # The app answered, but refused every record sent: they wait, and it's no sign the app is down.
    refused: bool = False


# How many extra requests one flush may spend halving refused batches to find the records refused.
SIFT_BUDGET = 30
OUT_OF_TIME = "this run's time ran out, and the rest is sent first next run"


def _send(api: ApiClient, batch: OutboxBatch) -> None:
    """Send a batch of the outbox. Its records were checked as they were queued (Store.enqueue), so it's
    checked again only when one was queued under another contract, by an earlier release."""
    checked = batch.checked
    if batch.kind == "postings":
        api.send_postings(batch.body, checked=checked)
    elif batch.kind == "run":
        api.send_run(batch.body, checked=checked)
    elif batch.kind == "coverage":
        api.send_coverage(batch.body, checked=checked)
    else:
        api.add_sources(batch.body, checked=checked)


@dataclass
class _Sifting:
    store: Store
    api: ApiClient
    until: float | None = None
    budget: int = SIFT_BUDGET
    sent: int = 0
    # (the records, the app's reason): a single record is one proven refused; more ran out of budget.
    refused: list[tuple[tuple[int, ...], str]] = field(default_factory=lambda: [])

    def check_time(self) -> None:
        if self.until is not None and time.monotonic() >= self.until:
            raise _OutOfTime

    def send(self, batch: OutboxBatch) -> None:
        """Send a batch; refused, halve it until the refused records are found. An app that's down raises, and
        so does reaching `until` before a smaller batch."""
        try:
            _send(self.api, batch)
        except ApiError as error:
            if error.status != 400:
                raise
            reason = str(error)
        except ContractError as error:
            reason = str(error)
        else:
            self.store.ack(batch.seqs)
            self.sent += len(batch.seqs)
            return
        if len(batch.seqs) == 1 or self.budget <= 0:
            self.refused.append((batch.seqs, reason))
            return
        half = len(batch.seqs) // 2
        for part in (batch.seqs[:half], batch.seqs[half:]):
            for smaller in self.store.outbox_batches(part):
                self.check_time()
                self.budget -= 1
                self.send(smaller)


class _OutOfTime(Exception):
    pass


def flush(
    store: Store, api: ApiClient, until: float | None = None, seqs: Collection[int] | None = None
) -> FlushResult:
    """Send the outbox batch by batch, in order, acknowledging each that arrives; or just the records `seqs`
    names.

    An app that's down or busy stops it, and so does reaching `until` (a time.monotonic() reading): what isn't
    sent stays, in order, for the next run. A record the app refuses would be refused every run and hold back
    everything behind it, so its batch is halved until it's found, and it's set aside (kept 30 days, with the
    reason). Only when the app took something else in the same flush, though: an app refusing everything is
    most likely a release behind the engine, and then everything waits for it.
    """
    sifting = _Sifting(store, api, until)
    try:
        for batch in store.outbox_batches(seqs):
            sifting.check_time()
            sifting.send(batch)
    except _OutOfTime:
        return FlushResult(sifting.sent, OUT_OF_TIME, _set_aside(sifting))
    except ApiError as error:
        return FlushResult(sifting.sent, str(error), _set_aside(sifting))
    refused = sum(len(seqs) for seqs, _reason in sifting.refused)
    if refused and not sifting.sent:
        _seqs, reason = sifting.refused[0]
        noun = "the 1 record" if refused == 1 else f"all {refused} records"
        return FlushResult(
            0, f"The app refused {noun} sent, so they wait for the next run: {reason}", refused=True
        )
    return FlushResult(sifting.sent, None, _set_aside(sifting))


def _set_aside(sifting: _Sifting) -> int:
    """Set aside the records proven refused, when the app took something else; the rest wait."""
    if not sifting.sent:
        return 0
    count = 0
    for seqs, reason in sifting.refused:
        if len(seqs) == 1:
            sifting.store.set_aside(seqs, reason)
            count += 1
    return count
