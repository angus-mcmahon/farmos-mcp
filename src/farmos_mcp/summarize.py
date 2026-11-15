"""Compacts JSON:API documents into small, agent-friendly payloads.

This is the key token-efficiency layer: strip the JSON:API envelope, prune
noisy/irrelevant attributes, truncate long text, and dereference common
relationships one hop (resolving names from `included` resources).
"""

from __future__ import annotations

from typing import Any

# Attributes we never want to send to an agent.
_NOISY_ATTRS = {
    "data", "file", "image", "geometry", "physical_geometry", "intrinsic_geometry",
    "drupal_internal__id", "drupal_internal__revision_id", "drupal_internal__vid",
    "revision_translation_affected", "revision_created", "revision_log_message",
    "revision_default", "revision_user", "default_langcode", "langcode",
    "content_translation_source", "content_translation_outdated",
    "content_translation_created", "path", "source", "weight", "changed",
}

# Attributes worth keeping verbatim, by entity type.
_KEEP_ATTRS: dict[str, set[str]] = {
    "asset": {"name", "status", "archived", "is_location", "is_group", "inventory",
              "longitude", "latitude"},
    "log": {"name", "timestamp", "status", "notes", "is_movement"},
    "taxonomy_term": {"name", "description"},
    "quantity": {"measure", "value"},
    "user": {"display_name", "name"},
}

_SCALAR_MAX_LEN = 120
_TEXT_MAX_LEN = 400


def included_index(doc: dict) -> dict[str, dict]:
    """Map resource UUID -> resource object from a document's `included`."""
    index: dict[str, dict] = {}
    for resource in doc.get("included") or []:
        rid = resource.get("id")
        if rid:
            index[rid] = resource
    return index


def _value_text(value: Any) -> str | None:
    """Render a quantity value (fraction dict or scalar) as a short string."""
    if isinstance(value, dict):
        decimal = value.get("decimal")
        if decimal is not None:
            return str(decimal)
        num, den = value.get("numerator"), value.get("denominator")
        if num is not None:
            try:
                n, d = float(num), float(den or 1)
                return f"{n:g}" if d == 1 else f"{n:g}/{d:g}"
            except (TypeError, ValueError):
                return None
        return None
    return str(value)


def _label(resource: dict, included: dict[str, dict] | None = None) -> str | None:
    """Best human label for a resource (its name, or a quantity's measure/value)."""
    attrs = resource.get("attributes") or {}
    name = attrs.get("name")
    if isinstance(name, str) and name:
        return name
    value = _value_text(attrs.get("value"))
    if value is not None:
        measure = attrs.get("measure")
        label = f"{measure}: {value}" if measure else value
        # Append the unit's name when the units relationship resolves.
        if included:
            units = (resource.get("relationships") or {}).get("units") or {}
            ref = units.get("data")
            unit = included.get(ref.get("id")) if isinstance(ref, dict) else None
            unit_name = (unit or {}).get("attributes", {}).get("name")
            if unit_name:
                label = f"{label} {unit_name}"
        return label
    return None


def _clip(value: Any, limit: int = _TEXT_MAX_LEN) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1] + "…"
    return value


def summarize_resource(resource: dict, included: dict[str, dict]) -> dict:
    """One JSON:API resource -> compact dict with relationships flattened to labels."""
    full_type = resource.get("type") or ""
    entity = full_type.split("--", 1)[0]
    out: dict[str, Any] = {"id": resource.get("id"), "type": full_type}

    keep = _KEEP_ATTRS.get(entity, set())
    for key, value in (resource.get("attributes") or {}).items():
        if key in _NOISY_ATTRS or key in out:
            continue
        if isinstance(value, dict) and "value" in value:
            # Drupal text fields serialize as {value, format, processed}; keep
            # the plain text value agents can read.
            value = value.get("value")
        if key in keep:
            out[key] = _clip(value)
        elif isinstance(value, bool) or isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, str) and len(value) <= _SCALAR_MAX_LEN:
            out[key] = _clip(value)
        # other lists/dicts are dropped to keep payloads small

    for name, rel in (resource.get("relationships") or {}).items():
        data = rel.get("data") if isinstance(rel, dict) else None
        if not data:
            continue
        refs = data if isinstance(data, list) else [data]
        resolved: list[dict[str, Any]] = []
        for ref in refs:
            if not isinstance(ref, dict) or not ref.get("id"):
                continue
            inc = included.get(ref.get("id"))
            label = _label(inc, included) if inc else None
            if label:
                resolved.append({"id": ref.get("id"), "type": ref.get("type"), "name": label})
        if not resolved:
            continue  # unlabeled stubs (revision_user, log_type, ...) are noise
        out[name] = resolved if isinstance(data, list) else resolved[0]
    return out


def summarize_collection(doc: dict, *, offset: int = 0) -> dict:
    """A collection document -> {count, data[], next_offset?}."""
    included = included_index(doc)
    resources = doc.get("data") or []
    items = [summarize_resource(r, included) for r in resources]
    out: dict[str, Any] = {"count": len(items), "data": items}
    if (doc.get("links") or {}).get("next"):
        out["next_offset"] = offset + len(items)
    return out


def summarize_single(doc: dict) -> dict:
    """A single-resource document -> compact resource dict."""
    included = included_index(doc)
    resource = doc.get("data") or {}
    return summarize_resource(resource, included)
