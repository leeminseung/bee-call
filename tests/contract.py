"""The app contract's JSON Schemas (contract/schemas/), for checking every body the server answers."""

import functools
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMAS = Path(__file__).resolve().parent.parent / "contract" / "schemas"


@functools.cache
def _validators() -> dict[str, Draft202012Validator]:
    schemas = {p.stem: json.loads(p.read_text()) for p in sorted(SCHEMAS.glob("*.json"))}
    registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas.values())
    return {name: Draft202012Validator(schema, registry=registry) for name, schema in schemas.items()}


def check_body(name: str, body) -> None:
    """Fails the test with every way `body` breaks its schema: contract/schemas/<name>.json, or for an example's name
    (call-view-live) the schema whose name is the longest prefix of it, as contract/check.py pairs them."""
    validators = _validators()
    schema = max((s for s in validators if s != "defs" and (name == s or name.startswith(s + "-"))), key=len)
    errors = sorted(validators[schema].iter_errors(body), key=lambda e: list(e.absolute_path))
    if errors:
        lines = [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message[:300]}" for e in errors[:10]]
        raise AssertionError(f"not a valid {schema} body:\n" + "\n".join(lines))
