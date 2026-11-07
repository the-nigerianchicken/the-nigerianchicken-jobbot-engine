"""The source readers: one module per board system, all with the same interface."""

from jobbot_engine.model import SourceKind
from jobbot_engine.sources.amazon import Amazon
from jobbot_engine.sources.apple import Apple
from jobbot_engine.sources.ashby import Ashby
from jobbot_engine.sources.base import Detail, Reader, to_posting
from jobbot_engine.sources.github_list import GithubList
from jobbot_engine.sources.google import Google
from jobbot_engine.sources.greenhouse import Greenhouse
from jobbot_engine.sources.http import Http, SourceError
from jobbot_engine.sources.jobbank import JobBank
from jobbot_engine.sources.lever import Lever
from jobbot_engine.sources.microsoft import Microsoft
from jobbot_engine.sources.netflix import Netflix
from jobbot_engine.sources.shopify import Shopify
from jobbot_engine.sources.uber import Uber
from jobbot_engine.sources.workday import Workday

_ALL: tuple[Reader, ...] = (
    Greenhouse(),
    Lever(),
    Ashby(),
    Workday(),
    Amazon(),
    Apple(),
    Google(),
    Microsoft(),
    Shopify(),
    Uber(),
    Netflix(),
    GithubList(),
    JobBank(),
)
READERS: dict[SourceKind, Reader] = {reader.kind: reader for reader in _ALL}

__all__ = ["READERS", "Detail", "Http", "Reader", "SourceError", "to_posting"]
