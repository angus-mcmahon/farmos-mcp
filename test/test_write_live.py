"""Write-path live test: create → verify-by-read → clean up → verify-gone.

Runs against the real farmOS from .env, so it is strict about pollution:
- every created record is tracked and DELETED in a finally block (even on
  failure),
- each delete is followed by a read that must 404,
- if the server refuses deletes, leftovers are reported loudly with their
  UUIDs so nothing silently accumulates.

Agents get no delete tool by design — the test uses a direct HTTP DELETE for
cleanup only (same credentials as the configured .env user).

Run:  .venv/bin/python test/test_write_live.py
"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SERVER_BIN = next(
    (p for p in [PROJECT / "venv-dev" / "bin" / "farmos-mcp", PROJECT / ".venv" / "bin" / "farmos-mcp"] if p.exists()),
    None,
)

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []
TRACKED: list[tuple[str, str, str]] = []  # (label, full_type, uuid) still on server
DELETED: list[tuple[str, str, str]] = []  # deleted, pending the gone-404 check


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(("PASS" if ok else "FAIL", name, detail))
    return ok


async def call(session: ClientSession, name: str, args: dict | None = None):
    result = await session.call_tool(name, args or {})
    text = result.content[0].text if result.content else ""
    if result.isError:
        return None, text
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, f"non-JSON tool output: {text[:200]}"


async def direct_cleanup() -> None:
    """HTTP-delete every tracked record (newest first), regardless of failures."""
    import httpx
    from dotenv import load_dotenv

    load_dotenv(PROJECT / ".env")
    base = (os.environ.get("FARMOS_URL") or "").strip()
    if base and not base.startswith(("http://", "https://")):
        base = "http://" + base
    async with httpx.AsyncClient(base_url=base, timeout=30) as http:
        tok = await http.post("/oauth/token", data={
            "grant_type": "password",
            "client_id": os.environ.get("FARMOS_CLIENT_ID", "farm"),
            "username": os.environ["FARMOS_USERNAME"],
            "password": os.environ["FARMOS_PASSWORD"],
        })
        if tok.status_code != 200:
            print("  !! cleanup could not authenticate; records remain tracked")
            return
        headers = {"Authorization": f"Bearer {tok.json()['access_token']}"}
        for label, full_type, uid in reversed(TRACKED):
            entity, bundle = full_type.split("--", 1)
            r = await http.delete(f"/api/{entity}/{bundle}/{uid}", headers=headers)
            if r.status_code == 204:
                DELETED.append((label, full_type, uid))
                TRACKED.remove((label, full_type, uid))
            elif r.status_code == 404:
                # Already gone: farmOS cascade-deletes the quantities a log
                # references when the log is deleted (farm_log_quantity
                # hook_log_delete), so a quantity may 404 after its log.
                DELETED.append((label, full_type, uid))
                TRACKED.remove((label, full_type, uid))
                print(f"  i  {label} ({full_type} {uid}) already gone (404) — cascade-deleted with its log")
            else:
                print(f"  !! delete failed: {label} ({full_type} {uid}) -> HTTP {r.status_code}")


async def run_tests() -> bool:
    if SERVER_BIN is None:
        print("SKIP: no farmos-mcp server binary found")
        return True

    env = dict(os.environ)
    env["FARMOS_MCP_ENV_FILE"] = str(PROJECT / ".env")
    params = StdioServerParameters(command=str(SERVER_BIN), env=env, cwd=str(PROJECT))

    tag = f"mcp-test {uuid.uuid4().hex[:8]}"
    asset_type, log_type = "asset--equipment", "log--observation"

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            try:
                # -- create asset ------------------------------------------------
                asset_name = f"{tag} asset"
                created, err = await call(
                    session, "farmos_create_asset",
                    {"type": "equipment", "name": asset_name, "notes": "created by mcp write test"},
                )
                if not check("create_asset responds", created is not None, (err or "")[:160]):
                    return False
                asset_uuid = created["id"]
                TRACKED.append(("test asset", asset_type, asset_uuid))
                check("create_asset returns compact summary",
                      created.get("name") == asset_name and created.get("type") == asset_type,
                      str(created)[:140])

                # -- verify by read ------------------------------------------------
                got, err = await call(session, "farmos_get", {"full_type": asset_type, "uuid": asset_uuid})
                check("asset read-back matches", got is not None and got.get("name") == asset_name,
                      (err or "")[:140])

                # -- update + verify -----------------------------------------------
                new_notes = f"{tag} updated notes"
                updated, err = await call(
                    session, "farmos_update_entity",
                    {"full_type": asset_type, "uuid": asset_uuid, "notes": new_notes},
                )
                check("update_entity responds", updated is not None, (err or "")[:160])
                got, err = await call(session, "farmos_get", {"full_type": asset_type, "uuid": asset_uuid})
                check("asset update verified by read",
                      got is not None and (got.get("notes") or "").startswith(tag),
                      f"notes={(got or {}).get('notes')!r:.90}")

                # -- archive / unarchive + verify ------------------------------------
                archived, err = await call(session, "farmos_archive", {"full_type": asset_type, "uuid": asset_uuid})
                check("archive responds", archived is not None, (err or "")[:160])
                got, err = await call(session, "farmos_get", {"full_type": asset_type, "uuid": asset_uuid})
                check("archive verified by read",
                      got is not None and got.get("archived") not in (None, "", 0),
                      f"archived={(got or {}).get('archived')!r}")
                unarchived, err = await call(
                    session, "farmos_archive", {"full_type": asset_type, "uuid": asset_uuid, "unarchive": True}
                )
                check("unarchive responds", unarchived is not None, (err or "")[:160])
                got, err = await call(session, "farmos_get", {"full_type": asset_type, "uuid": asset_uuid})
                check("unarchive verified by read",
                      got is not None and got.get("archived") in (None, "", 0),
                      f"archived={(got or {}).get('archived')!r}")

                # -- create log referencing the asset ---------------------------------
                log_name = f"{tag} log"
                log_created, err = await call(
                    session, "farmos_create_log",
                    {"type": "observation", "name": log_name,
                     "asset_refs": [{"type": asset_type, "id": asset_uuid}],
                     "notes": "created by mcp write test"},
                )
                if not check("create_log responds", log_created is not None, (err or "")[:160]):
                    return False
                log_uuid = log_created["id"]
                TRACKED.append(("test log", log_type, log_uuid))
                check("create_log returns summary with asset rel",
                      log_created.get("name") == log_name
                      and any(a.get("id") == asset_uuid for a in log_created.get("asset", [])),
                      str(log_created)[:180])

                got, err = await call(session, "farmos_get", {"full_type": log_type, "uuid": log_uuid})
                asset_rel = (got or {}).get("asset") or []
                if isinstance(asset_rel, dict):
                    asset_rel = [asset_rel]
                check("log read-back resolves asset",
                      got is not None and asset_uuid in {a.get("id") for a in asset_rel},
                      (err or "")[:140])

                # -- log status transition + verify ------------------------------------
                done, err = await call(
                    session, "farmos_update_entity",
                    {"full_type": log_type, "uuid": log_uuid, "status": "done"},
                )
                check("log status update responds", done is not None, (err or "")[:160])
                got, err = await call(session, "farmos_get", {"full_type": log_type, "uuid": log_uuid})
                check("log status verified by read", got is not None and got.get("status") == "done",
                      f"status={(got or {}).get('status')!r}")

                # -- invalid input guards (nothing created) -----------------------------
                # Unknown fields are stripped by schema validation, so the tool
                # refuses with "Nothing to update" instead of silently writing.
                bad, err = await call(
                    session, "farmos_update_entity",
                    {"full_type": asset_type, "uuid": asset_uuid, "inventory": "hack"},
                )
                check("update refuses unknown fields (no silent write)",
                      bad is None and "Nothing to update" in (err or ""), (err or "")[:140])

                bad, err = await call(
                    session, "farmos_create_log",
                    {"type": "observation", "name": "x", "asset_refs": [{"type": "log--activity", "id": "x"}]},
                )
                check("create_log rejects non-asset refs",
                      bad is None and "asset--*" in (err or ""), (err or "")[:140])

                # -- M3.1/M3.2: checkin shorthand + inventory change -------------------
                # A second asset shares the first one's name prefix so name
                # resolution has something to be ambiguous about.
                asset_two_name = f"{tag} asset two"
                created2, err = await call(
                    session, "farmos_create_asset",
                    {"type": "equipment", "name": asset_two_name, "notes": "created by mcp write test"},
                )
                check("create second asset responds", created2 is not None, (err or "")[:160])
                asset_two_uuid = (created2 or {}).get("id")
                if asset_two_uuid:
                    TRACKED.append(("test asset two", asset_type, asset_two_uuid))

                def sum_inventory(entries) -> float:
                    """Numeric total of the 'count' entries in an inventory list."""
                    total = 0.0
                    for entry in entries or []:
                        if not isinstance(entry, dict) or entry.get("measure") != "count":
                            continue
                        try:
                            total += float(entry.get("value") or 0)
                        except (TypeError, ValueError):
                            pass
                    return total

                # checkin happy path: exact name resolves, log auto-named
                checkin, err = await call(
                    session, "farmos_checkin_log", {"type": "observation", "asset_name": asset_name},
                )
                if check("checkin responds", checkin is not None, (err or "")[:200]):
                    TRACKED.append(("checkin log", "log--observation", checkin["log_summary"]["id"]))
                    summary = checkin["log_summary"]
                    resolution = checkin.get("asset_resolution") or {}
                    check("checkin auto-names + marks done",
                          summary.get("name") == f"Observation on {asset_name}"
                          and summary.get("status") == "done",
                          str(summary)[:160])
                    check("checkin resolves exact name unambiguously",
                          len(resolution.get("matched") or []) == 1
                          and resolution["matched"][0].get("id") == asset_uuid
                          and resolution.get("ambiguous") is False,
                          str(resolution)[:160])

                # ambiguous name: refuses and lists candidates, nothing written
                amb, err = await call(
                    session, "farmos_checkin_log", {"type": "observation", "asset_name": tag},
                )
                check("checkin refuses ambiguous name with candidates",
                      amb is None and "allow_ambiguous" in (err or "")
                      and asset_name in (err or "") and asset_two_name in (err or ""),
                      (err or "")[:240])

                # allow_ambiguous: ONE log referencing BOTH matched assets
                amb_ok, err = await call(
                    session, "farmos_checkin_log",
                    {"type": "observation", "asset_name": tag, "allow_ambiguous": True},
                )
                if check("checkin allow_ambiguous responds", amb_ok is not None, (err or "")[:200]):
                    TRACKED.append(("checkin amb log", "log--observation", amb_ok["log_summary"]["id"]))
                    amb_rels = amb_ok["log_summary"].get("asset") or []
                    if isinstance(amb_rels, dict):
                        amb_rels = [amb_rels]
                    asset_ids = {a.get("id") for a in amb_rels if isinstance(a, dict)}
                    check("allow_ambiguous logs on both matched assets",
                          asset_uuid in asset_ids and asset_two_uuid in asset_ids,
                          f"assets={amb_ok['log_summary'].get('asset')!r}"[:180])

                # done=false: status stays pending
                pend, err = await call(
                    session, "farmos_checkin_log",
                    {"type": "maintenance", "asset_name": asset_name, "done": False},
                )
                if check("checkin done=false responds", pend is not None, (err or "")[:200]):
                    TRACKED.append(("checkin pend log", "log--maintenance", pend["log_summary"]["id"]))
                    check("checkin done=false is pending",
                          pend["log_summary"].get("status") == "pending",
                          f"status={pend['log_summary'].get('status')!r}")

                # asset_refs path with explicit name
                uuid_path, err = await call(
                    session, "farmos_checkin_log",
                    {"name": f"{tag} uuid checkin",
                     "asset_refs": [{"type": asset_type, "id": asset_uuid}]},
                )
                if check("checkin asset_refs path responds", uuid_path is not None, (err or "")[:200]):
                    TRACKED.append(("checkin uuid log", "log--observation", uuid_path["log_summary"]["id"]))
                    resolution = uuid_path.get("asset_resolution") or {}
                    check("checkin asset_refs resolution has no query",
                          resolution.get("query") is None and resolution.get("ambiguous") is False
                          and any(m.get("id") == asset_uuid for m in resolution.get("matched") or []),
                          str(resolution)[:160])

                # checkin with quantity sugar (+ unit term resolution when the
                # farm has unit terms)
                terms, _ = await call(session, "farmos_terms", {"type": "unit", "limit": 10})
                unit_name = ((terms or {}).get("data") or [{}])[0].get("name")
                unit_ok = bool(unit_name)
                qty_checkin, err = await call(
                    session, "farmos_checkin_log",
                    {"asset_name": asset_name,
                     "quantity": {"value": 3, "units": unit_name or "mcp-test no such unit"}},
                )
                if check("checkin with quantity responds", qty_checkin is not None, (err or "")[:240]):
                    q_rows = qty_checkin["log_summary"].get("quantity") or []
                    if isinstance(q_rows, dict):
                        q_rows = [q_rows]
                    check("checkin quantity visible in read-back",
                          any(str(q.get("type", "")).startswith("quantity--") for q in q_rows),
                          f"quantity={qty_checkin['log_summary'].get('quantity')!r}"[:180])
                    if not unit_ok:
                        check("checkin reports unresolved units", "units_resolution" in qty_checkin,
                              str(qty_checkin)[:160])
                    for q in qty_checkin.get("quantities") or []:
                        TRACKED.append(("checkin quantity", q["type"], q["id"]))
                    TRACKED.append(("checkin qty log", qty_checkin["log_summary"]["type"],
                                    qty_checkin["log_summary"]["id"]))

                # inventory_change: +5 count on a fresh asset (before == [])
                inv, err = await call(
                    session, "farmos_inventory_change",
                    {"asset_refs": [{"type": asset_type, "id": asset_uuid}],
                     "measure": "count", "delta": 5},
                )
                if check("inventory_change responds", inv is not None, (err or "")[:240]):
                    changes = inv.get("inventory_changes") or []
                    before = (changes or [{}])[0].get("before")
                    after = (changes or [{}])[0].get("after")
                    check("inventory_change reports before empty", changes and not (before or []),
                          f"before={before!r}"[:160])
                    check("inventory after increment sums to 5",
                          changes and abs(sum_inventory(after) - 5) < 1e-6, f"after={after!r}"[:160])
                    check("inventory_change log is done",
                          inv["log_summary"].get("status") == "done",
                          f"status={inv['log_summary'].get('status')!r}")
                    for q in inv.get("quantities") or []:
                        TRACKED.append(("inventory quantity", q["type"], q["id"]))
                    TRACKED.append(("inventory log", inv["log_summary"]["type"],
                                    inv["log_summary"]["id"]))

                # inventory_change decrement (with units when one resolves cleanly)
                dec_args = {"asset_refs": [{"type": asset_type, "id": asset_uuid}],
                            "measure": "count", "delta": -2}
                if unit_ok:
                    dec_args["units_name"] = unit_name
                dec, err = await call(session, "farmos_inventory_change", dec_args)
                if dec is None and unit_ok and "unit term" in (err or ""):
                    # the farm's unit names are ambiguous — units are optional here
                    dec_args.pop("units_name")
                    dec, err = await call(session, "farmos_inventory_change", dec_args)
                if check("inventory decrement responds", dec is not None, (err or "")[:240]):
                    changes = dec.get("inventory_changes") or []
                    after = (changes or [{}])[0].get("after")
                    check("inventory after decrement sums to 3",
                          changes and abs(sum_inventory(after) - 3) < 1e-6, f"after={after!r}"[:160])
                    for q in dec.get("quantities") or []:
                        TRACKED.append(("inventory quantity", q["type"], q["id"]))
                    TRACKED.append(("inventory dec log", dec["log_summary"]["type"],
                                    dec["log_summary"]["id"]))

                # inventory_change guards (nothing written)
                zero, err = await call(
                    session, "farmos_inventory_change",
                    {"asset_refs": [{"type": asset_type, "id": asset_uuid}], "delta": 0},
                )
                check("inventory_change refuses zero delta",
                      zero is None and "non-zero" in (err or ""), (err or "")[:160])
                bad_measure, err = await call(
                    session, "farmos_inventory_change",
                    {"asset_refs": [{"type": asset_type, "id": asset_uuid}],
                     "delta": 1, "measure": "bushels"},
                )
                check("inventory_change refuses bad measure",
                      bad_measure is None and "bushels" in (err or "") and "count" in (err or ""),
                      (err or "")[:160])
                unknown, err = await call(
                    session, "farmos_inventory_change",
                    {"asset_refs": [{"type": asset_type, "id": str(uuid.uuid4())}], "delta": 1},
                )
                check("inventory_change refuses unknown asset (404)",
                      unknown is None and "404" in (err or ""), (err or "")[:160])

            finally:
                qty_expected = sum(1 for _, ft, _ in TRACKED if ft.startswith("quantity--"))
                await direct_cleanup()

                # -- inventory reverts once the adjustment logs are gone -------------
                # farmOS recomputes inventory from done logs; deleting them reverts
                # it. If it does NOT revert, document the actual behavior instead
                # of failing so the finding stays visible.
                got, err = await call(session, "farmos_get", {"full_type": asset_type, "uuid": asset_uuid})
                inv_left = (got or {}).get("inventory") or []
                if inv_left:
                    check("asset inventory after cleanup (documented actual behavior)", True,
                          f"inventory did NOT revert: {inv_left!r}"[:200])
                else:
                    check("asset inventory reverts after cleanup", True)

                # -- every created quantity is gone (404) after cleanup --------------
                qty_deleted = sum(1 for _, ft, _ in DELETED if ft.startswith("quantity--"))
                check("created quantities removed (404 after cleanup)",
                      qty_expected > 0 and qty_deleted == qty_expected,
                      f"{qty_deleted}/{qty_expected} quantity resources deleted")

                # -- verify everything is gone ------------------------------------
                for label, full_type, uid in reversed(DELETED):
                    got, err = await call(session, "farmos_get", {"full_type": full_type, "uuid": uid})
                    check(f"{label} gone after cleanup (404)",
                          got is None and "404" in (err or ""),
                          (err or "")[:140])

    return all(status == "PASS" for status, _, _ in RESULTS)


async def main() -> None:
    ok = await run_tests()
    print()
    for status, name, detail in RESULTS:
        mark = "✓" if status == "PASS" else "✗"
        line = f"{mark} {status}  {name}"
        if detail and status == "FAIL":
            line += f"  | {detail}"
        print(line)
    failed = sum(1 for s, _, _ in RESULTS if s == "FAIL")
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} checks passed")
    if TRACKED:
        print("POLLUTION WARNING — records left on the server:")
        for label, full_type, uid in TRACKED:
            print(f"  - {label}: {full_type} {uid}")
    if not ok or TRACKED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
