"""Which open postings are the same job, so that a job is shown once (the Repeats rule).

Two open postings are the same job when they have the same company and title, letters only, and name the
same place, unless one is a list's. A list names a place its own way ("Ontario, Canada" for the board's
"CAD Remote – ON"), so against a list's copy the place isn't compared; on a board, or on a company's two
boards, the same title in two places is two jobs. A list names the company and words the title its own way
too ("Acme Inc.", "Summer 2027 Software Engineer Intern"), so two postings from different sources that link
to the same job (job_link) are that job whatever they call it. Of a job's copies the board's own comes
first, then the first found; each later copy is a repeat of the first one the person is shown, from their
sources and left by their other rules. When that one closes or their rules hide it, the next is shown.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Iterator
from dataclasses import dataclass

from jobbot_engine.model import Posting, stamp
from jobbot_engine.sources.github_list import job_link, twin_key

_LIST = "github_list:"


def _letters(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


@dataclass(frozen=True)
class Copy:
    """What telling one copy of a job from another needs."""

    id: str
    source_id: str
    company: str
    title: str
    location: str
    first_seen: str
    links: tuple[str, ...] = ()  # the jobs its link and apply link name (job_link), if they name one

    @property
    def job(self) -> str:
        return twin_key(self.company, self.title)

    @property
    def listed(self) -> bool:
        return self.source_id.startswith(_LIST)

    @property
    def rank(self) -> tuple[bool, str, str]:
        return (self.listed, self.first_seen, self.id)

    def same_job(self, other: Copy) -> bool:
        if self.source_id != other.source_id and not set(self.links).isdisjoint(other.links):
            return True
        if self.job != other.job:
            return False
        if self.source_id != other.source_id and (self.listed or other.listed):
            return True
        return _letters(self.location) == _letters(other.location)


def links(*urls: str) -> tuple[str, ...]:
    """The jobs these links name (job_link), each once."""
    return tuple(dict.fromkeys(key for key in map(job_link, urls) if key is not None))


def copy_of(posting: Posting) -> Copy:
    return Copy(
        posting.id, posting.source_id, posting.company, posting.title, posting.location,
        stamp(posting.first_seen_at) or "", links(posting.url, posting.apply_url),
    )  # fmt: skip


class Copies:
    """The open postings, by job and by link: kept up to date as a run finds and closes postings."""

    def __init__(self, copies: Iterable[Copy] = ()) -> None:
        self._by_job: dict[str, dict[str, Copy]] = {}
        self._by_link: dict[str, dict[str, Copy]] = {}
        self._of: dict[str, Copy] = {}
        for found in copies:
            self._put(found)

    def _put(self, found: Copy) -> None:
        self._by_job.setdefault(found.job, {})[found.id] = found
        for link in found.links:
            self._by_link.setdefault(link, {})[found.id] = found
        self._of[found.id] = found

    def add(self, posting: Posting) -> None:
        """A posting as it now is: a closed one is no longer a copy of anything."""
        self.remove(posting.id)
        if posting.closed_at is None:
            self._put(copy_of(posting))

    def remove(self, posting_id: str) -> None:
        found = self._of.pop(posting_id, None)
        if found is None:
            return
        for index, key in ((self._by_job, found.job), *((self._by_link, link) for link in found.links)):
            del index[key][posting_id]
            if not index[key]:
                del index[key]

    def _near(self, me: Copy) -> Iterator[Copy]:
        """The other open postings with this one's company and title, or one of its links, each once."""
        near = dict(self._by_job.get(me.job, {}))
        for link in me.links:
            near.update(self._by_link.get(link, {}))
        return (other for other in near.values() if other.id != me.id)

    def others(self, posting_id: str) -> list[str]:
        """The other open postings with this posting's company and title or its link, whose repeat it may be
        or not."""
        me = self._of.get(posting_id)
        return [] if me is None else [other.id for other in self._near(me)]

    def _copies(self, me: Copy, sources: Collection[str]) -> list[Copy]:
        """This job's other copies on the person's sources, the one shown first first."""
        found = [other for other in self._near(me) if other.source_id in sources and me.same_job(other)]
        return sorted(found, key=lambda other: other.rank)

    def shown_instead(
        self, posting: Posting, sources: Collection[str], shown: Callable[[Copy], bool]
    ) -> str | None:
        """Where the job is shown instead, said for the person, when this posting is a repeat; else None.
        `shown` says whether the person's other rules leave a copy shown."""
        me = copy_of(posting)
        first = next((o for o in self._copies(me, sources) if o.rank < me.rank and shown(o)), None)
        if first is None:
            return None
        if first.source_id == me.source_id:
            return f"posted more than once by {posting.company}"
        if first.listed:
            return f"also on the {first.source_id.removeprefix(_LIST).title()} list"
        return f"also on {first.company}'s own board"

    def found_before(self, posting: Posting, sources: Collection[str], shown: Callable[[Copy], bool]) -> bool:
        """Whether a copy of the job found before this posting is shown to the person: it was news then."""
        me = copy_of(posting)
        return any(other.first_seen < me.first_seen and shown(other) for other in self._copies(me, sources))
