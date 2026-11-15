"""Environment-based configuration (with optional .env file support)."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import dotenv

from .errors import FarmOSConfigError


@dataclass(frozen=True)
class Config:
    """Resolved server configuration."""

    base_url: str
    username: str | None = None
    password: str | None = None
    client_id: str = "farm"
    client_secret: str | None = None
    scope: str | None = None
    access_token: str | None = None
    read_only: bool = False
    default_limit: int = 50
    max_limit: int = 50
    timeout_seconds: float = 30.0
    summary_max_records: int = 5000


def _as_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw and raw.strip() else default
    except ValueError:
        return default


def _candidate_env_files() -> list[str]:
    """Where to look for a .env file, in priority order."""
    candidates: list[str] = []
    seen: set[str] = set()

    def walk_up(base: Path) -> None:
        try:
            here = base.resolve()
        except OSError:
            return
        for d in (here, *here.parents):
            cand = str(d / ".env")
            if cand not in seen:
                seen.add(cand)
                candidates.append(cand)

    walk_up(Path.cwd())
    # Also walk up from this package: handles MCP clients that launch the
    # server from an unrelated working directory (e.g. VS Code extension host).
    walk_up(Path(__file__).resolve().parent)
    return candidates


def _load_env_file() -> list[str]:
    """Load the first .env file found.

    Real environment variables always win (override=False). An explicit
    FARMOS_MCP_ENV_FILE takes strict priority: if it is set but missing, no
    fallback search happens. Returns the searched paths for error reporting.
    """
    explicit = (os.environ.get("FARMOS_MCP_ENV_FILE") or "").strip()
    if explicit:
        if os.path.isfile(explicit):
            dotenv.load_dotenv(explicit, override=False)
        return [explicit]
    searched = _candidate_env_files()
    for path in searched:
        if os.path.isfile(path):
            dotenv.load_dotenv(path, override=False)
            break
    return searched


def load_config() -> Config:
    """Build a Config from the environment (and the first .env file found)."""
    searched = _load_env_file()

    raw_url = (os.environ.get("FARMOS_URL") or "").strip().rstrip("/")
    if not raw_url:
        hint = ", ".join(searched[:6]) if searched else "no candidate paths"
        raise FarmOSConfigError(
            "FARMOS_URL is not set. Create a .env file (copy .env.example) containing "
            "e.g. 'FARMOS_URL=http://your-farmos-host', or set FARMOS_URL in your MCP "
            f"client config. Searched for .env at: {hint}"
        )
    if not raw_url.startswith(("http://", "https://")):
        # Bare hostname or LAN IP (e.g. 'FARMOS_URL=192.168.3.140'): assume http.
        raw_url = f"http://{raw_url}"

    username = os.environ.get("FARMOS_USERNAME") or None
    password = os.environ.get("FARMOS_PASSWORD") or None
    access_token = os.environ.get("FARMOS_ACCESS_TOKEN") or None
    if not access_token and not (username and password):
        raise FarmOSConfigError(
            "Authentication required: set FARMOS_ACCESS_TOKEN, or both "
            "FARMOS_USERNAME and FARMOS_PASSWORD (in .env or your MCP client config)"
        )

    return Config(
        base_url=raw_url,
        username=username,
        password=password,
        client_id=(os.environ.get("FARMOS_CLIENT_ID") or "farm").strip(),
        client_secret=os.environ.get("FARMOS_CLIENT_SECRET") or None,
        scope=os.environ.get("FARMOS_SCOPE") or None,
        access_token=access_token,
        read_only=_as_bool("FARMOS_READ_ONLY", False),
        default_limit=max(1, _as_int("FARMOS_DEFAULT_LIMIT", 50)),
        max_limit=max(1, _as_int("FARMOS_MAX_LIMIT", 50)),
        timeout_seconds=max(1.0, _as_int("FARMOS_TIMEOUT_MS", 30_000) / 1000),
        summary_max_records=max(100, _as_int("FARMOS_SUMMARY_MAX_RECORDS", 5000)),
    )


class LazyConfig:
    """Resolves the Config on first use.

    Lets the MCP server boot even when misconfigured: tools/list still works,
    and tool calls return a clean, actionable error instead of the server
    dying at startup (which an MCP client reports as a dead server).
    """

    def __init__(self) -> None:
        self._config: Config | None = None

    def __call__(self) -> Config:
        if self._config is None:
            self._config = load_config()
        return self._config
