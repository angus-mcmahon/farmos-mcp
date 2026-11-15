"""Shared helpers for tool modules."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from ..client import FarmOSAPIError, FarmOSClient, Filter
from ..discovery import Discovery
from ..errors import FarmOSError
from ..summarize import included_index, summarize_resource


def to_epoch(value: str) -> str:
    """Convert an ISO-8601 timestamp or bare date to Unix epoch seconds (UTC).

    farmOS's log `timestamp` field is a `created`-type integer column; JSON:API
    filters on it must be epoch seconds — ISO strings cause HTTP 500s. Accepts
    '2024-05-01', '2024-05-01T10:30:00', or full RFC3339 strings.
    """
    value = value.strip()
    # Python 3.10's fromisoformat rejects the 'Z' suffix; normalize it.
    if value.upper().endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FarmOSError(
            f"Invalid timestamp '{value}': use ISO-8601 like 2024-05-01 or 2024-05-01T10:30:00Z"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp()))


async def fan_out_collections(
    client: FarmOSClient,
    discovery: Discovery,
    entity_type: str,
    *,
    filters: list[Filter] | None = None,
    includes: list[str] | None = None,
    sort: str | None = None,
    limit: int = 20,
    offset: int = 0,
    server_max_limit: int = 50,
) -> dict:
    """Query every bundle of an entity type and merge the results into one
    globally-ordered, globally-paginated window.

    JSON:API collections are per-bundle, and Drupal caps page[limit] per
    request (usually 50) — so a plain global offset cannot be forwarded to
    each bundle. Instead we over-fetch each bundle from its top (up to
    offset+limit records, capped by the server max), merge, sort globally per
    the requested sort, and slice out [offset, offset+limit). Bundles that
    error with 4xx (e.g. no permission) are skipped; 5xx errors fail loudly
    rather than faking "no results".
    """
    bundles = discovery.bundles_for(entity_type)
    if not bundles:
        raise FarmOSError(f"The server exposes no '{entity_type}' bundles")

    per_bundle_limit = min(offset + limit, server_max_limit)

    async def one(bundle: str):
        try:
            return await client.get_collection(
                entity_type,
                bundle,
                filters=filters,
                includes=includes,
                sort=[sort] if sort else None,
                limit=per_bundle_limit,
                offset=0,
            )
        except FarmOSAPIError as exc:
            if exc.status_code >= 500:
                raise  # server-side failure: fail loudly, never fake "no results"
            return None  # 4xx (e.g. no permission for this bundle): skip it
        except FarmOSError:
            return None

    results = await asyncio.gather(*(one(b) for b in bundles))
    docs = [r for r in results if r is not None]

    included: dict[str, dict] = {}
    for doc in docs:
        included.update(included_index(doc))

    merged: list[dict] = []
    for doc in docs:
        merged.extend(doc.get("data") or [])
    key, reverse = _sort_key_for(sort)
    merged.sort(key=key, reverse=reverse)

    items = merged[offset : offset + limit]
    out: dict = {"count": len(items), "data": [summarize_resource(r, included) for r in items]}
    if len(merged) > offset + len(items):
        out["next_offset"] = offset + len(items)
    return out


def _sort_key_for(sort: str | None):
    """Build a global merge sort from the requested per-bundle sort string."""
    field = (sort or "name").lstrip("-")
    reverse = (sort or "name").startswith("-")

    def key(resource: dict):
        attrs = resource.get("attributes") or {}
        value = attrs.get(field)
        if isinstance(value, str):
            return (0, value.lower(), resource.get("type") or "")
        return (1, "", resource.get("type") or "")

    return key, reverse
