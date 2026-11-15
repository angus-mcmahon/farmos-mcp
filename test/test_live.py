"""Live integration tests: drive the real MCP server against a real farmOS.

Launches the farmos-mcp server over stdio (exactly like VS Code does) and
calls every tool, asserting shapes and error behavior. No credentials are
printed; config comes from the project .env (discovered like a normal launch).

Run:  .venv/bin/python test/test_live.py
Exits non-zero if any check fails. Skips cleanly if the server is unreachable.
"""

import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

# Prefer the venv VS Code uses, fall back to .venv.
SERVER_BIN = next(
    (p for p in [PROJECT / "venv-dev" / "bin" / "farmos-mcp", PROJECT / ".venv" / "bin" / "farmos-mcp"] if p.exists()),
    None,
)

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []  # (status, name, detail)


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(("PASS" if ok else "FAIL", name, detail))
    return ok


async def call(session: ClientSession, name: str, args: dict | None = None):
    """Call a tool; return (payload_dict, error_text_or_None)."""
    result = await session.call_tool(name, args or {})
    text = result.content[0].text if result.content else ""
    if result.isError:
        return None, text
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, f"non-JSON tool output: {text[:200]}"


async def run_tests() -> bool:
    env = dict(os.environ)
    env["FARMOS_MCP_ENV_FILE"] = str(PROJECT / ".env")
    params = StdioServerParameters(command=str(SERVER_BIN), env=env, cwd=str(PROJECT))

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            check(
                "server initializes",
                init.serverInfo.name == "farmos-mcp",
                f"{init.serverInfo.name} v{init.serverInfo.version}",
            )

            # -- farmos_info ------------------------------------------------
            info, err = await call(session, "farmos_info")
            check("farmos_info responds", info is not None, (err or "")[:120])
            if info:
                check("farmos_info has farm_name", bool(info.get("farm_name")), str(info.get("farm_name")))
                check("farmos_info has version", bool(info.get("farmos_version")), str(info.get("farmos_version")))
                check(
                    "farmos_info has system_of_measurement",
                    info.get("system_of_measurement") in {"metric", "us_customary", None},
                    str(info.get("system_of_measurement")),
                )

            # -- farmos_list_resources -------------------------------------
            resources, err = await call(session, "farmos_list_resources")
            check("list_resources responds", resources is not None, (err or "")[:120])
            if resources:
                check("list_resources finds types", resources.get("count", 0) > 0,
                      f"{resources.get('count')} types")
                all_types = resources.get("types", [])
                check("types use entity--bundle form", all("--" in t for t in all_types[:10]))

            asset_res, err = await call(session, "farmos_list_resources", {"entity": "asset"})
            check("list_resources filters by entity", asset_res is not None and asset_res.get("count", 0) > 0)
            asset_bundles = [t.split("--", 1)[1] for t in (asset_res or {}).get("types", [])]
            check("asset bundles include land/plant", {"land", "plant"} <= set(asset_bundles),
                  f"bundles: {', '.join(asset_bundles[:8])}…")

            # -- farmos_assets (all bundles) --------------------------------
            assets, err = await call(session, "farmos_assets", {"limit": 10})
            check("assets search responds", assets is not None, (err or "")[:120])
            first_asset = None
            if assets:
                items = assets.get("data", [])
                check("assets summary compact", all(("id" in a and "type" in a) for a in items[:10]))
                check("assets payload size sane", len(json.dumps(assets)) < 20_000,
                      f"{len(json.dumps(assets))} bytes for {len(items)} assets")
                first_asset = items[0] if items else None

            # -- farmos_assets (single bundle + filters) --------------------
            if asset_bundles:
                bundle = asset_bundles[0]
                single, err = await call(session, "farmos_assets", {"type": bundle, "limit": 5})
                check(f"assets by bundle '{bundle}' responds", single is not None, (err or "")[:120])

                nomatch, err = await call(session, "farmos_assets", {"name_contains": "zzz_no_such_asset_xyz", "limit": 5})
                check("name_contains filter works", nomatch is not None and nomatch.get("count") == 0)

                if first_asset and first_asset.get("type", "").startswith("asset--"):
                    parent_id = None
                    parent = first_asset.get("parent")
                    if isinstance(parent, dict):
                        parent_id = parent.get("id")
                    if parent_id:
                        kids, err = await call(session, "farmos_assets", {"parent_id": parent_id, "limit": 10})
                        check("parent_id filter responds", kids is not None, (err or "")[:120])

            # -- farmos_get ---------------------------------------------------
            if first_asset:
                got, err = await call(session, "farmos_get", {"full_type": first_asset["type"], "uuid": first_asset["id"]})
                check("farmos_get returns the asset", got is not None and got.get("id") == first_asset["id"], (err or "")[:120])

                # A well-formed but non-existent UUID (farmOS reports these 404s).
                missing_uuid = str(uuid.uuid4())
                missing, err = await call(
                    session, "farmos_get",
                    {"full_type": first_asset["type"], "uuid": missing_uuid},
                )
                check(
                    "farmos_get unknown uuid -> clean 404",
                    missing is None and "404" in (err or "") and "not found" in (err or "").lower(),
                    (err or "")[:160],
                )
            else:
                check("farmos_get exercised", False, "no assets on server to fetch")

            # -- farmos_logs ---------------------------------------------------
            logs, err = await call(session, "farmos_logs", {"limit": 10})
            check("logs search responds", logs is not None, (err or "")[:120])
            if logs:
                log_items = logs.get("data", [])
                if log_items:
                    l0 = log_items[0]
                    check("log summary has asset rel", "asset" in l0 or l0.get("type", "").startswith("log--"))
                    # the fan-out merge must honor the -timestamp sort (newest first)
                    ts_list = [l.get("timestamp") or "" for l in log_items]
                    check("logs sorted newest first", ts_list == sorted(ts_list, reverse=True),
                          f"first three: {ts_list[:3]}")
                # window filter: broad range must actually return this farm's
                # logs (catches the epoch-vs-ISO timestamp filter regression)
                window, err = await call(
                    session, "farmos_logs",
                    {"timestamp_after": "2000-01-01", "timestamp_before": "2030-01-01", "limit": 5},
                )
                check("timestamp window filter responds", window is not None, (err or "")[:120])
                check("timestamp window returns data", (window or {}).get("count", 0) >= 5,
                      f"count={(window or {}).get('count')}")

                # a narrow empty window must return exactly zero (filter works)
                empty_window, err = await call(
                    session, "farmos_logs",
                    {"timestamp_after": "2031-01-01", "timestamp_before": "2031-12-01", "limit": 5},
                )
                check("empty window returns exactly 0",
                      empty_window is not None and empty_window.get("count") == 0,
                      f"count={(empty_window or {}).get('count') if empty_window else err}")

                # the current year's window is the agent-facing use case
                y2026, err = await call(
                    session, "farmos_logs",
                    {"timestamp_after": "2026-01-01", "timestamp_before": "2027-01-01", "limit": 50},
                )
                check("2026 window returns data", (y2026 or {}).get("count", 0) >= 1,
                      f"count={(y2026 or {}).get('count') if y2026 else err}")
                # pagination must return *different* records, not re-slice page 1
                if logs.get("count", 0) == 10 and "next_offset" in logs:
                    page2, err = await call(session, "farmos_logs", {"limit": 10, "offset": logs["next_offset"]})
                    check("logs pagination responds", page2 is not None, (err or "")[:120])
                    p1_ids = {i["id"] for i in log_items}
                    p2_ids = {i["id"] for i in (page2 or {}).get("data", [])}
                    check("logs pagination returns new records",
                          page2 is not None and (not p2_ids or not (p1_ids & p2_ids)),
                          f"overlap={len(p1_ids & p2_ids)}" if p2_ids else "page2 empty")

            # -- farmos_summary ----------------------------------------------
            summary, err = await call(
                session, "farmos_summary",
                {"timestamp_after": "2026-01-01", "timestamp_before": "2027-01-01", "group_by": "month"},
            )
            check("summary responds", summary is not None, (err or "")[:120])
            if summary:
                total = summary.get("total", 0)
                check("summary 2026 finds logs", total >= 1, f"total={total}")
                month_sum = sum(summary.get("by_month", {}).values())
                check("summary months sum to total", month_sum == total,
                      f"{month_sum} != {total}")
                type_sum = sum(summary.get("by_type", {}).values())
                check("summary by_type sums to total", type_sum == total,
                      f"{type_sum} != {total}")
                status_sum = sum(summary.get("status", {}).values())
                check("summary status sums to total", status_sum == total)
                check("summary is compact", len(json.dumps(summary)) < 3000,
                      f"{len(json.dumps(summary))} bytes")
                check("summary reports scan", "scanned" in summary and "truncated" in summary)

            # group_by=asset must resolve names via included resources
            by_asset, err = await call(
                session, "farmos_summary",
                {"timestamp_after": "2026-01-01", "timestamp_before": "2027-01-01",
                 "group_by": "asset", "top": 5},
            )
            check("summary by asset responds", by_asset is not None, (err or "")[:120])
            if by_asset:
                names = [a.get("name") for a in by_asset.get("top_assets", [])]
                check("summary top_assets have names", bool(names) and all(names),
                      f"{names[:3]}")

            # narrow empty window must be exactly zero
            empty_sum, err = await call(
                session, "farmos_summary",
                {"timestamp_after": "2031-01-01", "timestamp_before": "2031-12-01"},
            )
            check("summary empty window is 0",
                  empty_sum is not None and empty_sum.get("total") == 0,
                  f"total={(empty_sum or {}).get('total') if empty_sum else err}")

            # single-bundle + status filter consistency
            bundle_summary, err = await call(
                session, "farmos_summary",
                {"type": "activity", "timestamp_after": "2026-01-01",
                 "timestamp_before": "2027-01-01", "group_by": "type"},
            )
            check("summary single bundle responds", bundle_summary is not None, (err or "")[:120])
            if bundle_summary:
                check("summary single bundle only has that type",
                      set(bundle_summary.get("by_type", {})) <= {"activity"},
                      str(bundle_summary.get("by_type")))

            # -- farmos_summary v2: quantity rollups + interval ---------------
            qsum, err = await call(
                session, "farmos_summary",
                {"timestamp_after": "2026-01-01", "timestamp_before": "2027-01-01",
                 "quantities": True},
            )
            check("summary v2 quantities responds", qsum is not None, (err or "")[:120])
            qty_totals = (qsum or {}).get("quantity_totals", [])
            valid_measures = {
                "count", "length", "weight", "area", "volume", "time", "temperature",
                "pressure", "water_content", "value", "rate", "rating", "ratio",
                "probability", "speed",
            }
            check("quantity_totals is a list", isinstance(qty_totals, list),
                  f"{len(qty_totals)} entries")
            check("quantity entries have measure/unit/total",
                  all(isinstance(t, dict) and {"measure", "unit", "total"} <= set(t) for t in qty_totals),
                  str(qty_totals[:2]))
            check("quantity measures are valid",
                  all(t.get("measure") in valid_measures for t in qty_totals),
                  str(sorted({t.get("measure", "?") for t in qty_totals})))
            check("summary v2 payload compact",
                  qsum is not None and len(json.dumps(qsum)) < 3000,
                  f"{len(json.dumps(qsum)) if qsum else -1} bytes")

            # cross-check measures against raw logs for the same window
            raw_measures: set[str] = set()
            offset = 0
            for _ in range(3):  # a couple of pages is enough for the probe
                raw, err = await call(
                    session, "farmos_logs",
                    {"timestamp_after": "2026-01-01", "timestamp_before": "2027-01-01",
                     "limit": 50, "offset": offset},
                )
                if raw is None:
                    break
                for item in raw.get("data", []):
                    for q in item.get("quantity") or []:
                        nm = str(q.get("name") or "")
                        if ":" in nm:
                            raw_measures.add(nm.split(":", 1)[0].strip())
                if "next_offset" not in raw or raw["next_offset"] == offset:
                    break
                offset = raw["next_offset"]
            if raw_measures:
                summary_measures = {t["measure"] for t in qty_totals}
                check("summary measures cover raw-log measures", summary_measures >= raw_measures,
                      f"summary={sorted(summary_measures)} raw={sorted(raw_measures)}")
                dominant = max((t for t in qty_totals if t["measure"] in raw_measures),
                               key=lambda t: t["total"], default=None)
                if dominant:
                    dom_sum = sum(t["total"] for t in qty_totals if t["measure"] == dominant["measure"])
                    check(f"dominant measure '{dominant['measure']}' totals > 0",
                          dom_sum > 0, f"sum={dom_sum}")
            else:
                check("no raw quantities -> empty quantity_totals", qty_totals == [], str(qty_totals))

            # interval=week: primary ISO-week time series
            week_sum, err = await call(
                session, "farmos_summary",
                {"timestamp_after": "2026-01-01", "timestamp_before": "2027-01-01",
                 "interval": "week"},
            )
            check("summary interval=week responds", week_sum is not None, (err or "")[:120])
            if week_sum:
                bi = week_sum.get("by_interval", {})
                check("week labels are ISO weeks",
                      bool(bi) and all(re.fullmatch(r"\d{4}-W\d{2}", k) for k in bi),
                      str(list(bi)[:2]))
                check("week buckets sum to total", sum(bi.values()) == week_sum.get("total"),
                      f"{sum(bi.values())} != {week_sum.get('total')}")
                check("interval=week replaces by_month", "by_month" not in week_sum,
                      str(week_sum.get("by_month")))
                check("interval=week payload compact", len(json.dumps(week_sum)) < 3000,
                      f"{len(json.dumps(week_sum))} bytes")

            # interval=day: date-keyed time series
            day_sum, err = await call(
                session, "farmos_summary",
                {"timestamp_after": "2026-01-01", "timestamp_before": "2027-01-01",
                 "interval": "day"},
            )
            check("summary interval=day responds", day_sum is not None, (err or "")[:120])
            if day_sum:
                di = day_sum.get("by_interval", {})
                check("day labels are dates",
                      bool(di) and all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", k) for k in di),
                      str(list(di)[:2]))
                check("day buckets sum to total", sum(di.values()) == day_sum.get("total"),
                      f"{sum(di.values())} != {day_sum.get('total')}")

            # group_by + interval compose (by_interval as a secondary series)
            both, err = await call(
                session, "farmos_summary",
                {"timestamp_after": "2026-01-01", "timestamp_before": "2027-01-01",
                 "group_by": "type", "interval": "week"},
            )
            check("summary group_by type + interval week responds", both is not None, (err or "")[:120])
            if both:
                check("type+week has both series", "by_type" in both and "by_interval" in both,
                      str(sorted(both)))

            # invalid interval must raise, listing the valid values
            bad_iv, err = await call(
                session, "farmos_summary",
                {"timestamp_after": "2026-01-01", "timestamp_before": "2027-01-01",
                 "interval": "fortnight"},
            )
            check("invalid interval -> error listing values",
                  bad_iv is None and "week" in (err or "") and "day" in (err or ""),
                  (err or "")[:160])

            # empty window with quantities=true: nothing to roll up
            empty_q, err = await call(
                session, "farmos_summary",
                {"timestamp_after": "1999-01-01", "timestamp_before": "1999-12-31",
                 "quantities": True},
            )
            check("summary empty window quantities empty",
                  empty_q is not None and empty_q.get("quantity_totals") == []
                  and empty_q.get("total") == 0,
                  str(empty_q or err))

            # -- farmos_terms ------------------------------------------------
            terms_any = False
            for vocab in ("plant_type", "season", "log_category", "animal_type", "unit"):
                terms, err = await call(session, "farmos_terms", {"type": vocab, "limit": 5})
                if terms is not None:
                    terms_any = True
                    if terms.get("count", 0) > 0:
                        check(f"terms '{vocab}' return names", any("name" in t for t in terms.get("data", [])))
                        break
            check("farmos_terms reaches at least one vocabulary", terms_any)

            # -- error handling ----------------------------------------------
            bad_type, err = await call(session, "farmos_assets", {"type": "spaceship"})
            check("bad asset bundle -> suggestion error", bad_type is None and ("Unknown" in (err or "") or "Did you mean" in (err or "")), (err or "")[:120])

            bad_vocab, err = await call(session, "farmos_terms", {"type": "zodiac_signs"})
            check("bad vocabulary -> suggestion error", bad_vocab is None and "Unknown" in (err or ""), (err or "")[:120])

            # -- farmos_get batch mode (M3.5) ----------------------------------
            # Pick a bundle holding >=2 assets so every batch row shares one type.
            batch_type, batch_ids = None, []
            for b in asset_bundles:
                bres, _ = await call(session, "farmos_assets", {"type": b, "limit": 5})
                pairs = [(a["id"], a["type"]) for a in (bres or {}).get("data", []) if a.get("id")]
                if len(pairs) >= 2:
                    batch_type, batch_ids = f"asset--{b}", pairs
                    break
            if batch_type is None and first_asset:
                # Fallback: same-type ids from the earlier all-bundle search.
                pairs = [(a["id"], a["type"]) for a in assets.get("data", [])
                         if a.get("id") and a.get("type") == first_asset.get("type")]
                if len(pairs) >= 2:
                    batch_type, batch_ids = first_asset["type"], pairs
            check("batch setup finds >=2 same-type assets", len(batch_ids) >= 2,
                  f"{batch_type or '?'}: {len(batch_ids)} ids" if batch_type else "no same-type pair found")

            if len(batch_ids) >= 2:
                id1, id2 = batch_ids[0][0], batch_ids[1][0]

                # Happy path: 2 known ids -> 2 normal summary rows, no wrapper noise.
                batch, err = await call(session, "farmos_get", {"full_type": batch_type, "uuids": [id1, id2]})
                check("batch get responds", batch is not None, (err or "")[:120])
                if batch:
                    rows = batch.get("data", [])
                    check("batch get count matches rows", batch.get("count") == 2 and len(rows) == 2,
                          f"count={batch.get('count')} rows={len(rows)}")
                    check("batch rows are normal summaries", all({"id", "type", "name"} <= set(r) for r in rows),
                          str(rows[0])[:120] if rows else "")
                    check("batch rows carry no error", all("error" not in r for r in rows), str(rows)[:160])
                    check("batch payload < 15KB", len(json.dumps(batch)) < 15000,
                          f"{len(json.dumps(batch))} bytes")

                # Missing UUID in the middle: per-record 404 row, order preserved.
                miss = str(uuid.uuid4())
                batch3, err = await call(session, "farmos_get", {"full_type": batch_type, "uuids": [id1, miss, id2]})
                check("batch with missing uuid responds", batch3 is not None, (err or "")[:120])
                if batch3:
                    rows3 = batch3.get("data", [])
                    check("batch missing uuid count is 3", batch3.get("count") == 3 and len(rows3) == 3,
                          f"count={batch3.get('count')}")
                    check("missing uuid -> not-found row", rows3[1] == {"id": miss, "error": "not found"},
                          str(rows3[1])[:120])
                    check("found rows are not error rows",
                          all("error" not in r for i, r in enumerate(rows3) if i != 1), str(rows3)[:160])
                    check("batch preserves requested order", [r.get("id") for r in rows3] == [id1, miss, id2],
                          str([r.get("id") for r in rows3]))

                # Backward compat: single uuid still returns a bare record.
                single, err = await call(session, "farmos_get", {"full_type": batch_type, "uuid": id1})
                check("single get still returns bare record",
                      single is not None and "count" not in single and "data" not in single
                      and single.get("id") == id1 and "name" in single,
                      f"keys={sorted((single or {}).keys())}")

                both_guard, err = await call(
                    session, "farmos_get", {"full_type": batch_type, "uuid": id1, "uuids": [id2]},
                )
                check("batch guard: uuid+uuids rejected",
                      both_guard is None and "both" in (err or ""), (err or "")[:160])

                neither, err = await call(session, "farmos_get", {"full_type": batch_type})
                check("batch guard: neither uuid nor uuids rejected",
                      neither is None and "uuids" in (err or ""), (err or "")[:160])

                over, err = await call(
                    session, "farmos_get",
                    {"full_type": batch_type, "uuids": [str(uuid.uuid4()) for _ in range(11)]},
                )
                check("batch guard: 11 uuids over cap",
                      over is None and "10" in (err or ""), (err or "")[:160])

                unk, err = await call(
                    session, "farmos_get", {"full_type": "asset--nosuchbundle", "uuids": [id1, id2]},
                )
                check("batch unknown type -> suggestion error",
                      unk is None and ("Unknown" in (err or "") or "Did you mean" in (err or "")), (err or "")[:160])

                # Mixed order proof with 3 same-type assets (missing slot first on purpose? no: [id3, miss, id2]).
                if len(batch_ids) >= 3:
                    id3 = batch_ids[2][0]
                    mix, err = await call(session, "farmos_get", {"full_type": batch_type, "uuids": [id3, miss, id2]})
                    check("mixed batch responds", mix is not None, (err or "")[:120])
                    if mix:
                        mrows = mix.get("data", [])
                        check("mixed batch order proof",
                              len(mrows) == 3 and mrows[0].get("id") == id3
                              and mrows[1] == {"id": miss, "error": "not found"}
                              and mrows[2].get("id") == id2,
                              str([r.get("id") for r in mrows]))
                else:
                    check("mixed batch order proof skipped (<3 same-type assets)", True,
                          f"{batch_type}: {len(batch_ids)} ids")

    return all(status == "PASS" for status, _, _ in RESULTS)


async def main() -> None:
    if SERVER_BIN is None:
        print("SKIP: no farmos-mcp server binary found (run `pip install -e .`)")
        return
    env_probe = os.environ.copy()
    env_probe["FARMOS_MCP_ENV_FILE"] = str(PROJECT / ".env")

    ok = await run_tests()
    width = max(len(name) for _, name, _ in RESULTS)
    print()
    for status, name, detail in RESULTS:
        mark = "✓" if status == "PASS" else "✗"
        line = f"{mark} {status}  {name.ljust(width)}"
        if detail and status == "FAIL":
            line += f"  | {detail}"
        elif detail and len(detail) < 60 and not detail.startswith("bundles"):
            line += f"  ({detail})"
        print(line)
    failed = sum(1 for s, _, _ in RESULTS if s == "FAIL")
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} checks passed")
    if failed:
        oauth_block = sum(1 for s, _, d in RESULTS if s == "FAIL" and "OAuth token request failed" in d)
        if oauth_block:
            print(
                f"\nAUTH BLOCKED: {oauth_block} failure(s) are the server-side OAuth 500 "
                "(Simple OAuth key pair or key paths). Fix on the farmOS host:\n"
                "  1) generate keys:            drush simple-oauth:generate-keys /opt/drupal/keys\n"
                "  2) point config at the files:\n"
                "       drush config-set simple_oauth.settings private_key /opt/drupal/keys/private.key\n"
                "       drush config-set simple_oauth.settings public_key  /opt/drupal/keys/public.key\n"
                "     (or save the paths in the UI: /admin/config/people/simple-oauth)\n"
                "  3) drush cr"
            )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
