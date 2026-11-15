"""Discovers server capabilities from the root /api document.

The root document carries meta (farm name, version, system of measurement)
plus a links map of every exposed resource type — used to validate the
type/bundle names agents pass in, with helpful close-match suggestions.

Also caches per-bundle field metadata (attribute + relationship names) from
the jsonapi_schema resource schemas, powering field-level error hints.
"""

from __future__ import annotations

import asyncio
import difflib
from dataclasses import dataclass, field


def _field_sort(name: str) -> tuple[int, str]:
    """Domain fields first; drupal_internal__* machinery sorted last."""
    return (name.startswith("drupal_internal__"), name)


def _schema_fields(doc: dict) -> tuple[list[str], list[str]]:
    """Extract (attribute, relationship) field names from a resource schema doc.

    Live shape (jsonapi_schema contrib): definitions.attributes.properties and
    definitions.relationships.properties map field name -> property schema.
    Unexpected shapes degrade to empty lists (hints are best-effort).
    """
    defs = doc.get("definitions") or {}

    def names(node: object) -> list[str]:
        if not isinstance(node, dict):
            return []
        props = node.get("properties")
        if not isinstance(props, dict):
            return []
        return sorted((str(key) for key in props), key=_field_sort)

    return names(defs.get("attributes")), names(defs.get("relationships"))

from .client import FarmOSClient
from .errors import FarmOSAPIError, FarmOSError


@dataclass
class Discovery:
    """Cached view of the farmOS server's resource-type catalog."""

    farm_name: str | None = None
    version: str | None = None
    system_of_measurement: str | None = None
    me_href: str | None = None
    types: dict[str, str] = field(default_factory=dict)  # e.g. "log--activity" -> href
    _fields: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # full_type -> field metadata
    _schema_tried: set[str] = field(default_factory=set)  # full_types whose schema was attempted (incl. failures)
    _loaded: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def ensure(self, client: FarmOSClient) -> "Discovery":
        """Fetch and cache GET /api once (farm meta + resource types)."""
        if self._loaded:
            return self
        async with self._lock:
            if self._loaded:
                return self
            doc = await client.request("GET", "/api")
            meta = doc.get("meta") or {}
            farm = meta.get("farm") or {}
            self.farm_name = farm.get("name")
            self.version = farm.get("version")
            self.system_of_measurement = farm.get("system_of_measurement")
            self.me_href = ((meta.get("links") or {}).get("me") or {}).get("href")
            for key, link in (doc.get("links") or {}).items():
                if isinstance(link, dict) and link.get("href") and "--" in key:
                    self.types[key] = str(link["href"])
            self._loaded = True
            return self

    def bundles_for(self, entity_type: str) -> list[str]:
        """All bundles exposed for an entity type, e.g. asset -> [animal, land, plant, ...]."""
        prefix = f"{entity_type}--"
        return sorted(key.split("--", 1)[1] for key in self.types if key.startswith(prefix))

    def entity_types(self) -> list[str]:
        """Distinct entity types, e.g. [asset, log, taxonomy_term, ...]."""
        return sorted({key.split("--", 1)[0] for key in self.types})

    def validate_type(self, full_type: str) -> tuple[str, str]:
        """Validate an 'asset--plant' style type; return (entity_type, bundle)."""
        if full_type in self.types:
            entity_type, bundle = full_type.split("--", 1)
            return entity_type, bundle
        if "--" not in full_type:
            raise FarmOSError(
                f"Type '{full_type}' must look like '<entity>--<bundle>' (e.g. 'asset--plant'). "
                f"Entity types on this server: {', '.join(self.entity_types())}"
            )
        entity_type = full_type.split("--", 1)[0]
        hint: str | None = None
        bundles = self.bundles_for(entity_type)
        if bundles:
            hint = f"Known '{entity_type}' bundles: {', '.join(bundles)}"
        else:
            hint = f"Entity types on this server: {', '.join(self.entity_types())}"
        suggestions = difflib.get_close_matches(full_type, sorted(self.types), n=4, cutoff=0.4)
        if suggestions:
            hint = f"Did you mean: {', '.join(suggestions)}? {hint}"
        raise FarmOSError(f"Unknown resource type '{full_type}'. {hint}")

    # -- schema-driven field metadata (M3.4) ----------------------------------

    async def ensure_schema(self, client: FarmOSClient, full_type: str) -> None:
        """Fetch + cache the JSON:API field schema for one resource bundle.

        Validates full_type first (unknown types raise, same as validate_type).
        The field definitions live at <collection href>/resource/schema
        (jsonapi_schema contrib, verified live). Best-effort by design: any
        client HTTP error on the schema fetch caches EMPTY metadata and never
        raises — field hints are a bonus, never a hard dependency.
        """
        await self.ensure(client)
        entity, bundle = self.validate_type(full_type)
        if full_type in self._schema_tried:
            return
        async with self._lock:
            if full_type in self._schema_tried:
                return
            href = self.types.get(full_type, f"/api/{entity}/{bundle}")
            path = f"{href.rstrip('/')}/resource/schema"
            self._schema_tried.add(full_type)  # recorded before fetch: failures stay cached
            try:
                doc = await client.request("GET", path)
                attrs, rels = _schema_fields(doc)
                self._fields[full_type] = {"attributes": attrs, "relationships": rels}
            except FarmOSAPIError:
                pass  # no metadata: fields_for returns empty lists for this type

    def fields_for(self, full_type: str) -> dict:
        """Cached field metadata WITHOUT fetching: {"attributes": [...], "relationships": [...]}.

        Empty lists when the type is unknown or its schema was never fetched
        (or failed). Lists are sorted domain-first, drupal_internal__* last.
        """
        meta = self._fields.get(full_type)
        if not meta:
            return {"attributes": [], "relationships": []}
        return {"attributes": list(meta["attributes"]), "relationships": list(meta["relationships"])}

    def suggest_fields(self, full_type: str, bad_name: str, n: int = 4, cutoff: float = 0.4) -> list[str]:
        """Closest valid field names to bad_name (attributes + relationships, difflib).

        Cache-only like fields_for; empty list when no metadata is cached.
        """
        meta = self._fields.get(full_type)
        if not meta:
            return []
        candidates = [*meta["attributes"], *meta["relationships"]]
        return difflib.get_close_matches(bad_name, candidates, n=n, cutoff=cutoff)
