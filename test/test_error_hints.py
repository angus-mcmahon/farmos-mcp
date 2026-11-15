#!/usr/bin/env python3
"""Offline tests for M3.4 error intelligence (field hints + filter suggestions).

No live server: discovery's schema fetch runs against a mock client serving a
canned /api root document plus canned /api/{entity}/{bundle}/resource/schema
documents (the shape verified live against farmOS 3.4.5).

Run:  .venv/bin/python test/test_error_hints.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from farmos_mcp.discovery import Discovery  # noqa: E402
from farmos_mcp.errors import (  # noqa: E402
    FarmOSAPIError,
    FarmOSError,
    describe_field_errors,
    enrich_api_error,
    pointer_field,
    validation_hint,
)

RESULTS: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(("PASS" if ok else "FAIL", name, detail))
    return ok


# -- canned documents (same shape as the live server) --------------------------

ROOT = {
    "meta": {"farm": {"name": "Test Farm", "version": "3.4.5", "system_of_measurement": "metric"}},
    "links": {
        "asset--equipment": {"href": "/api/asset/equipment"},
        "log--observation": {"href": "/api/log/observation"},
    },
}

EQ_PATH = "/api/asset/equipment/resource/schema"
LOG_PATH = "/api/log/observation/resource/schema"

EQ_ATTRS = [
    "drupal_internal__id",
    "drupal_internal__revision_id",
    "flag",
    "inventory",
    "is_location",
    "manufacturer",
    "model",
    "name",
    "notes",
    "quick",
    "serial_number",
    "status",
]
EQ_RELS = ["asset_type", "equipment_type", "file", "group", "image", "location", "owner", "parent", "revision_user", "uid"]
LOG_ATTRS = ["created", "is_movement", "name", "notes", "status", "timestamp"]
LOG_RELS = ["asset", "category", "location", "quantity", "log_type"]


def schema_doc(attrs: list[str], rels: list[str], const: str) -> dict:
    """Same shape as a live /api/{entity}/{bundle}/resource/schema document."""
    return {
        "$schema": "https://json-schema.org/draft/2019-09/hyper-schema",
        "title": "schema",
        "definitions": {
            "type": {"const": const},
            "attributes": {"type": "object", "properties": {a: {} for a in attrs}},
            "relationships": {"type": "object", "properties": {r: {} for r in rels}},
        },
    }


SCHEMAS = {
    EQ_PATH: schema_doc(EQ_ATTRS, EQ_RELS, "asset--equipment"),
    LOG_PATH: schema_doc(LOG_ATTRS, LOG_RELS, "log--observation"),
}

KNOWN_EQ = EQ_ATTRS + EQ_RELS


class MockClient:
    """Duck-typed FarmOSClient stand-in with canned responses."""

    def __init__(self, root: dict, schemas: dict[str, dict], failures: set[str] | None = None):
        self.root = root
        self.schemas = schemas
        self.failures = failures or set()
        self.calls: list[tuple[str, str]] = []

    async def request(self, method: str, path: str, *, params=None, json_body=None) -> dict:
        self.calls.append((method, path))
        if path in self.failures:
            raise FarmOSAPIError.from_response(
                404, {"errors": [{"title": "Not Found", "detail": "no such route"}]}, path
            )
        if path == "/api":
            return self.root
        if path in self.schemas:
            return self.schemas[path]
        raise FarmOSAPIError.from_response(404, {"errors": [{"title": "Not Found"}]}, path)


async def main() -> None:
    # -- 1) ensure + ensure_schema populate fields_for; unknown types raise ----
    disc = Discovery()
    client = MockClient(ROOT, SCHEMAS)
    await disc.ensure(client)
    check("ensure loads farm meta", disc.farm_name == "Test Farm" and disc.version == "3.4.5")
    check("ensure loads types", disc.validate_type("asset--equipment") == ("asset", "equipment"))

    fields_for_before = disc.fields_for("asset--equipment")
    check("fields_for empty before ensure_schema",
          fields_for_before == {"attributes": [], "relationships": []})

    await disc.ensure_schema(client, "asset--equipment")
    eq = disc.fields_for("asset--equipment")
    check("fields_for populated from schema",
          "name" in eq["attributes"] and "parent" in eq["relationships"], str(eq)[:160])
    check("fields_for drupal_internal__ sorted last",
          eq["attributes"][-2:] == ["drupal_internal__id", "drupal_internal__revision_id"], str(eq["attributes"]))
    check("fields_for sorted domain-first",
          eq["attributes"][:2] == ["flag", "inventory"], str(eq["attributes"][:4]))

    await disc.ensure_schema(client, "log--observation")
    log = disc.fields_for("log--observation")
    check("second bundle cached independently",
          "timestamp" in log["attributes"] and "quantity" in log["relationships"])

    check("fields_for unknown type -> empty", disc.fields_for("asset--nope") == {"attributes": [], "relationships": []})

    returned = disc.fields_for("asset--equipment")
    returned["attributes"].append("junk")
    check("fields_for returns a copy", "junk" not in disc.fields_for("asset--equipment")["attributes"])

    try:
        await disc.ensure_schema(client, "asset--nope")
        check("ensure_schema unknown type raises", False, "no exception raised")
    except FarmOSError:
        check("ensure_schema unknown type raises", True)

    # -- 2) schema fetch 4xx -> cached empty metadata, no exception ------------
    disc2 = Discovery()
    client2 = MockClient(ROOT, {}, failures={EQ_PATH})
    await disc2.ensure(client2)
    try:
        await disc2.ensure_schema(client2, "asset--equipment")
        check("schema 4xx never raises", True)
    except FarmOSError:
        check("schema 4xx never raises", False, "ensure_schema raised")
    check("schema 4xx -> empty fields",
          disc2.fields_for("asset--equipment") == {"attributes": [], "relationships": []})
    check("suggest_fields on empty metadata -> []", disc2.suggest_fields("asset--equipment", "name") == [])
    await disc2.ensure_schema(client2, "asset--equipment")
    check("failed schema fetch cached (no refetch)",
          client2.calls == [("GET", "/api"), ("GET", EQ_PATH)], str(client2.calls))

    # -- 3) pointer_field -------------------------------------------------------
    check("pointer attributes", pointer_field({"source": {"pointer": "/data/attributes/inventory"}}) == "inventory")
    check("pointer relationships", pointer_field({"source": {"pointer": "/data/relationships/parent"}}) == "parent")
    check("pointer no leading slash", pointer_field({"source": {"pointer": "data/attributes/name"}}) == "name")
    check("pointer deeper path", pointer_field({"source": {"pointer": "/data/attributes/notes/value"}}) == "notes")
    check("pointer bare attributes", pointer_field({"source": {"pointer": "/data/attributes"}}) is None)
    check("pointer empty string", pointer_field({"source": {"pointer": ""}}) is None)
    check("pointer missing source", pointer_field({"detail": "boom"}) is None)
    check("pointer non-string", pointer_field({"source": {"pointer": 42}}) is None)
    check("pointer non-dict", pointer_field("junk") is None)  # type: ignore[arg-type]
    check("pointer unrelated root", pointer_field({"source": {"pointer": "/data"}}) is None)

    # -- 4) describe_field_errors ----------------------------------------------
    errs = [
        {"detail": "The state '1' is invalid.", "source": {"pointer": "/data/attributes/status"}},
        {"detail": "The state '1' is invalid.", "source": {"pointer": "/data/attributes/status"}},  # dupe
        {"title": "Bad Request", "source": {"pointer": "/data/attributes/name"}},
        {"detail": "no pointer here at all"},
    ]
    out = describe_field_errors(errs)
    check("describe_field_errors dedupes + uses detail",
          out == ["The state '1' is invalid.: field 'status'", "Bad Request: field 'name'"], str(out))
    check("describe_field_errors title fallback",
          describe_field_errors([{"title": "Bad Request", "source": {"pointer": "/data/attributes/name"}}])
          == ["Bad Request: field 'name'"])
    check("describe_field_errors empty on garbage",
          describe_field_errors([{"detail": "junk"}, "junk", {}, None]) == [])  # type: ignore[list-item]
    check("describe_field_errors empty input", describe_field_errors([]) == [])

    # -- 5) validation_hint ------------------------------------------------------
    hint = validation_hint(
        422,
        [{"detail": "no such property", "source": {"pointer": "/data/attributes/inventry"}}],
        KNOWN_EQ,
        "asset--equipment",
    )
    check("hint unknown field: does-not-exist wording",
          "field 'inventry' does not exist on asset--equipment" in hint, str(hint))
    check("hint unknown field: did-you-mean", "Did you mean 'inventory'?" in hint, str(hint))
    check("hint unknown field: lists fields",
          "Fields:" in hint and "name" in hint and "manufacturer" in hint and "parent" in hint, str(hint))

    hint_val = validation_hint(
        422,
        [{"detail": "The state '1' is invalid.", "source": {"pointer": "/data/attributes/status"}}],
        KNOWN_EQ,
        "asset--equipment",
    )
    check("hint known field: value pass-through",
          hint_val is not None and "invalid value for field 'status' on asset--equipment" in hint_val
          and "The state '1' is invalid." in hint_val, str(hint_val))

    check("hint empty errors -> None", validation_hint(422, [], KNOWN_EQ, "asset--equipment") is None)
    check("hint no pointers no details -> None",
          validation_hint(422, [{"title": "Broken"}], KNOWN_EQ, "asset--equipment") is None)

    multi = validation_hint(
        422,
        [
            {"detail": "bad", "source": {"pointer": "/data/attributes/zzz"}},
            {"detail": "bad", "source": {"pointer": "/data/attributes/name"}},  # known -> value msg
        ],
        ["name", "status"],
        "log--observation",
    )
    check("hint mixed fields", multi is not None and "field 'zzz' does not exist on log--observation" in multi
          and "invalid value for field 'name'" in multi, str(multi))

    # -- 6) enrich_api_error -----------------------------------------------------
    exc = FarmOSAPIError.from_response(
        422,
        {"errors": [{"detail": "no such property", "source": {"pointer": "/data/attributes/inventry"}}]},
        "/api/asset/equipment",
    )
    msg = enrich_api_error(exc, {"attributes": EQ_ATTRS, "relationships": EQ_RELS}, "asset--equipment")
    check("enrich keeps HTTP lead", msg.startswith("HTTP 422"), msg)
    check("enrich names the bad field", "inventry" in msg, msg)
    check("enrich includes suggestion + fields", "Did you mean 'inventory'?" in msg and "Fields:" in msg, msg)
    check("enrich single line", "\n" not in msg, msg)

    exc_boring = FarmOSAPIError.from_response(500, {}, "/api/asset/equipment")
    check("enrich unchanged when no hint",
          enrich_api_error(exc_boring, {"attributes": EQ_ATTRS, "relationships": EQ_RELS}, "asset--equipment")
          == str(exc_boring))

    # -- 7) suggestion cutoff ----------------------------------------------------
    hint_none = validation_hint(
        422,
        [{"source": {"pointer": "/data/attributes/zzzzz"}}],
        KNOWN_EQ,
        "asset--equipment",
    )
    check("cutoff: no suggestion", hint_none is not None and "Did you mean" not in hint_none, str(hint_none))
    check("cutoff: still lists fields", hint_none is not None and "Fields:" in hint_none and "name" in hint_none)
    check("suggest_fields nonsense -> []", disc.suggest_fields("asset--equipment", "zzzzz") == [])
    sugg = disc.suggest_fields("asset--equipment", "nmae")
    check("suggest_fields close hit first", bool(sugg) and sugg[0] == "name", str(sugg))

    # -- 8) read errors: live-shaped 400 filter rejection ------------------------
    filter_errs = [
        {
            "title": "Bad Request",
            "status": "400",
            "detail": "Invalid nested filtering. The field `is_locaton`, given in the path "
                      "`is_locaton`, does not exist.",
        }
    ]
    fhint = validation_hint(400, filter_errs, KNOWN_EQ, "asset--equipment")
    check("400 filter detail mined", fhint is not None and "field 'is_locaton' does not exist on asset--equipment" in fhint,
          str(fhint))
    check("400 filter suggests closest", fhint is not None and "Did you mean 'is_location'?" in fhint, str(fhint))
    check("400 filter lists fields", fhint is not None and "Fields:" in fhint)
    fhint2 = validation_hint(400, filter_errs, KNOWN_EQ, "asset--land")
    check("400 hint names the queried type", fhint2 is not None and "on asset--land" in fhint2)

    check("400 unrelated detail -> None",
          validation_hint(400, [{"title": "Bad Request", "detail": "Totally different failure."}],
                          KNOWN_EQ, "asset--equipment") is None)
    check("400 backtick fallback",
          validation_hint(400, [{"detail": "`bogus_path` is not a valid filter path."}], KNOWN_EQ,
                          "asset--equipment") is not None)

    # -- summary ------------------------------------------------------------------
    failed = [r for r in RESULTS if r[0] == "FAIL"]
    for status, name, detail in RESULTS:
        print(f"{status}  {name}" + (f"  [{detail}]" if detail and status == "FAIL" else ""))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
