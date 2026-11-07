"""The shared schema contract: every record the engine and the app exchange.

The schema files live in the repo's schema/ folder. The app checks the same files the same way
(app/src/shared/schema.ts), so a record one side accepts, the other accepts too.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from jobbot_engine import __version__

SCHEMA_NAMES: tuple[str, ...] = (
    "source",
    "user-source",
    "follow",
    "posting",
    "decision",
    "run-summary",
    "coverage-report",
    "settings",
    "job",
    "engine-config",
    "postings-batch",
    "sources-add",
    "next-job",
    "job-answer",
    "sign-in",
    "posting-action",
    "source-change",
    "job-request",
    "push-subscription",
    "source-request",
    "profile",
    "person-add",
    "person-limits",
)
_BASE = "https://jobbot.invalid/schema/"


def _schema_dir() -> Path:
    """The schema files: packaged with the engine when installed, or the repo's schema/ in development."""
    packaged = resources.files("jobbot_engine") / "schema"
    if packaged.is_dir():
        return Path(str(packaged))
    return Path(__file__).resolve().parents[3] / "schema"


def data_dir() -> Path:
    """The data both sides read besides the schemas: the rules' catalogue and the places jobbot recognises."""
    return _schema_dir() / "data"


@cache
def contract_digest() -> str:
    """A sha256 over every schema and data file and the engine's version: what was checked under one
    contract, or by one release's checker, isn't trusted under another."""
    digest = hashlib.sha256(__version__.encode() + b"\0")
    for path in sorted([*_schema_dir().glob("*.json"), *data_dir().glob("*.json")]):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


@cache
def _registry() -> Registry[Any]:
    loaded: list[tuple[str, Resource[Any]]] = []
    for path in sorted(_schema_dir().glob("*.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        loaded.append((contents["$id"], Resource.from_contents(contents)))
    return Registry[Any]().with_resources(loaded)


@cache
def _validator(name: str) -> Any:
    """A Draft 2020-12 validator for one schema, with formats (date-time, uri, uuid) checked.

    jsonschema skips "format" unless it's given a format checker, which would let "yesterday" pass as a
    timestamp here while the app refuses it. The schema's references are put in place once, here, since
    looking each one up as a record is checked was most of what checking cost. Typed Any because
    jsonschema's own validator types are partial.
    """
    uri = f"{_BASE}{name}.json"
    schema = inline(_registry().contents(uri), _registry().resolver(uri))
    return Draft202012Validator(
        schema, registry=_registry(), format_checker=Draft202012Validator.FORMAT_CHECKER
    )


# Keywords whose values are data, not schemas, so a "$ref" inside them is left alone.
_DATA = frozenset({"enum", "const", "examples", "default"})


def inline(node: Any, resolver: Any, within: tuple[int, ...] = ()) -> Any:
    """`node` with each "$ref" replaced by what it points to, beside the reference's other keywords.

    `resolver` is the referencing library's, for the file `node` is in. The contract has no recursive
    schema; one would be refused here rather than looping.
    """
    if isinstance(node, list):
        return [inline(item, resolver, within) for item in cast(list[Any], node)]
    if not isinstance(node, dict):
        return node
    schema = cast(dict[str, Any], node)
    rest = {
        key: value if key in _DATA else inline(value, resolver, within)
        for key, value in schema.items()
        if key != "$ref"
    }
    if "$ref" not in schema:
        return rest
    found = resolver.lookup(schema["$ref"])
    if id(found.contents) in within:
        raise ValueError(f"the schema refers to itself through {schema['$ref']}, which can't be put in place")
    target = inline(found.contents, found.resolver, (*within, id(found.contents)))
    if isinstance(target, dict):
        target = {k: v for k, v in cast(dict[str, Any], target).items() if k not in ("$id", "$schema")}
    return {"allOf": [target], **rest} if rest else target


def validate(name: str, record: object) -> list[str]:
    """Every way `record` breaks schema `name`, as "<json pointer>: <reason>". Empty means valid."""
    if name not in SCHEMA_NAMES:
        raise ValueError(f"no schema named {name!r}; the schemas are {', '.join(SCHEMA_NAMES)}")
    errors: list[ValidationError] = list(_validator(name).iter_errors(record))
    leaves = [leaf for error in errors for leaf in _leaves(error)]
    return [f"{_pointer(error)}: {error.message}" for error in sorted(leaves, key=_pointer)]


def _leaves(error: ValidationError) -> Iterator[ValidationError]:
    """The specific failures under a oneOf or anyOf, rather than its summary "not valid under any"."""
    if error.context:
        for sub in error.context:
            yield from _leaves(sub)
    else:
        yield error


def _pointer(error: ValidationError) -> str:
    return "/" + "/".join(str(part) for part in error.absolute_path)
