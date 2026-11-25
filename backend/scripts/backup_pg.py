"""PostgreSQL helpers shared by backup.py / restore.py.

* Uses a local `pg_dump`/`pg_restore` when on PATH, otherwise `docker exec -i <container>` (container name is a parameter:
  --pg-container or AETHER_PG_CONTAINER, default "aether-pg"). Force docker with AETHER_PG_VIA_DOCKER=1.
* The database password is handed to the tool ONLY through the PGPASSWORD environment variable of the child process
  (`docker exec -e PGPASSWORD` forwards the client's value; it never appears in argv or logs).
* Output of these helpers must never include the URL password (callers print `redact(url)`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

TIMEOUT_S = 1800


class PgError(RuntimeError):
    pass


def parse(url: str) -> URL:
    u = make_url(url)
    if not u.drivername.startswith("postgresql"):
        raise PgError("not a PostgreSQL URL")
    return u


def redact(url: str | URL) -> str:
    return (url if isinstance(url, URL) else make_url(url)).render_as_string(hide_password=True)


def _cmd(tool: str, u: URL, container: str, *extra: str, interactive: bool = False) -> tuple[list[str], dict]:
    env = {**os.environ, "PGPASSWORD": u.password or ""}
    local = shutil.which(tool)
    if local and os.environ.get("AETHER_PG_VIA_DOCKER") != "1":
        host, port = u.host or "127.0.0.1", str(u.port or 5432)
        return [local, "-h", host, "-p", port, "-U", u.username or "postgres", *extra], env
    docker = shutil.which("docker")
    if not docker:
        raise PgError(f"neither a local {tool} nor docker is available")
    # Inside the container the server listens on its own port/host, not the host-mapped one.
    cport = os.environ.get("AETHER_PG_CONTAINER_PORT", "5432")
    cmd = [docker, "exec", *(["-i"] if interactive else []), "-e", "PGPASSWORD", container, tool,
           "-h", "127.0.0.1", "-p", cport, "-U", u.username or "postgres", *extra]
    return cmd, env


def dump(url: str, container: str, out_file: Path) -> None:
    """Custom-format, transactionally consistent dump of the database in `url` into out_file."""
    u = parse(url)
    cmd, env = _cmd("pg_dump", u, container, "-Fc", "--no-owner", "--no-privileges", u.database or "")
    with out_file.open("wb") as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, env=env, timeout=TIMEOUT_S)
    if r.returncode != 0:
        out_file.unlink(missing_ok=True)
        raise PgError(f"pg_dump failed: {r.stderr.decode(errors='replace')[:500]}")


def restore_into(url_new_db: str, container: str, dump_file: Path) -> None:
    u = parse(url_new_db)
    cmd, env = _cmd("pg_restore", u, container, "--no-owner", "--no-privileges", "--exit-on-error", "-d", u.database or "",
                    interactive=True)
    with dump_file.open("rb") as f:
        r = subprocess.run(cmd, stdin=f, stderr=subprocess.PIPE, stdout=subprocess.PIPE, env=env, timeout=TIMEOUT_S)
    if r.returncode != 0:
        raise PgError(f"pg_restore failed: {r.stderr.decode(errors='replace')[:500]}")


def _admin(u: URL):
    return create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")


def database_table_count(u: URL) -> int | None:
    """Number of user tables in `u.database`, or None if the database does not exist."""
    with _admin(u).connect() as c:
        if not c.execute(text("SELECT 1 FROM pg_database WHERE datname=:n"), {"n": u.database}).first():
            return None
    e = create_engine(u)
    try:
        with e.connect() as c:
            return int(c.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')")).scalar_one())
    finally:
        e.dispose()


def create_database(u: URL) -> None:
    if not (u.database or "").replace("_", "").isalnum():
        raise PgError("restore database name must be alphanumeric/underscore")
    with _admin(u).connect() as c:
        c.execute(text(f'CREATE DATABASE "{u.database}"'))


def alembic_revision(url: str) -> str | None:
    e = create_engine(url)
    try:
        with e.connect() as c:
            try:
                row = c.execute(text("SELECT version_num FROM alembic_version")).first()
                return row[0] if row else None
            except Exception:  # noqa: BLE001 - table absent => baseline created by create_all
                return None
    finally:
        e.dispose()
