"""Write tools for the farmOS MCP server (M2 + M3 write conveniences).

Design notes:
- All mutating tools check FARMOS_READ_ONLY at call time (not registration) so
  an unconfigured server still boots and tools/list stays stable.
- JSON:API relationship references need the full resource type, so asset
  references are {"type": "asset--<bundle>", "id": "<uuid>"} — exactly the
  shape farmos_assets / farmos_get already return to agents.
- update_entity enforces an attribute allowlist; broader edits should go
  through the API directly rather than an unguarded passthrough.
- Inventory is NEVER patched directly (M3.2): adjustments go through a 'done'
  log carrying an inventory_adjustment quantity, the farmOS-native pattern.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from ..client import FarmOSAPIError, FarmOSClient, Filter
from ..config import Config
from ..discovery import Discovery
from ..errors import FarmOSError, enrich_api_error
from ..summarize import summarize_single
from .helpers import to_epoch

UPDATABLE_FIELDS = {"name", "notes", "status"}

# Valid quantity measures (QuantityHelper::quantityMeasures in farmOS source).
MEASURES = (
    "count", "length", "weight", "area", "volume", "time", "temperature",
    "pressure", "water_content", "value", "rate", "rating", "ratio",
    "probability", "speed",
)

# Log bundles the checkin shorthand accepts (the common "note something
# happened" cases; anything more exotic goes through farmos_create_log).
CHECKIN_TYPES = ("observation", "activity", "harvest", "input", "maintenance", "medical")


def _ref_list(refs: list[dict] | None, field: str) -> list[dict]:
    """Validate relationship references ({type, id}) coming from an agent."""
    out: list[dict] = []
    for ref in refs or []:
        if not isinstance(ref, dict) or not ref.get("id") or not ref.get("type"):
            raise FarmOSError(
                f"{field} entries must be objects like "
                '{"type": "asset--plant", "id": "<uuid>"} '
                "(copy type+id straight from farmos_assets / farmos_get results)"
            )
        if not str(ref["type"]).startswith("asset--"):
            raise FarmOSError(f"{field} references must be asset--* types, got '{ref['type']}'")
        out.append({"type": ref["type"], "id": ref["id"]})
    return out


async def _create_and_verify(
    client: FarmOSClient, entity_type: str, bundle: str, attributes: dict,
    relationships: dict | None, includes: list[str] | None,
) -> dict:
    """POST a resource, then immediately GET it back with includes.

    Returns the read-back summary so callers see resolved relationships and
    server-normalized field values — a write is only reported after a read
    confirms it.
    """
    created = await client.create_resource(entity_type, bundle, attributes, relationships)
    uuid_ = (created.get("data") or {}).get("id")
    doc = await client.get_resource(entity_type, bundle, uuid_, includes=includes)
    return summarize_single(doc)


def _check_writable(config: Callable[[], Config]) -> Config:
    cfg = config()
    if cfg.read_only:
        raise FarmOSError(
            "This server is in read-only mode (FARMOS_READ_ONLY=1); write tools are disabled"
        )
    return cfg


async def _hinted_call(
    client: FarmOSClient, discovery: Discovery, full_type: str,
    call: Callable[..., dict], *args, **kwargs,
) -> dict:
    """Run a client write call; on 400/422 re-raise with schema-driven field hints.

    Uses the /api schema cache (M3.4) so agents see e.g. "field 'inventry' does
    not exist on asset--equipment. Did you mean 'inventory'?" instead of raw
    Drupal validation jargon. Hint building is best-effort: any failure keeps
    the original error message intact.
    """
    try:
        return await call(*args, **kwargs)
    except FarmOSAPIError as exc:
        if exc.status_code not in (400, 422):
            raise
        message = str(exc)
        try:
            await discovery.ensure_schema(client, full_type)
            message = enrich_api_error(exc, discovery.fields_for(full_type), full_type)
        except Exception:  # noqa: BLE001 — hints must never mask the real error
            pass
        raise FarmOSAPIError(exc.status_code, message, exc.errors) from exc


def _is_uuid(value: str) -> bool:
    """True for a plain UUID string (36 chars, 4 dashes)."""
    return len(value) == 36 and value.count("-") == 4


def _fraction(value: float) -> dict:
    """Quantity value as a farmOS fraction; whole numbers keep denominator 1."""
    if float(value).is_integer():
        return {"numerator": int(value), "denominator": 1}
    return {"numerator": round(value * 1000), "denominator": 1000}


async def _post_quantity(client: FarmOSClient, attributes: dict, relationships: dict | None) -> dict:
    """POST a quantity resource — quantity--standard first, quantity--material
    as a 4xx fallback for servers configured differently. Returns the created
    document so callers reference the type the server actually accepted."""
    last_error: Exception | None = None
    for bundle in ("standard", "material"):
        try:
            return await client.create_resource("quantity", bundle, attributes, relationships)
        except FarmOSAPIError as exc:
            if exc.status_code >= 500:
                raise  # server-side failure: fail loudly, never fake success
            last_error = exc
    raise last_error  # type: ignore[misc]  # loop exits only via return/raise


async def _resolve_unit(
    client: FarmOSClient, units: str | None, *, strict: bool
) -> tuple[dict | None, str | None]:
    """Resolve a unit term name or UUID to a taxonomy_term--unit reference.

    Returns (ref, note): ref is None when nothing (or several) matched, and
    note explains that. strict=True (inventory changes) raises instead — units
    select the inventory bucket, so ambiguity must not be guessed away.
    """
    units = (units or "").strip()
    if not units:
        return None, None
    if _is_uuid(units):
        return {"type": "taxonomy_term--unit", "id": units}, None
    try:
        doc = await client.get_collection(
            "taxonomy_term", "unit", filters=[Filter("name", units, "CONTAINS")], limit=5
        )
    except FarmOSAPIError as exc:
        if exc.status_code >= 500:
            raise
        return None, f"unit term search failed (HTTP {exc.status_code}); attached no units"
    matches = [((d.get("attributes") or {}).get("name") or "?", d.get("id")) for d in doc.get("data") or []]
    if len(matches) == 1:
        return {"type": "taxonomy_term--unit", "id": matches[0][1]}, None
    names = ", ".join(f"'{name}'" for name, _ in matches)
    if strict:
        found = f"matches: {names}" if matches else "no unit term found (check farmos_terms type='unit')"
        raise FarmOSError(
            f"units_name '{units}' resolved to {len(matches)} unit term(s) ({found}). Units "
            "change the inventory bucket, so pass an exact term UUID (from farmos_terms) "
            "or a more precise name."
        )
    if matches:
        note = (
            f"'{units}' matched {len(matches)} unit terms ({names}) — attached no units; "
            "pass a term UUID (from farmos_terms) to be precise"
        )
    else:
        note = f"no unit term matched '{units}' (see farmos_terms type='unit') — attached no units"
    return None, note


async def _resolve_assets_by_name(
    client: FarmOSClient, discovery: Discovery, asset_name: str
) -> list[dict]:
    """CONTAINS-search an asset name across every asset bundle concurrently.

    Returns de-duplicated candidates as {id, type, name}; bundles that error
    with 4xx (e.g. no permission) are skipped, 5xx fails loudly.
    """
    filters = [Filter("name", asset_name, "CONTAINS")]

    async def one(bundle: str):
        try:
            return await client.get_collection("asset", bundle, filters=filters, limit=10)
        except FarmOSAPIError as exc:
            if exc.status_code >= 500:
                raise
            return None
        except FarmOSError:
            return None

    docs = await asyncio.gather(*(one(b) for b in discovery.bundles_for("asset")))
    seen: dict[str, dict] = {}
    for doc in docs:
        for resource in doc.get("data") or []:
            rid = resource.get("id")
            if rid and rid not in seen:
                seen[rid] = {
                    "id": rid,
                    "type": resource.get("type"),
                    "name": (resource.get("attributes") or {}).get("name") or rid,
                }
    return list(seen.values())


def register(mcp: FastMCP, client: FarmOSClient, config: Callable[[], Config], discovery: Discovery) -> None:

    @mcp.tool()
    async def farmos_create_asset(
        type: str,
        name: str,
        notes: str | None = None,
        is_location: bool | None = None,
        is_group: bool | None = None,
        parent_refs: list[dict] | None = None,
    ) -> dict:
        """Create a farmOS asset (plant, animal, equipment, land, structure, ...).

        Args:
            type: asset bundle, e.g. 'plant', 'animal', 'equipment', 'land', 'group'.
            name: display name (required).
            notes: free-text notes.
            is_location / is_group: set true to make the asset usable as a
                location or as a group assignment target.
            parent_refs: parent asset references, e.g.
                [{"type": "asset--land", "id": "<uuid>"}].
        Returns the created asset (id, name, status, ...).
        """
        _check_writable(config)
        await discovery.ensure(client)
        discovery.validate_type(f"asset--{type}")
        attributes: dict = {"name": name, "status": "active"}
        if notes is not None:
            attributes["notes"] = notes
        if is_location is not None:
            attributes["is_location"] = is_location
        if is_group is not None:
            attributes["is_group"] = is_group
        relationships: dict = {}
        parents = _ref_list(parent_refs, "parent_refs")
        if parents:
            relationships["parent"] = {"data": parents}
        return await _hinted_call(
            client, discovery, f"asset--{type}", _create_and_verify,
            client, "asset", type, attributes, relationships or None, ["parent"],
        )

    @mcp.tool()
    async def farmos_create_log(
        type: str,
        name: str,
        timestamp: str | None = None,
        status: str = "pending",
        asset_refs: list[dict] | None = None,
        location_refs: list[dict] | None = None,
        notes: str | None = None,
        is_movement: bool | None = None,
    ) -> dict:
        """Create a farmOS log (activity, observation, harvest, input, ...).

        Args:
            type: log bundle, e.g. 'activity', 'observation', 'harvest', 'input'.
            name: display name (required).
            timestamp: ISO-8601 ('2026-01-01' or '2026-01-01T10:30:00Z'); defaults to now.
            status: 'pending' (default) or 'done'.
            asset_refs / location_refs: asset references, e.g.
                [{"type": "asset--animal", "id": "<uuid>"}] — copy type+id from
                farmos_assets / farmos_get results. locations are location assets.
            notes: free-text notes.
            is_movement: true for movement logs (changes asset locations).
        Returns the created log with its relationships resolved.
        """
        _check_writable(config)
        await discovery.ensure(client)
        discovery.validate_type(f"log--{type}")
        if status not in {"pending", "done"}:
            raise FarmOSError("status must be 'pending' or 'done'")
        attributes: dict = {
            "name": name,
            "timestamp": int(to_epoch(timestamp)) if timestamp else int(time.time()),
            "status": status,
        }
        if notes is not None:
            attributes["notes"] = notes
        if is_movement is not None:
            attributes["is_movement"] = is_movement
        relationships: dict = {}
        assets = _ref_list(asset_refs, "asset_refs")
        if assets:
            relationships["asset"] = {"data": assets}
        locations = _ref_list(location_refs, "location_refs")
        if locations:
            relationships["location"] = {"data": locations}
        return await _hinted_call(
            client, discovery, f"log--{type}", _create_and_verify,
            client, "log", type, attributes, relationships or None,
            ["asset", "location", "category", "quantity"],
        )

    @mcp.tool()
    async def farmos_update_entity(
        full_type: str,
        uuid: str,
        name: str | None = None,
        notes: str | None = None,
        status: str | None = None,
    ) -> dict:
        """Update selected fields of one farmOS resource (asset, log, ...).

        Args:
            full_type: resource type like 'asset--plant' or 'log--activity'
                (see farmos_list_resources).
            uuid: the resource UUID.
            name / notes: new values; omit to leave unchanged.
            status: for logs — 'pending' or 'done'; for assets — the boolean
                published flag is managed via archive/unarchive instead.
        Returns the updated resource.
        """
        _check_writable(config)
        await discovery.ensure(client)
        entity_type, bundle = discovery.validate_type(full_type)
        requested = {"name": name, "notes": notes, "status": status}
        attributes = {k: v for k, v in requested.items() if v is not None}
        if not attributes:
            raise FarmOSError("Nothing to update: provide name, notes, and/or status")
        unsupported = set(attributes) - UPDATABLE_FIELDS
        if unsupported:
            raise FarmOSError(
                f"Fields {sorted(unsupported)} cannot be updated via this tool; "
                f"updatable fields: {sorted(UPDATABLE_FIELDS)}"
            )
        doc = await _hinted_call(
            client, discovery, full_type, client.patch_resource,
            entity_type, bundle, uuid, attributes,
        )
        return summarize_single(doc)

    @mcp.tool()
    async def farmos_archive(full_type: str, uuid: str, unarchive: bool = False) -> dict:
        """Archive (or unarchive) a farmOS asset — the soft-delete pattern.

        Args:
            full_type: an asset--* resource type, e.g. 'asset--equipment'.
            uuid: the asset UUID.
            unarchive: true to restore an archived asset.
        Returns the updated asset.
        """
        _check_writable(config)
        await discovery.ensure(client)
        entity_type, bundle = discovery.validate_type(full_type)
        if entity_type != "asset":
            raise FarmOSError(
                f"Archiving applies to assets; '{full_type}' is a '{entity_type}'. "
                "For logs, update status instead (pending/done)."
            )
        attributes = {"archived": None if unarchive else int(time.time())}
        doc = await _hinted_call(
            client, discovery, full_type, client.patch_resource,
            entity_type, bundle, uuid, attributes,
        )
        return summarize_single(doc)

    @mcp.tool()
    async def farmos_flag(full_type: str, uuid: str, flags: list[str], clear: bool = False) -> dict:
        """Set or clear farmOS flags (e.g. 'monitor', 'priority') on an asset or log.

        Args:
            full_type: resource type like 'asset--animal' or 'log--activity'.
            uuid: the resource UUID.
            flags: flag names to set. Which flags exist depends on the server
                (farm_flag module); invalid names are rejected with a 4xx error.
            clear: true to remove ALL flags (then flags must be empty).
        Returns the updated resource.
        """
        _check_writable(config)
        await discovery.ensure(client)
        entity_type, bundle = discovery.validate_type(full_type)
        if clear:
            if flags:
                raise FarmOSError("clear=true removes all flags; pass an empty flags list")
            doc = await _hinted_call(
                client, discovery, full_type, client.patch_resource,
                entity_type, bundle, uuid, {"flag": []},
            )
            return summarize_single(doc)
        if not flags:
            raise FarmOSError("Provide flag names, or clear=true to remove all flags")
        # Read-modify-write so existing flags are preserved.
        current = await client.get_resource(entity_type, bundle, uuid)
        attrs = ((current.get("data") or {}).get("attributes") or {})
        existing = [f for f in (attrs.get("flag") or []) if isinstance(f, str)]
        merged = sorted(set(existing) | set(flags))
        doc = await _hinted_call(
            client, discovery, full_type, client.patch_resource,
            entity_type, bundle, uuid, {"flag": merged},
        )
        return summarize_single(doc)

    @mcp.tool()
    async def farmos_checkin_log(
        type: str = "observation",
        asset_name: str | None = None,
        asset_refs: list[dict] | None = None,
        name: str | None = None,
        notes: str | None = None,
        quantity: dict | None = None,
        done: bool = True,
        allow_ambiguous: bool = False,
    ) -> dict:
        """One-shot "log something happened on asset X" — resolves the asset by
        name and creates the log in a single call.

        Args:
            type: log bundle — one of observation, activity, harvest, input,
                maintenance, medical (default 'observation').
            asset_name: asset name to resolve (exact match preferred, else
                contains-match). Must resolve to exactly one asset unless
                allow_ambiguous=true, which logs on every match.
            asset_refs: alternative to asset_name — direct references already
                in hand, e.g. [{"type": "asset--equipment", "id": "<uuid>"}].
                Exactly one of asset_name / asset_refs is required.
            name: log display name; auto-generated as '<Type> on <asset>'
                when omitted.
            notes: free-text notes.
            quantity: optional measurement sugar, e.g.
                {"value": 3, "measure": "count", "units": "each"} — 'units'
                may be a unit term name or a taxonomy_term--unit UUID (from
                farmos_terms); an ambiguous or unmatched name attaches no
                units and is reported in the response. This quantity carries
                NO inventory_adjustment, so it does not change asset inventory.
            done: true → status 'done' (default); false → 'pending'.
            allow_ambiguous: log on ALL assets matching asset_name instead of
                refusing when the name is ambiguous.
        Returns {log_summary, asset_resolution: {query, matched, ambiguous},
        quantities?: [{id, type, label}], units_resolution?} — the read-back
        log summary, the asset matches used, and any created quantity
        resources (returned so callers can track/clean them up).
        """
        _check_writable(config)
        await discovery.ensure(client)
        if type not in CHECKIN_TYPES:
            extra = (
                f" '{type}' is a real log bundle on this server but is not allowed here — "
                "use farmos_create_log for it"
                if f"log--{type}" in discovery.types else ""
            )
            raise FarmOSError(f"checkin type must be one of: {', '.join(CHECKIN_TYPES)}.{extra}")
        discovery.validate_type(f"log--{type}")
        if bool(asset_name) == bool(asset_refs):
            raise FarmOSError("Provide exactly one of asset_name / asset_refs")

        matched: list[dict] = []
        ambiguous = False
        refs: list[dict] = []
        if asset_name:
            candidates = await _resolve_assets_by_name(client, discovery, asset_name.strip())
            exact = [c for c in candidates if str(c["name"]).lower() == asset_name.strip().lower()]
            if len(exact) == 1:
                matched = exact
            elif len(candidates) == 1:
                matched = candidates
            elif not candidates:
                raise FarmOSError(
                    f"No asset matched asset_name '{asset_name}' (CONTAINS search across all "
                    "asset bundles) — check the name via farmos_assets, or pass asset_refs "
                    "with a UUID"
                )
            elif allow_ambiguous:
                matched, ambiguous = candidates, True
            else:
                bullets = "\n".join(f"  - {c['id']} ({c['type']}) {c['name']}" for c in candidates)
                raise FarmOSError(
                    f"asset_name '{asset_name}' matched {len(candidates)} assets — pass "
                    "allow_ambiguous=true to log on all, or use asset_refs with a UUID:\n"
                    f"{bullets}"
                )
        else:
            refs = _ref_list(asset_refs, "asset_refs")
            if not refs:
                raise FarmOSError("asset_refs is empty — pass asset_name or non-empty asset_refs")

        quantities: list[dict] = []
        unit_note: str | None = None
        if quantity is not None:
            if not isinstance(quantity, dict) or quantity.get("value") is None:
                raise FarmOSError(
                    'quantity must be a dict like {"value": 3, "measure": "count", '
                    '"units": "<term name or UUID>"}'
                )
            measure = quantity.get("measure") or "count"
            if measure not in MEASURES:
                raise FarmOSError(
                    f"quantity measure '{measure}' is not valid. Valid measures: {', '.join(MEASURES)}"
                )
            try:
                value = float(quantity["value"])
            except (TypeError, ValueError) as exc:
                raise FarmOSError("quantity.value must be a number") from exc
            unit_ref, unit_note = await _resolve_unit(client, quantity.get("units"), strict=False)
            created = await _hinted_call(
                client, discovery, "quantity--standard", _post_quantity,
                client,
                {"measure": measure, "value": _fraction(value)},
                {"units": {"data": unit_ref}} if unit_ref else None,
            )
            data = created.get("data") or {}
            quantities.append({"id": data.get("id"), "type": data.get("type"), "label": f"{value:g} {measure}"})

        if not name and matched:
            name = f"{type.capitalize()} on {matched[0]['name']}"
        elif not name and refs:
            # Auto-naming on the UUID path needs the asset's name — one read.
            try:
                entity, bundle = str(refs[0]["type"]).split("--", 1)
                doc = await client.get_resource(entity, bundle, refs[0]["id"])
                first_name = ((doc.get("data") or {}).get("attributes") or {}).get("name")
                name = f"{type.capitalize()} on {first_name}" if first_name else type.capitalize()
            except (FarmOSAPIError, FarmOSError):
                name = type.capitalize()

        relationships: dict = {
            "asset": {
                "data": [{"type": c["type"], "id": c["id"]} for c in matched] if matched else refs
            },
        }
        if quantities:
            relationships["quantity"] = {
                "data": [{"type": q["type"], "id": q["id"]} for q in quantities]
            }
        attributes: dict = {
            "name": name,
            "timestamp": int(time.time()),
            "status": "done" if done else "pending",
        }
        if notes is not None:
            attributes["notes"] = notes
        log_summary = await _hinted_call(
            client, discovery, f"log--{type}", _create_and_verify,
            client, "log", type, attributes, relationships or None,
            ["asset", "location", "category", "quantity"],
        )

        if asset_name:
            resolution = {"query": asset_name, "matched": matched, "ambiguous": ambiguous}
        else:
            asset_rels = log_summary.get("asset") or []
            if isinstance(asset_rels, dict):
                asset_rels = [asset_rels]
            names = {a.get("id"): a.get("name") for a in asset_rels if isinstance(a, dict)}
            resolution = {
                "query": None,
                "matched": [
                    {"id": r["id"], "type": r["type"], "name": names.get(r["id"]) or r["id"]}
                    for r in refs
                ],
                "ambiguous": False,
            }
        out: dict = {"log_summary": log_summary, "asset_resolution": resolution}
        if quantities:
            out["quantities"] = quantities
        if unit_note:
            out["units_resolution"] = unit_note
        return out

    @mcp.tool()
    async def farmos_inventory_change(
        asset_refs: list[dict],
        delta: float,
        measure: str = "count",
        units_name: str | None = None,
        log_type: str = "input",
        notes: str | None = None,
        timestamp: str | None = None,
    ) -> dict:
        """Adjust an asset's inventory the farmOS-native way: a 'done' adjustment
        log carrying an inventory_adjustment quantity — never a direct PATCH of
        the asset.

        Inventory totals are COMPUTED by farmOS from adjustment quantities on
        logs with status 'done' and timestamp <= now (grouped per measure+units
        pair); deleting the adjustment log reverts them. 'reset' semantics
        (set-to-value) are NOT exposed via delta — agents needing a reset
        should create a log with a 'reset' quantity via the API directly.

        Args:
            asset_refs: target asset references, e.g.
                [{"type": "asset--equipment", "id": "<uuid>"}] — one quantity
                is created per asset, all carried by one adjustment log.
            delta: signed amount to apply; positive increments, negative
                decrements. Must be non-zero.
            measure: one of count, length, weight, area, volume, time,
                temperature, pressure, water_content, value, rate, rating,
                ratio, probability, speed.
            units_name: optional unit term name or taxonomy_term--unit UUID.
                Units select the inventory bucket, so an ambiguous or
                unmatched unit name is an error — pass an exact term UUID
                (from farmos_terms) or a more precise name.
            log_type: 'input' (default) or 'activity'.
            notes: free-text notes on the adjustment log.
            timestamp: ISO-8601 ('2026-01-01' or '2026-01-01T10:30:00Z');
                defaults to now. farmOS only counts logs with timestamp <=
                now, so a future timestamp will not take effect until then.
        Returns {log_summary, inventory_changes: [{id, type, name, before,
        after}], quantities: [{id, type, label}]} — before/after inventory
        read straight back from the assets, plus the created quantities.
        """
        _check_writable(config)
        await discovery.ensure(client)
        if log_type not in ("input", "activity"):
            raise FarmOSError("log_type must be 'input' or 'activity'")
        discovery.validate_type(f"log--{log_type}")
        if measure not in MEASURES:
            raise FarmOSError(
                f"measure '{measure}' is not valid. Valid measures: {', '.join(MEASURES)}"
            )
        if not delta:
            raise FarmOSError(
                "delta must be non-zero (positive increments, negative decrements)"
            )
        refs = _ref_list(asset_refs, "asset_refs")
        if not refs:
            raise FarmOSError("asset_refs must be a non-empty list of asset references")
        unit_ref, _ = await _resolve_unit(client, units_name, strict=True)

        # Confirm every target exists and carries an inventory field before
        # writing anything.
        async def read_asset(ref: dict) -> tuple[dict, dict]:
            entity, bundle = str(ref["type"]).split("--", 1)
            doc = await client.get_resource(entity, bundle, ref["id"])
            return ref, (doc.get("data") or {}).get("attributes") or {}

        targets: list[tuple[dict, list, str | None]] = []
        for ref, attrs in await asyncio.gather(*(read_asset(ref) for ref in refs)):
            if "inventory" not in attrs:
                raise FarmOSError(
                    f"asset {ref['id']} ({ref['type']}) has no inventory field "
                    "(farm_inventory module disabled?)"
                )
            inventory = attrs.get("inventory")
            targets.append((ref, inventory if isinstance(inventory, list) else [], attrs.get("name")))

        # One adjustment quantity per asset, all carried by one log.
        adjustment = "increment" if delta > 0 else "decrement"
        label = f"{'+' if delta > 0 else '-'}{abs(delta):g} {measure}"
        quantities: list[dict] = []
        for ref, _inventory, _name in targets:
            rels: dict = {"inventory_asset": {"data": {"type": ref["type"], "id": ref["id"]}}}
            if unit_ref:
                rels["units"] = {"data": unit_ref}
            created = await _hinted_call(
                client, discovery, "quantity--standard", _post_quantity,
                client,
                {"measure": measure, "value": _fraction(abs(delta)),
                 "inventory_adjustment": adjustment, "label": label},
                rels,
            )
            data = created.get("data") or {}
            quantities.append({"id": data.get("id"), "type": data.get("type"), "label": label})

        log_attributes = {
            "name": f"Inventory adjustment: {label}",
            "timestamp": int(to_epoch(timestamp)) if timestamp else int(time.time()),
            "status": "done",
        }
        if notes is not None:
            log_attributes["notes"] = notes
        log_relationships = {
            "asset": {"data": [{"type": ref["type"], "id": ref["id"]} for ref, _, _ in targets]},
            "quantity": {"data": [{"type": q["type"], "id": q["id"]} for q in quantities]},
        }
        log_summary = await _hinted_call(
            client, discovery, f"log--{log_type}", _create_and_verify,
            client, "log", log_type, log_attributes, log_relationships,
            ["asset", "quantity"],
        )

        # Re-read every target asset so the agent sees the effect, not the write.
        async def refetch(ref: dict) -> list:
            entity, bundle = str(ref["type"]).split("--", 1)
            doc = await client.get_resource(entity, bundle, ref["id"])
            inventory = ((doc.get("data") or {}).get("attributes") or {}).get("inventory")
            return inventory if isinstance(inventory, list) else []

        afters = await asyncio.gather(*(refetch(ref) for ref, _, _ in targets))
        inventory_changes = [
            {"id": ref["id"], "type": ref["type"], "name": asset_name or ref["id"],
             "before": before, "after": after}
            for (ref, before, asset_name), after in zip(targets, afters)
        ]
        return {"log_summary": log_summary, "inventory_changes": inventory_changes, "quantities": quantities}
