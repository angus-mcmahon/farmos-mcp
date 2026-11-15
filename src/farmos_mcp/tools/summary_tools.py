"""farmos_summary: server-side log aggregation for tiny time-window reports.

Full record listings cost ~700 bytes each through the LLM, so year-scale
histories on large farms get expensive. This tool pages through every matching
log *inside the MCP* (sparse fieldsets, per-bundle paging, a shared record
cap) and returns compact counts instead — same tool-call and token cost
whether the window covers 10 logs or 10,000.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..client import FarmOSClient
from ..config import Config
from ..discovery import Discovery
from ..errors import FarmOSError
from ..summarize import included_index
from .helpers import to_epoch
from .log_tools import _log_filters

_PAGE_SIZE = 50
_GROUP_KEYS = {"month", "year", "type", "status", "asset", "none"}
_INTERVALS = {"month", "week", "day"}


def _value_float(value: Any) -> float | None:
    """Quantity value (fraction dict or scalar) -> float, or None if unparseable."""
    if isinstance(value, dict):
        decimal = value.get("decimal")
        if decimal is not None:
            try:
                return float(decimal)
            except (TypeError, ValueError):
                pass
        num = value.get("numerator")
        if num is None:
            return None
        try:
            return float(num) / float(value.get("denominator") or 1)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quantity_triples(log: dict, included: dict[str, dict]) -> list[tuple[str, float, str]]:
    """Resolve a log's quantity relationship into (measure, value, unit) triples."""
    rel = (log.get("relationships") or {}).get("quantity") or {}
    refs = rel.get("data") or []
    if isinstance(refs, dict):
        refs = [refs]
    out: list[tuple[str, float, str]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        qres = included.get(ref.get("id")) or {}
        attrs = qres.get("attributes") or {}
        val = _value_float(attrs.get("value"))
        if val is None:
            continue
        units = (qres.get("relationships") or {}).get("units") or {}
        uref = units.get("data")
        unit = included.get(uref.get("id")) if isinstance(uref, dict) else None
        unit_name = str(((unit or {}).get("attributes") or {}).get("name") or "")
        out.append((str(attrs.get("measure") or ""), val, unit_name))
    return out


def _bucket(ts: str, interval: str) -> str:
    """Bucket an ISO timestamp into a 'week'/'day' label ('unknown' when unparseable)."""
    if not ts:
        return "unknown"
    try:
        day = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except ValueError:
        return "unknown"
    if interval == "day":
        return day.isoformat()
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def register(mcp: FastMCP, client: FarmOSClient, config: Callable[[], Config], discovery: Discovery) -> None:

    @mcp.tool()
    async def farmos_summary(
        timestamp_after: str | None = None,
        timestamp_before: str | None = None,
        type: str | None = None,
        asset_id: str | None = None,
        status: str | None = None,
        group_by: str = "month",
        top: int = 10,
        quantities: bool = False,
        interval: str | None = None,
    ) -> dict:
        """Aggregate farmOS logs within a time window and return compact counts (not records).

        Scales to any volume: the server pages through all matching logs
        internally, so the payload stays tiny whether a window holds 10 logs
        or 10,000. Optional extras stay totals-only: quantity_totals (with
        quantities=true) and a by_interval time series (interval='week'/'day').

        Args:
            timestamp_after / timestamp_before: ISO-8601 window, e.g.
                '2026-01-01' or '2026-01-01T00:00:00Z'. Omit both for all-time.
            type: optional log bundle such as 'activity', 'harvest', 'maintenance'.
            asset_id: UUID of an asset the logs must reference.
            status: 'done' or 'pending'.
            group_by: 'month' (default), 'year', 'type', 'status', 'asset', or 'none'.
            top: how many entries to return when grouping by asset.
            quantities: when true, also sums quantity values per measure+unit and
                returns quantity_totals: [{measure, unit, total}]. Counts every
                scanned log regardless of status; totals are decimal sums (a
                1/2 fraction adds 0.5).
            interval: time-series bucketing under by_interval — 'week' gives ISO
                labels like '2026-W07', 'day' gives dates like '2026-11-14'.
                'month' or omitted keeps group_by's by_month/by_year behavior.
        """
        await discovery.ensure(client)
        cfg = config()
        cap = cfg.summary_max_records
        if group_by not in _GROUP_KEYS:
            group_by = "month"
        if interval is not None and interval not in _INTERVALS:
            raise FarmOSError(
                f"invalid interval '{interval}'; valid values: month, week, day (or omit)"
            )

        bundles = [type] if type else discovery.bundles_for("log")
        if type:
            discovery.validate_type(f"log--{type}")
        if not bundles:
            raise ValueError("no log bundles on this server")

        filters = _log_filters(asset_id, None, status, timestamp_after, timestamp_before)

        want_assets = group_by == "asset"
        includes = ["asset"] if want_assets else []
        if quantities:
            includes += ["quantity", "quantity.units"]
        # Sparse fieldsets: only fetch what aggregation needs.
        fields = {f"log--{b}": "timestamp,status,asset" for b in bundles}
        if quantities:
            for b in bundles:
                fields[f"log--{b}"] += ",quantity"
            fields["quantity--standard"] = "measure,value"
            fields["quantity--material"] = "measure,value"
            fields["taxonomy_term--unit"] = "name"
        if want_assets:
            for b in discovery.bundles_for("asset"):
                fields[f"asset--{b}"] = "name"

        counter = {"scanned": 0}
        truncated = False
        skipped_bundles: list[str] = []

        async def scan(bundle: str):
            """Page through one bundle, collecting (type, attrs, asset_names, quantity triples)."""
            nonlocal truncated
            found: list[tuple[str, dict, list[str], list[tuple[str, float, str]]]] = []
            offset = 0
            while True:
                if counter["scanned"] >= cap:
                    truncated = True
                    break
                try:
                    doc = await client.get_collection(
                        "log", bundle, filters=filters, includes=includes,
                        limit=_PAGE_SIZE, offset=offset, fields=fields,
                    )
                except Exception as exc:
                    if not str(exc):
                        pass
                    # Bundle-level 4xx (permissions/unsupported fieldset): skip
                    # transparently rather than failing the whole summary.
                    from ..errors import FarmOSAPIError
                    if isinstance(exc, FarmOSAPIError) and exc.status_code < 500:
                        skipped_bundles.append(bundle)
                        return found
                    raise
                data = doc.get("data") or []
                if not data:
                    break

                # Each page carries its own `included`; index it per document.
                included = included_index(doc)
                names_by_id: dict[str, str] = {}
                if want_assets:
                    for inc in doc.get("included") or []:
                        if inc.get("type", "").startswith("asset--"):
                            nm = (inc.get("attributes") or {}).get("name")
                            if nm:
                                names_by_id[inc.get("id")] = str(nm)

                for r in data:
                    attrs = r.get("attributes") or {}
                    rel = (r.get("relationships") or {}).get("asset") or {}
                    refs = rel.get("data") or []
                    if isinstance(refs, dict):
                        refs = [refs]
                    asset_names = [names_by_id.get(ref.get("id"), "") for ref in refs if isinstance(ref, dict)]
                    quants = _quantity_triples(r, included) if quantities else []
                    found.append((r.get("type") or "", attrs, [n for n in asset_names if n], quants))

                counter["scanned"] += len(data)
                offset += len(data)
                if not (doc.get("links") or {}).get("next"):
                    break
            return found

        results = await asyncio.gather(*(scan(b) for b in bundles))
        rows = [row for chunk in results for row in chunk]

        by_type: Counter[str] = Counter()
        by_status: Counter[str] = Counter()
        grouped: Counter[str] = Counter()
        by_asset: Counter[str] = Counter()
        by_interval: Counter[str] = Counter()
        qty_totals: Counter[tuple[str, str]] = Counter()
        interval_on = interval in ("week", "day")
        for rtype, attrs, asset_names, quants in rows:
            bundle_name = rtype.split("--", 1)[-1]
            by_type[bundle_name] += 1
            st = attrs.get("status") or "unknown"
            by_status[st] += 1
            ts = str(attrs.get("timestamp") or "")
            if group_by == "month":
                grouped[ts[:7] or "unknown"] += 1
            elif group_by == "year":
                grouped[ts[:4] or "unknown"] += 1
            elif group_by == "type":
                grouped[bundle_name] += 1
            elif group_by == "status":
                grouped[st] += 1
            if interval_on:
                by_interval[_bucket(ts, interval)] += 1
            for name in asset_names:
                by_asset[name] += 1
            for measure, val, unit in quants:
                qty_totals[(measure, unit)] += val

        out: dict = {
            "total": len(rows),
            "scanned": counter["scanned"],
            "truncated": truncated,
            "status": dict(by_status),
            "by_type": dict(by_type),
        }
        if timestamp_after or timestamp_before:
            out["window"] = {"after": timestamp_after, "before": timestamp_before}
        else:
            out["window"] = "all-time"
        if interval_on:
            # Week/day bucketing is the primary series for month/year/none
            # (no by_month/by_year alongside it) and additive for type/status/asset.
            out["by_interval"] = dict(sorted(by_interval.items()))
        elif group_by == "month":
            out["by_month"] = dict(sorted(grouped.items()))
        elif group_by == "year":
            out["by_year"] = dict(sorted(grouped.items()))
        if group_by == "asset":
            out["top_assets"] = [{"name": n, "count": c} for n, c in by_asset.most_common(max(1, top))]
        if quantities:
            out["quantity_totals"] = [
                {"measure": m, "unit": u, "total": round(v, 6)}
                for (m, u), v in sorted(qty_totals.items())
            ]
        if truncated:
            out["truncated"] = True
            out["note"] = (
                f"scanning stopped at the {cap}-record cap; narrow the window or add "
                "filters (type/asset_id) for exact totals"
            )
        if skipped_bundles:
            out["skipped_bundles"] = skipped_bundles
        return out
