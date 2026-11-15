#!/usr/bin/env python3
"""Read-only probe of the farmOS JSON:API schema endpoint (jsonapi_schema contrib).

Discovers the real URL shape + JSON shape for field metadata (M3.4). Never
prints credentials or tokens; only statuses, URLs and field names.

Run:  .venv/bin/python test/probe_schema.py [asset--equipment log--observation ...]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parent.parent


def env_base() -> str:
    load_dotenv(PROJECT / ".env")
    base = (os.environ.get("FARMOS_URL") or "").strip()
    if base and not base.startswith(("http://", "https://")):
        base = "http://" + base
    return base.rstrip("/")


async def get_token(http: httpx.AsyncClient) -> str:
    resp = await http.post(
        "/oauth/token",
        data={
            "grant_type": "password",
            "client_id": os.environ.get("FARMOS_CLIENT_ID", "farm"),
            "username": os.environ["FARMOS_USERNAME"],
            "password": os.environ["FARMOS_PASSWORD"],
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def strings_below(node: object, path: str = "$") -> list[tuple[str, str]]:
    """(json_path, string) for every string leaf under node."""
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out += strings_below(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += strings_below(v, f"{path}[{i}]")
    elif isinstance(node, str):
        out.append((path, node))
    return out


def schema_mentions(doc: object) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for path, s in strings_below(doc):
        if "schema" in s.lower():
            hits.append((path, s))
    # dict keys containing 'schema'
    def keys_below(node: object, path: str = "$") -> list[tuple[str, str]]:
        out = []
        if isinstance(node, dict):
            for k, v in node.items():
                if "schema" in str(k).lower():
                    out.append((f"{path}.{k}", json.dumps(v)[:120]))
                out += keys_below(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                out += keys_below(v, f"{path}[{i}]")
        return out
    hits += keys_below(doc)
    return hits


def compact_snippet(body: object, n: int = 220) -> str:
    s = json.dumps(body, separators=(",", ":")) if not isinstance(body, str) else body
    return s[:n] + ("…" if len(s) > n else "")


def describe_doc(doc: object) -> None:
    """Best-effort structural summary of a schema-ish document."""
    print("    top-level keys:", sorted(doc.keys()) if isinstance(doc, dict) else type(doc).__name__)
    if not isinstance(doc, dict):
        return
    for key in ("$schema", "title", "type", "definitions", "properties", "allOf", "anyOf", "oneOf"):
        if key in doc:
            v = doc[key]
            if isinstance(v, dict):
                print(f"    {key}: dict with keys {sorted(v.keys())[:40]}")
            else:
                print(f"    {key}: {compact_snippet(v, 100)}")
    data = doc.get("data")
    if isinstance(data, dict):
        print("    data keys:", sorted(data.keys()))
        for sub in ("attributes", "relationships"):
            node = data.get(sub)
            if isinstance(node, dict):
                print(f"    data.{sub} keys: {sorted(node.keys())}")
    for defname, d in (doc.get("definitions") or {}).items() if isinstance(doc.get("definitions"), dict) else []:
        if isinstance(d, dict):
            props = d.get("properties") or {}
            print(f"    definitions.{defname}: keys={sorted(d.keys())[:12]} properties={sorted(props.keys())[:40]}")


async def main() -> None:
    full_types = sys.argv[1:] or ["asset--equipment", "log--observation"]
    base = env_base()
    if not base:
        print("no FARMOS_URL configured — cannot probe")
        return
    async with httpx.AsyncClient(base_url=base, timeout=30, follow_redirects=True) as http:
        tok = await get_token(http)
        auth = {"Authorization": f"Bearer {tok}"}

        # 0) root doc: any 'schema' mentions anywhere?
        r = await http.get("/api", headers=auth)
        print(f"GET /api -> {r.status_code}")
        root = r.json() if r.status_code == 200 else {}
        print("  schema mentions in /api root doc:")
        for path, value in schema_mentions(root)[:25]:
            print(f"    {path} = {value[:160]}")

        # 1) candidate URL shapes (read-only GETs)
        candidates: list[tuple[str, dict | None]] = [
            ("/api/asset/equipment/resource/schema", None),  # per-bundle field definitions (the winner)
            ("/api/asset/equipment/schema", None),           # stub -> refs resource/schema
            ("/api/schema", None),
            ("/api/schema/asset/equipment", None),
            ("/api/schema/asset--equipment", None),
            ("/api/schema", {"resourceType": "asset--equipment"}),
            ("/api/schema", {"type": "asset--equipment"}),
            ("/api/schema", {"resource_type": "asset--equipment"}),
            ("/api/asset/equipment", {"includeSchema": "1"}),
            ("/api/asset/equipment", {"schema": "1"}),
        ]
        working: tuple[str, dict] | None = None
        print("\n  candidate probes:")
        for path, params in candidates:
            r = await http.get(path, headers=auth, params=params)
            body: object = {}
            try:
                body = r.json()
            except ValueError:
                body = r.text[:120]
            print(f"    GET {path}{' ' + str(params) if params else ''} -> {r.status_code}")
            if r.status_code == 200 and isinstance(body, dict) and body:
                describe_doc(body)
                if working is None:
                    working = (path, params or {})
            elif r.status_code >= 400:
                print(f"      body: {compact_snippet(body, 160)}")

        # 2) field lists for the requested types via the working pattern
        if working is None:
            print("\n  !! no candidate returned 200 — report candidates list for extension")
            return
        wpath, wparams = working
        print(f"\n  WORKING pattern: {wpath} (params={wparams or 'none'})")
        for ft in full_types:
            entity, bundle = ft.split("--", 1)
            paths = [
                wpath.replace("equipment", bundle).replace("asset", entity),
                f"/api/{entity}/{bundle}/resource/schema",
            ]
            for path in paths:
                r = await http.get(path, headers=auth, params=wparams or None)
                if r.status_code != 200:
                    print(f"  GET {path} -> {r.status_code}")
                    continue
                doc = r.json()
                print(f"  GET {path} -> 200")
                describe_doc(doc)
                break


if __name__ == "__main__":
    asyncio.run(main())
