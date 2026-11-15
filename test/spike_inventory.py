"""Live inventory-semantics spike for farmOS 3.4.5 (M3.2 pre-req).

Probes the REAL server (credentials from PROJECT/.env — never printed) with
plain httpx + OAuth password grant, fully self-cleaning:

  1. GET an existing quantity--standard resource (read-only field inspection).
  2. POST asset--equipment -> read its `inventory` (before).
  3. POST quantity--standard {measure, value, inventory_adjustment}
     + relationship inventory_asset -> the asset.
  4. POST log--input status 'done' referencing asset + quantity.
  5. GET asset -> `inventory` (after) — does it recompute?
  6. DELETE the log, DELETE the quantity, GET asset -> reverted?

Every step reports its HTTP status code. If quantity--standard rejects the
inventory fields (4xx), the probe retries with quantity--material and reports
which bundle works.

Run:  .venv/bin/python test/spike_inventory.py
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parent.parent
TAG = f"mcp-test {uuid.uuid4().hex[:8]}"


def auth_headers(token: str | None = None) -> dict:
    headers = {"Accept": "application/vnd.api+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def post_headers(token: str) -> dict:
    return auth_headers(token) | {"Content-Type": "application/vnd.api+json"}


def describe(doc: dict) -> str:
    """Compact resource description: attribute + relationship key names."""
    data = doc.get("data") or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    attrs = sorted((data.get("attributes") or {}).keys())
    rels = sorted((data.get("relationships") or {}).keys())
    return f"type={data.get('type')} attrs={attrs} rels={rels}"


def inventory_of(doc: dict):
    """The asset document's `inventory` attribute, or '<absent>'."""
    attrs = ((doc.get("data") or {}).get("attributes") or {})
    return attrs.get("inventory", "<absent>")


async def run_probe(http: httpx.AsyncClient, get_h: dict, post_h: dict, bundle: str, findings: dict, leftovers: list) -> bool:
    """One full create->recompute->cleanup probe for a quantity bundle."""
    print(f"\n=== WRITE PROBE with quantity--{bundle} ===")
    asset_id = log_id = qty_id = None
    ok = False
    try:
        # [2] probe asset
        r = await http.post("/api/asset/equipment", headers=post_h, json={"data": {
            "type": "asset--equipment",
            "attributes": {"name": f"{TAG} spike asset", "status": "active"},
        }})
        print(f"[2] POST asset--equipment -> HTTP {r.status_code}")
        if r.status_code != 201:
            print(f"    body: {r.text[:300]}")
            return False
        asset_id = r.json()["data"]["id"]

        # [3] before inventory
        r = await http.get(f"/api/asset/equipment/{asset_id}", headers=get_h)
        before = inventory_of(r.json())
        print(f"[3] GET asset (before) -> HTTP {r.status_code}  inventory={before!r}")
        findings["before"] = before

        # [4] inventory adjustment quantity referencing the asset
        r = await http.post(f"/api/quantity/{bundle}", headers=post_h, json={"data": {
            "type": f"quantity--{bundle}",
            "attributes": {"measure": "count", "value": {"numerator": 5, "denominator": 1},
                           "inventory_adjustment": "increment"},
            "relationships": {"inventory_asset": {"data": {"type": "asset--equipment", "id": asset_id}}},
        }})
        print(f"[4] POST quantity--{bundle} (increment 5, inventory_asset rel) -> HTTP {r.status_code}")
        if r.status_code != 201:
            print(f"    body: {r.text[:400]}")
            findings[f"quantity_error_{bundle}"] = r.text[:400]
            return False
        qty_id = r.json()["data"]["id"]
        findings["quantity_fields"] = describe(r.json())

        # [5] done adjustment log carrying the quantity
        r = await http.post("/api/log/input", headers=post_h, json={"data": {
            "type": "log--input",
            "attributes": {"name": f"{TAG} spike input", "timestamp": int(time.time()), "status": "done"},
            "relationships": {
                "asset": {"data": [{"type": "asset--equipment", "id": asset_id}]},
                "quantity": {"data": [{"type": f"quantity--{bundle}", "id": qty_id}]},
            },
        }})
        print(f"[5] POST log--input (status done, quantity rel) -> HTTP {r.status_code}")
        if r.status_code != 201:
            print(f"    body: {r.text[:300]}")
            return False
        log_id = r.json()["data"]["id"]

        # [6] after inventory — did it recompute?
        r = await http.get(f"/api/asset/equipment/{asset_id}", headers=get_h)
        after = inventory_of(r.json())
        print(f"[6] GET asset (after)  -> HTTP {r.status_code}  inventory={after!r}")
        findings["after"] = after
        findings["bundle"] = bundle

        # [7-8] cleanup: delete the log first, then the quantity
        r = await http.delete(f"/api/log/input/{log_id}", headers=get_h)
        print(f"[7] DELETE log -> HTTP {r.status_code}")
        log_deleted = r.status_code == 204
        log_id = None if log_deleted else log_id
        r = await http.delete(f"/api/quantity/{bundle}/{qty_id}", headers=get_h)
        print(f"[8] DELETE quantity -> HTTP {r.status_code}")
        if r.status_code == 204:
            qty_id = None
        elif r.status_code == 404 and log_deleted:
            # farm_log_quantity's hook_log_delete cascade-deletes the quantities
            # a log references — confirm it is really gone.
            check = await http.get(f"/api/quantity/{bundle}/{qty_id}", headers=get_h)
            print(f"    quantity 404 after log delete (cascade) — GET confirms {check.status_code}")
            findings["quantity_cascade_deleted"] = check.status_code == 404
            qty_id = None if check.status_code == 404 else qty_id

        # [9] reverted?
        r = await http.get(f"/api/asset/equipment/{asset_id}", headers=get_h)
        reverted = inventory_of(r.json())
        print(f"[9] GET asset (after deletes) -> HTTP {r.status_code}  inventory={reverted!r}")
        findings["reverted"] = reverted
        ok = True
    finally:
        for label, path, uid in [
            ("log", f"/api/log/input/{log_id}", log_id),
            ("quantity", f"/api/quantity/{bundle}/{qty_id}", qty_id),
            ("asset", f"/api/asset/equipment/{asset_id}", asset_id),
        ]:
            if uid is None:
                continue
            r = await http.delete(path, headers=get_h)
            if r.status_code == 204:
                print(f"    cleanup: {label} {uid} deleted")
            elif r.status_code == 404:
                print(f"    cleanup: {label} {uid} already gone (404) — cascade-deleted with its log")
            else:
                leftovers.append(f"{label} {uid} (HTTP {r.status_code})")
    return ok


async def main() -> int:
    load_dotenv(PROJECT / ".env")
    base = (os.environ.get("FARMOS_URL") or "").strip()
    if base and not base.startswith(("http://", "https://")):
        base = "http://" + base

    findings: dict = {"bundle": None, "quantity_fields": None, "before": None,
                      "after": None, "reverted": None, "quantity_cascade_deleted": None}
    leftovers: list[str] = []

    async with httpx.AsyncClient(base_url=base, timeout=30, follow_redirects=True) as http:
        tok = await http.post("/oauth/token", data={
            "grant_type": "password",
            "client_id": os.environ.get("FARMOS_CLIENT_ID", "farm"),
            "username": os.environ["FARMOS_USERNAME"],
            "password": os.environ["FARMOS_PASSWORD"],
        })
        if tok.status_code != 200:
            print(f"auth failed: HTTP {tok.status_code}: {tok.text[:200]}")
            return 1
        token = tok.json()["access_token"]
        get_h, post_h = auth_headers(token), post_headers(token)

        # [1] inspect an existing quantity resource (read-only)
        r = await http.get("/api/quantity/standard", headers=get_h, params={"page[limit]": "1"})
        print(f"[1] GET existing quantity--standard -> HTTP {r.status_code}")
        if r.status_code == 200:
            data = (r.json().get("data") or [])
            if data:
                print(f"    existing quantity: {describe(r.json())}")
                print(f"    sample: {json.dumps(data[0])[:700]}")
            else:
                print("    (no existing quantity--standard resources on this farm)")
        else:
            print(f"    body: {r.text[:200]}")

        for bundle in ("standard", "material"):
            if await run_probe(http, get_h, post_h, bundle, findings, leftovers):
                break

    # ---- summary ------------------------------------------------------------
    print("\n" + "=" * 72)
    print("SPIKE FINDINGS")
    print("=" * 72)
    worked = findings["bundle"] is not None
    print(f"(a) quantity fields that worked: {findings['quantity_fields'] or 'NONE — see errors above'}")
    print(f"    shape used: attributes measure/count, value={{numerator: 5, denominator: 1}}, "
          "inventory_adjustment='increment'; relationship inventory_asset -> asset")
    print(f"(b) inventory recomputed: before={findings['before']!r} -> after={findings['after']!r} "
          f"({'YES' if worked else 'NO'})")
    print(f"(c) quantity bundle that works: {findings['bundle'] or 'NEITHER standard nor material'}")
    print(f"(d) inventory reverted after log+quantity delete: {findings['reverted']!r} "
          f"({'YES' if worked and not findings['reverted'] else 'NO'})")
    cascade = findings.get("quantity_cascade_deleted")
    print(f"    cascade behavior: deleting the log also deletes its quantities "
          f"(farm_log_quantity hook_log_delete) — confirmed={cascade}")
    print(f"(e) leftover records: {leftovers or 'none'}")
    if not worked:
        return 1
    return 0 if not leftovers else 1


if __name__ == "__main__":
    sys_exit = asyncio.run(main())
    raise SystemExit(sys_exit)
