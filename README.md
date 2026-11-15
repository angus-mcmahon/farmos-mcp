# farmos-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets AI agents read and write records in a [farmOS](https://farmos.org) 3.x server via its JSON:API.

## Setup overview

1. **Install** this package (below).
2. **Configure** `.env` with your farmOS URL + credentials.
3. **Server-side setup**: generate the farmOS OAuth2 key pair and point Simple
   OAuth's config at it — *fresh farmOS installs usually ship without this, and
   the API will 500 on every login until it's done* (details below).
4. **Run** the server and connect it to your MCP client.
5. **Verify** with the included live test suite.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Configure

Copy `.env.example` to `.env` and fill in your farmOS URL and credentials:

```
FARMOS_URL=https://farm.example.com
FARMOS_USERNAME=api-user
FARMOS_PASSWORD=secret
```

Authentication uses the OAuth2 password grant against `/oauth/token` (the bundled farmOS "farm" consumer by default).

## One-time server-side setup (OAuth2 keys)

farmOS's API authenticates with OAuth2, and the Simple OAuth module needs a
key pair **plus its config pointing at those files** before any login will
succeed. Fresh farmOS installs typically ship without this, and the symptom
is every token request failing with:

```
HTTP 500: The authorization server encountered an unexpected condition ...
You need to set the OAuth2 private key.
```

Run the block below on the farmOS host, from the directory containing your
`docker-compose.yml` (adjust `www` to your service name — `docker compose ps`
lists it; `/opt/drupal/keys` is the path the official farmOS compose file
bind-mounts as a persistent volume, outside the web root):

```bash
# 1) Generate the key pair (drush runs as root inside the container):
docker compose exec www drush simple-oauth:generate-keys /opt/drupal/keys

# 2) Make the keys readable by the HTTP server user. drush runs as root, but
#    the web server runs as www-data — if it cannot read the private key you
#    keep getting "You need to set the OAuth2 private key" even though the
#    config below is correct (required by the farmOS docs).
docker compose exec www chown www-data:www-data /opt/drupal/keys/private.key /opt/drupal/keys/public.key
docker compose exec www chmod 640 /opt/drupal/keys/private.key /opt/drupal/keys/public.key

# 3) Point Simple OAuth's config at the files. generate-keys only writes the
#    files — it does NOT update the config, so this step is required:
docker compose exec www drush config-set simple_oauth.settings private_key /opt/drupal/keys/private.key
docker compose exec www drush config-set simple_oauth.settings public_key  /opt/drupal/keys/public.key

# 4) Rebuild caches so the new config takes effect:
docker compose exec www drush cr
```

Non-Docker installs: same commands via plain `drush` in the site directory
(with your `www-data`-equivalent web user owning the keys), or save the paths
through the UI at `/admin/config/people/simple-oauth` and use the "Generate
keys" button there instead of step 1.

**Verify the server side is ready** — a bare POST should return `400`
(anything but `500`; `400` means the endpoint is now healthy but the request
is empty):

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://farm.example.com/oauth/token
```

If a real login then reports that the *private key file permissions are not
correct* even though the files are fine (common in containers where secrets
are injected), the check can be relaxed in the site's `settings.php` (see the
Simple OAuth docs):

```php
$settings['simple_oauth.key_permissions_check'] = FALSE;
```

Also make sure the API modules are enabled on your farmOS (`farm_api` and
`farm_api_oauth` under Administration » Modules) — `https://farm.example.com/api`
should return JSON anonymously before proceeding.

## Run

```bash
.venv/bin/farmos-mcp
```

The server speaks MCP over stdio. Example VS Code `mcp.json` config:

```json
{
  "servers": {
    "farmos": {
      "command": "/absolute/path/to/farmos-mcp/.venv/bin/farmos-mcp"
    }
  }
}
```

`.env` discovery walks up from the working directory and from the installed
package, so the config above works even when VS Code launches the server from
another directory. A bare hostname/IP is accepted for `FARMOS_URL`
(`FARMOS_URL=192.168.3.140` → `http://192.168.3.140`). If the server starts
but tools report a config error, check that `.env` exists and that real
environment variables aren't overriding it (the error names the paths that
were searched). A misconfigured server still starts — tool calls report the
problem instead of the process dying.

After restarting the server in your MCP client, `farmos_info` is the
quickest check that everything is wired up: it should return your farm's
name, farmOS version, and system of measurement.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Tool error `HTTP 500: … You need to set the OAuth2 private key` | Simple OAuth keys missing, unreadable by the web server user, or not wired into config | Do the **server-side setup** section above — all of it: generate, `chown`/`chmod` for `www-data`, `config-set`, `drush cr` |
| Tool error `HTTP 500` mentioning *private key file permissions* | The upstream permission check false-positives in containers | `$settings['simple_oauth.key_permissions_check'] = FALSE;` in `settings.php` |
| Tool error starting `HTTP 4xx` on login with valid-looking credentials | Wrong username/password, or the user lacks API permissions | Log into the farmOS UI with those credentials first; use a role with API access (e.g. farm manager) |
| Tool error `FARMOS_URL is not set` | `.env` not found or empty | Create `.env` (copy `.env.example`); the error lists the paths that were searched; or set `FARMOS_MCP_ENV_FILE` to its absolute path |
| Tool error `HTTP 404: resource not found (<path>)` | The UUID doesn't exist on the server (or the wrong bundle) | Re-fetch it via `farmos_assets` / `farmos_logs` to get the correct UUID |
| `test/test_live.py` prints `AUTH BLOCKED` | Every check failing with the same OAuth 500 | The server-side OAuth keys aren't ready yet — see the setup section |
| VS Code shows the server as stopped right after launch | Unhandled startup crash | Run `.venv/bin/farmos-mcp` manually in a terminal and read the traceback |

Errors from the farmOS API always lead with `HTTP <status>` so failures are
easy to triage. Run the live suite (`.venv/bin/python test/test_live.py`) any
time setup state changes — it pinpoints the failing layer.

## Tools

- `farmos_info` – server metadata: farm name, farmOS version, system of measurement, authenticated user
- `farmos_list_resources` – the JSON:API resource types this server exposes
- `farmos_assets` – search assets (plant, animal, land, equipment, …)
- `farmos_logs` – search logs (activity, seeding, harvest, …) by asset/location/time window
- `farmos_get` – fetch one resource with relationships resolved, or a batch
  of 2–10 UUIDs of the same type in one call (missing records come back as
  per-row `{id, error: "not found"}` instead of failing the batch)
- `farmos_terms` – list taxonomy terms (plant_type, animal_type, season, unit, …)
- `farmos_summary` – **server-side log aggregation** for compact counts (by
  month/year/type/status/asset) across a time window. The MCP pages through
  all matching logs internally (sparse fieldsets + a record cap, default
  5000 via `FARMOS_SUMMARY_MAX_RECORDS`), so a year-scale history costs the
  same ~300-byte payload as a week — prefer this over paging raw logs for
  "what happened in 2026?" style questions. `quantities: true` rolls up
  numeric quantity values into `quantity_totals` (measure/unit/total), and
  `interval: week|day` switches the time-series bucketing (`by_interval`).

### Write tools

- `farmos_create_asset` – create an asset (plant, animal, equipment, …)
- `farmos_create_log` – create a log (activity, observation, harvest, …) with
  asset/location references
- `farmos_checkin_log` – one-shot "log something happened on asset X":
  resolves `asset_name` (exact match first, then contains; refuses ambiguity
  unless `allow_ambiguous`), creates the log with optional quantity sugar and
  an auto-generated name. Returns the read-back summary, the asset matches
  used, and any created quantity resources (for tracking/cleanup).
- `farmos_inventory_change` – adjust an asset's inventory the farmOS-native
  way: a `done` adjustment log carrying an `inventory_adjustment` quantity
  (`delta` > 0 increments, < 0 decrements — never a direct PATCH of the
  asset). Units select the inventory bucket, so `units_name` must resolve
  exactly. Returns before/after inventory read back from each asset.
- `farmos_update_entity` – update `name` / `notes` / log `status` only
- `farmos_archive` / `farmos_flag` – soft-delete via archiving; set/clear flags

Safety model:

- Every create **reads the record back** (with relationships resolved) and
  returns that verified summary — never the raw POST response.
- There is intentionally **no delete tool** for agents. Use
  `farmos_archive` (soft delete); actual deletion is a server-admin action.
- Set `FARMOS_READ_ONLY=1` to disable all write tools (calls then fail with a
  clear message).
- `farmos_update_entity` enforces a strict field allowlist; unknown fields are
  refused rather than silently written.
- Field-level 400/422 errors come back as schema-driven hints: the
  server's per-bundle JSON:API schema (`/api/{entity}/{bundle}/resource/schema`)
  is cached lazily, and errors read e.g. "field 'inventry' does not exist on
  asset--equipment. Did you mean 'inventory'? Fields: …" instead of raw
  Drupal validation jargon.

## Tests

```bash
# Offline smoke test (no farmOS server needed): tools register, errors propagate
.venv/bin/python test/test_tools_list.py

# Offline field-error intelligence (mocked client, no farmOS needed):
.venv/bin/python test/test_error_hints.py

# Live integration test (needs .env credentials + a reachable farmOS):
.venv/bin/python test/test_live.py

# Write-path live test — MUTATES the configured farm, then cleans up:
.venv/bin/python test/test_write_live.py
```

The read suite drives the real server over stdio — every tool, filter paths,
pagination, unknown-type suggestions, and 404 handling — and prints an
actionable hint when the server-side OAuth key setup is incomplete. Use it as
the final "is my setup correct?" check; a green run means the MCP client will
work too (it exercises the exact same stdio launch path).

The write test creates uniquely named `mcp-test <hash>` records (two assets,
several logs via create/checkin/inventory paths, and their quantity
resources), verifies every write with a read, exercises status transitions
and input guards, then **deletes everything and verifies the 404s** —
including the assets' computed inventory reverting. Cleanup runs in a
`finally` block, so a mid-test failure
still deletes what was created; anything the server refuses to delete is
reported with UUIDs (POLLUTION WARNING).
