"""The one way the readers reach a board: short timeouts, one retry, a size cap, and errors in plain words."""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from jobbot_engine import __version__

USER_AGENT = f"jobbot-engine/{__version__} (private job search tool)"
TIMEOUT = (5, 20)  # connect, read
RETRY_AFTER = 1.5
CAP = 10 * 1024 * 1024
# A whole answer, however it trickles in: each read waits at most TIMEOUT's 20 seconds, but a server sending a
# byte every 19 seconds would otherwise hold a reading thread past the run's 15-minute lock. An answer allowed
# to be bigger than CAP (the GitHub lists, 40 MB) is given longer in proportion, so it needs the same speed.
DEADLINE = 60.0
_RETRIED = (429, 503)


class SourceError(Exception):
    """A source couldn't be read. The message says what failed, in plain words."""


class NotThere(SourceError):
    """The host answered 404: there's nothing at that address."""


class Refused(SourceError):
    """The host answered a 4xx other than 404 and 429: it won't let jobbot read that address."""


class TurnedAway(SourceError):
    """The host answered 429 twice: it's asking us to slow down. A run leaves it alone from here."""


class Waits(SourceError):
    """Not asked, and not a failure: whatever needed it waits for the next run, for the reason in `why`."""

    why = "it waits for the next run"


class HostResting(Waits):
    """The host turned this run away earlier."""

    why = "its host is turning requests away"


class OutOfTime(Waits):
    """The run's time for asking boards is over."""

    why = "this run's reading time ran out"


class Http:
    def __init__(
        self, sleep: Callable[[float], object] = time.sleep, cap: int = CAP, deadline: float = DEADLINE
    ) -> None:
        self._local = threading.local()
        # Every living thread's, to close them on purpose. Weak: a finished thread's goes with it, since the
        # job service answers each job on a new thread with one client, for weeks.
        self._sessions: weakref.WeakSet[requests.Session] = weakref.WeakSet()
        self._sessions_lock = threading.Lock()
        # Set by start_run, for a run only: the job service asks one link at a time, as the person asks.
        self._run = False
        self._resting: set[str] = (
            set()
        )  # hosts that turned this run away: asking again costs time and patience
        self._stop_at: float | None = None  # time.monotonic() after which this run asks nothing more
        self.sleep = sleep
        self.cap = cap
        self.deadline = deadline

    def start_run(self, stop_at: float) -> None:
        """From here, a host that turns a request away is left alone, and nothing is asked after `stop_at`."""
        self._run, self._stop_at = True, stop_at
        self._resting.clear()

    def stop_asking_at(self, stop_at: float) -> None:
        """Move the time this run asks nothing after, for its next part; the hosts resting stay resting."""
        self._stop_at = stop_at

    def _left(self, name: str) -> float:
        """Seconds the run has left to ask `name`; OutOfTime when none. Unlimited outside a run."""
        if self._stop_at is None:
            return float("inf")
        left = self._stop_at - time.monotonic()
        if left <= 0:
            raise OutOfTime(f"the run's time for asking ran out before {name} was asked")
        return left

    @property
    def session(self) -> requests.Session:
        """This thread's session: requests' sessions aren't safe to share between the run's threads."""
        session: requests.Session | None = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
            self._local.session = session
            with self._sessions_lock:
                self._sessions.add(session)
        return session

    def close(self) -> int:
        """Close every thread's connections, rather than leave them to garbage collection; how many sessions.
        A closed session still works: it opens new connections when asked again."""
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions = weakref.WeakSet()
        for session in sessions:
            session.close()
        self._local = threading.local()
        return len(sessions)

    def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        *,
        what: str | None = None,
        cap: int | None = None,
    ) -> Any:
        """The JSON at `url`. `cap` allows more than the usual 10 MB, for a source known to be big."""
        return self._json(self._fetch("GET", url, what, cap, params=params)[0], url, what)

    def post_json(self, url: str, payload: Any, *, what: str | None = None) -> Any:
        body, _ = self._fetch("POST", url, what, None, json=payload, headers={"Accept": "application/json"})
        return self._json(body, url, what)

    def get_text(self, url: str, *, what: str | None = None) -> str:
        return self.get_page(url, what=what)[1]

    def location(self, url: str, *, what: str | None = None) -> str:
        """Where `url` sends its visitors, read from one HEAD request and not followed; `url` itself when it
        sends them nowhere. For a short link, whose address says nothing about the job."""
        name = _name(url, what)
        host = urlparse(url).netloc.lower()
        if host in self._resting:
            raise HostResting(f"{host} turned this run away earlier, so {name} waits for the next run")
        left = self._left(name)
        try:
            response = self.session.head(
                url, allow_redirects=False, timeout=(min(TIMEOUT[0], left), min(TIMEOUT[1], left))
            )
        except (requests.ConnectionError, requests.Timeout) as error:
            raise SourceError(f"{name} couldn't be reached: {_reason(error)}") from None
        with response:
            if response.status_code == 404:
                raise NotThere(f"{name} answered 404: it doesn't exist")
            if response.status_code == 429:
                if self._run:
                    self._resting.add(host)
                raise TurnedAway(f"{name} answered 429: it's turning requests away")
            if 400 <= response.status_code < 500:
                raise Refused(f"{name} answered {response.status_code}")
            if response.status_code >= 400:
                raise SourceError(f"{name} answered {response.status_code}")
            target = response.headers.get("Location")
            return urljoin(url, target) if response.is_redirect and target else url

    def get_page(self, url: str, *, what: str | None = None) -> tuple[str, str]:
        """Where `url` leads after its redirects, and the page there."""
        body, final = self._fetch("GET", url, what, None)
        return final, body.decode("utf-8", errors="replace")

    def _json(self, body: bytes, url: str, what: str | None) -> Any:
        try:
            return json.loads(body)
        except ValueError:
            raise SourceError(f"{_name(url, what)} answered something that isn't JSON") from None

    def _fetch(
        self, method: str, url: str, what: str | None, cap: int | None, **kwargs: Any
    ) -> tuple[bytes, str]:
        """The body and the address it came from (after redirects), after at most one retry for a dropped
        connection or a board asking us to slow down."""
        name = _name(url, what)
        host = urlparse(url).netloc.lower()
        if host in self._resting:
            raise HostResting(f"{host} turned this run away earlier, so {name} waits for the next run")
        for attempt in (1, 2):
            left = self._left(name)
            timeout = (min(TIMEOUT[0], left), min(TIMEOUT[1], left))
            try:
                response = self.session.request(method, url, timeout=timeout, stream=True, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as error:
                if attempt == 1:
                    self.sleep(RETRY_AFTER)
                    continue
                raise SourceError(f"{name} couldn't be reached: {_reason(error)}") from None
            with response:
                if response.status_code in _RETRIED and attempt == 1:
                    self.sleep(RETRY_AFTER)
                    continue
                if response.status_code == 404:
                    raise NotThere(f"{name} answered 404: it doesn't exist")
                if response.status_code == 429:
                    if self._run:  # the host that said so, wherever a redirect led
                        self._resting.add(urlparse(response.url or url).netloc.lower())
                    raise TurnedAway(f"{name} answered 429 twice: it's turning requests away")
                if response.status_code in _RETRIED:
                    raise SourceError(f"{name} answered {response.status_code} twice: it's unavailable")
                if 400 <= response.status_code < 500:
                    raise Refused(f"{name} answered {response.status_code}")
                if response.status_code >= 400:
                    raise SourceError(f"{name} answered {response.status_code}")
                allowed = cap or self.cap
                limit = self.deadline * max(1.0, allowed / self.cap)
                body = self._read(response, name, allowed, limit, min(limit, self._left(name)))
                return body, response.url or url
        raise AssertionError("unreachable: the second attempt always returns or raises")

    def _read(self, response: requests.Response, name: str, cap: int, limit: float, deadline: float) -> bytes:
        body = bytearray()
        began = time.monotonic()
        # A read blocks until its chunk fills or 20 seconds pass without a byte, so the deadline is kept by
        # a watchdog that shuts the connection, which ends the read at once.
        cut = threading.Event()
        watchdog = threading.Timer(deadline, _cut, (response, cut))
        watchdog.daemon = True
        watchdog.start()
        try:
            for chunk in response.iter_content(chunk_size=65536):
                body.extend(chunk)
                if len(body) > cap:
                    raise SourceError(f"{name} answered more than {cap:,} bytes, so it was refused")
        except (requests.RequestException, OSError, ValueError, AttributeError):  # how a shut read ends
            if not cut.is_set():
                raise
        finally:
            watchdog.cancel()
        if cut.is_set():
            # The run's time ran out first. An answer given longer than a board's (a list) is always cut by
            # the run's clock: past a board's time and behind the pace its size needs, it's too slow.
            took = time.monotonic() - began
            if deadline < limit and (took < self.deadline or len(body) / took >= cap / limit):
                raise OutOfTime(f"the run's time for asking ran out while {name} was answering")
            if deadline < limit:
                raise SourceError(
                    f"{name} was answering too slowly to finish in {limit:g} seconds, so it was given up on"
                )
            raise SourceError(f"{name} took longer than {limit:g} seconds to answer, so it was given up on")
        return bytes(body)


def _name(url: str, what: str | None) -> str:
    return what or urlparse(url).netloc


def _reason(error: requests.RequestException) -> str:
    return "it timed out" if isinstance(error, requests.Timeout) else "the connection failed"


def _cut(response: requests.Response, cut: threading.Event) -> None:
    """Shut the answer's connection, which wakes a read blocked on it."""
    cut.set()
    # The socket under the answer being read: http.client's file over it (the connection let go of it).
    reading = getattr(getattr(getattr(response.raw, "_fp", None), "fp", None), "raw", None)
    sock = getattr(reading, "_sock", None)
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(Exception):
        response.close()
