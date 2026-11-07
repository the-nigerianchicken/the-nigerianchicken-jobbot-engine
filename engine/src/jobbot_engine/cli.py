"""The engine's command line, run by the server's timers and services, or by GitHub's runners.

    jobbot-engine run                  one run: check the sources that are due, judge, send, report
    jobbot-engine jobs                 answer previews and source checks as the app queues them
    jobbot-engine check-source URL     print what a link is, as the app's source check would
    jobbot-engine run-on-github        one run, its state fetched from the app first and kept there after,
                                       answering jobs while it goes (scheduler/on_github.py)
    jobbot-engine send-log FILE        send a run's log to the app, out of the public run's page

The environment gives JOBBOT_API_URL, JOBBOT_ENGINE_TOKEN (or JOBBOT_ENGINE_TOKEN_FILE) and JOBBOT_STATE_DIR
(by default /var/lib/jobbot-engine). A run that checked its sources exits 0 even when some failed: the
summary says so. It exits 1 only when it couldn't start.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jobbot_engine.api_client.client import ApiClient, ApiError
from jobbot_engine.scheduler import check_source, on_github
from jobbot_engine.scheduler.jobs import serve
from jobbot_engine.scheduler.run import run_once
from jobbot_engine.sources.http import Http
from jobbot_engine.store.db import Store

DEFAULT_STATE_DIR = "/var/lib/jobbot-engine"


class Unconfigured(Exception):
    """Something the engine needs from its environment is missing."""


def _clock() -> datetime:
    return datetime.now(UTC)


def _need(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise Unconfigured(
            f"{name} isn't set, so the engine doesn't know where the app is or how to reach it"
        )
    return value


def _state_dir() -> Path:
    return Path(os.environ.get("JOBBOT_STATE_DIR") or DEFAULT_STATE_DIR)


def _store() -> Store:
    return Store(_state_dir() / "state.db")


def _api() -> ApiClient:
    url = _need("JOBBOT_API_URL")
    token = os.environ.get("JOBBOT_ENGINE_TOKEN")
    if token:
        return ApiClient(url, token)
    token_file = Path(_need("JOBBOT_ENGINE_TOKEN_FILE"))
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise Unconfigured(f"The engine token couldn't be read from {token_file}: {error.strerror}") from None
    return ApiClient(url, token)


def _send_log(api: ApiClient, log: Path) -> int:
    """Send the log gzipped under GitHub's run number (and attempt, after the first). Prints nothing."""
    run = os.environ.get("GITHUB_RUN_ID") or "local"
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT") or "1"
    try:
        api.send_log(run if attempt == "1" else f"{run}-{attempt}", gzip.compress(log.read_bytes()))
    except (OSError, ApiError) as error:
        logging.getLogger(__name__).error("the log wasn't sent: %s", error)
        return 1
    return 0


def _first_settings(store: Store) -> Any:
    """The first person's settings from the last config a run saved, or None before any run."""
    config: Any = json.loads(store.get_meta("last_config") or "null")
    users: list[Any] = config["users"] if config else []
    return users[0]["settings"] if users else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobbot-engine", description="jobbot's engine")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="check the sources that are due, judge what changed, and report")
    commands.add_parser("jobs", help="answer previews and source checks as the app queues them")
    check = commands.add_parser("check-source", help="print what a link is, as the source check would")
    check.add_argument("url")
    commands.add_parser("run-on-github", help="one run, with its state fetched from the app and kept there")
    send_log = commands.add_parser("send-log", help="send a run's log to the app, under GitHub's run number")
    send_log.add_argument("log")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "check-source":
            store = _store()
            job = {"input": {"url": args.url}}
            print(json.dumps(check_source.run(job, Http(), _first_settings(store)), indent=2))
            return 0
        if args.command == "run-on-github":
            _api()
            return on_github.run(_api, _state_dir() / "state.db", _clock)
        if args.command == "send-log":
            return _send_log(_api(), Path(args.log))
        api, store = _api(), _store()
    except Unconfigured as error:
        print(str(error), file=sys.stderr)
        return 1
    if args.command == "jobs":
        serve(api, store, Http(), _clock)
        return 0
    return 0 if run_once(api, store, Http(), _clock) is not None else 1


if __name__ == "__main__":
    sys.exit(main())
