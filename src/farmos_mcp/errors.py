"""Typed errors raised by the farmOS MCP layers.

These surface as clean, agent-readable tool errors instead of raw tracebacks.
"""

from __future__ import annotations

import difflib
import re


class FarmOSError(Exception):
    """Base error for the farmOS MCP server."""


class FarmOSConfigError(FarmOSError):
    """Missing or invalid configuration."""


class FarmOSAuthError(FarmOSError):
    """OAuth token acquisition or refresh failed."""


class FarmOSAPIError(FarmOSError):
    """The farmOS JSON:API returned a non-success response."""

    def __init__(self, status_code: int, message: str, errors: list | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []

    @classmethod
    def from_response(
        cls, status_code: int, body: object, path: str | None = None
    ) -> "FarmOSAPIError":
        """Build a readable, status-aware error from a JSON:API error document.

        Agent-facing messages always lead with the HTTP status; 404s get a
        friendly "not found" message instead of Drupal's internal routing
        jargon (the raw error entries are kept on self.errors for debugging).
        """
        errors: list = []
        title: str | None = None
        detail: str | None = None
        if isinstance(body, dict):
            for err in body.get("errors") or []:
                if not isinstance(err, dict):
                    continue
                errors.append(err)
                title = title or err.get("title")
                detail = detail or err.get("detail")
        if status_code == 404:
            message = "resource not found"
        else:
            message = detail or title or "request failed"
        if path:
            message = f"{message} ({path})"
        return cls(status_code, f"HTTP {status_code}: {message}", errors)


class FarmOSNotFoundError(FarmOSAPIError):
    """The requested resource does not exist."""


# -- M3.4 error intelligence (pure helpers; no client/async dependencies) -----

_FIELD_PHRASE_RE = re.compile(r"field [`'\"]([\w.\-]+)[`'\"]", re.IGNORECASE)
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def _short_msg(err: dict, limit: int = 160) -> str:
    """detail-or-title message of one error dict, trimmed to a sane length."""
    text = err.get("detail") or err.get("title") or ""
    return str(text).strip()[:limit].rstrip()


def pointer_field(err: dict) -> str | None:
    """Field name from one JSON:API error's source.pointer, or None.

    /data/attributes/inventory -> 'inventory'; /data/relationships/parent ->
    'parent'. Tolerant of absent/odd shapes (missing pointer, no segment
    after attributes/relationships, non-string values) -> None.
    """
    if not isinstance(err, dict):
        return None
    source = err.get("source")
    pointer = source.get("pointer") if isinstance(source, dict) else None
    if not isinstance(pointer, str):
        return None
    pointer = pointer.strip().lstrip("/")
    if not pointer:
        return None
    parts = [p for p in pointer.split("/") if p]
    for i, part in enumerate(parts):
        if part in ("attributes", "relationships") and i + 1 < len(parts):
            # JSON pointer escapes: ~1 -> '/', ~0 -> '~'
            field_name = parts[i + 1].replace("~1", "/").replace("~0", "~")
            return field_name or None
    return None


def describe_field_errors(errors: list[dict]) -> list[str]:
    """Short '<message>: field '<name>'' strings for pointer-bearing errors.

    Ignores errors without a usable pointer (empty list when nothing usable);
    dedupes identical lines. Message prefers detail, then title.
    """
    out: list[str] = []
    seen: set[str] = set()
    for err in errors or []:
        if not isinstance(err, dict):
            continue
        field_name = pointer_field(err)
        if not field_name:
            continue
        msg = _short_msg(err)
        line = f"{msg}: field '{field_name}'" if msg else f"field '{field_name}'"
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def _detail_fields(errors: list[dict], status_code: int) -> list[tuple[str, str]]:
    """(field, message) pairs mined from error details when no pointers exist.

    Targets Drupal's filter rejection phrasing (verified live on 3.4.5):
    'Invalid nested filtering. The field `X`, given in the path `Y`, does not
    exist.' Tokens from details explicitly saying 'does not exist' are treated
    as offending; on plain 400s any backticked token is a last-resort guess.
    """
    found: list[tuple[str, str]] = []
    taken: set[str] = set()
    for err in errors or []:
        if not isinstance(err, dict):
            continue
        msg = _short_msg(err)
        if not msg:
            continue
        lower = msg.lower()
        tokens: list[str] = []
        if "does not exist" in lower:
            tokens = _FIELD_PHRASE_RE.findall(msg) or _BACKTICK_RE.findall(msg)
        elif status_code == 400:
            tokens = _BACKTICK_RE.findall(msg)
        for token in tokens:
            token = token.strip()
            if token and token not in taken:
                taken.add(token)
                found.append((token, msg))
    return found


def validation_hint(
    status_code: int, errors: list[dict], known_fields: list[str], full_type: str
) -> str | None:
    """Agent-facing hint for 422/400 field errors, or None when nothing derivable.

    Offending fields come from source.pointer (write validation) or, when no
    pointer exists, from Drupal's 'field `X` ... does not exist.' details
    (read/filter rejections). Unknown fields produce
    "field '<f>' does not exist on <full_type>" + closest-match suggestion +
    the full known-field list; known fields pass the value rejection through
    (Drupal detail) instead. All fields unknown-check against known_fields.
    """
    if not errors:
        return None
    known = set(known_fields or [])
    fields: list[str] = []
    msg_by_field: dict[str, str] = {}
    for err in errors:
        if not isinstance(err, dict):
            continue
        field_name = pointer_field(err)
        if not field_name or field_name in fields:
            continue
        fields.append(field_name)
        msg_by_field[field_name] = _short_msg(err)
    if not fields:
        for field_name, msg in _detail_fields(errors, status_code):
            if field_name not in fields:
                fields.append(field_name)
                msg_by_field[field_name] = msg
    if not fields:
        return None
    fields_listing = ", ".join(known_fields or [])
    parts: list[str] = []
    for field_name in fields:
        leaf = field_name.split(".")[-1]
        if leaf in known:
            value_part = f"invalid value for field '{field_name}' on {full_type}"
            msg = msg_by_field.get(field_name, "")
            if msg:
                value_part = f"{value_part}: {msg}"
            parts.append(value_part)
            continue
        part = f"field '{field_name}' does not exist on {full_type}"
        suffixes: list[str] = []
        if known:
            match = difflib.get_close_matches(leaf, known_fields or [], n=1, cutoff=0.4)
            if match:
                suffixes.append(f"Did you mean '{match[0]}'?")
            if fields_listing:
                suffixes.append(f"Fields: {fields_listing}")
        if suffixes:
            part += ". " + " ".join(suffixes)
        parts.append(part)
    return "; ".join(parts)


def enrich_api_error(exc: "FarmOSAPIError", discovery_fields: dict, full_type: str) -> str:
    """Single-line, hint-enriched message for a raised API error (pure, no network).

    Combines the error's 'HTTP <status>: ...' message with validation_hint
    built from discovery_fields (a fields_for() output dict) for full_type.
    Returns the original message unchanged when no hint can be derived.
    """
    known = list((discovery_fields or {}).get("attributes") or [])
    known += list((discovery_fields or {}).get("relationships") or [])
    base = str(exc)
    hint = validation_hint(exc.status_code, exc.errors, known, full_type)
    return f"{base} — {hint}" if hint else base
