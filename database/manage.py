"""Small, transactional SQLite migration CLI (Python standard library only)."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parent
MIGRATIONS_DIR = ROOT / "migrations"
SEEDS_DIR = ROOT / "seeds"
VERSION = 1


def connect(db_path):
    """Open a connection with foreign-key enforcement on, including for ETL callers."""
    connection = sqlite3.connect(str(db_path), isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def transaction(connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def run_sql(connection, script):
    """Execute a script without executescript's implicit transaction commit."""
    statement = ""
    for character in script:
        statement += character
        if character == ";" and sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        connection.execute(statement)


def applied_version(connection):
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not exists:
        return 0
    return connection.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()[0]


def migrate(connection):
    with transaction(connection):
        current = applied_version(connection)
        if current > VERSION:
            raise ValueError(f"Database version {current} is newer than this runner")
        if current == VERSION:
            return False
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        run_sql(connection, (MIGRATIONS_DIR / "001_initial.up.sql").read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (VERSION, "001_initial", datetime.now(timezone.utc).isoformat()),
        )
    return True


def rollback(connection):
    with transaction(connection):
        current = applied_version(connection)
        if current == 0:
            return False
        if current != VERSION:
            raise ValueError(f"Cannot roll back unsupported database version {current}")
        run_sql(connection, (MIGRATIONS_DIR / "001_initial.down.sql").read_text(encoding="utf-8"))
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (VERSION,))
    return True


def seed(connection):
    with transaction(connection):
        if applied_version(connection) != VERSION:
            raise ValueError("Run migrate before seed")
        run_sql(connection, (SEEDS_DIR / "001_reference.sql").read_text(encoding="utf-8"))
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("migrate", "rollback", "seed"))
    parser.add_argument("--db", required=True, type=Path, help="SQLite database file")
    arguments = parser.parse_args(argv)
    if not arguments.db.parent.exists():
        parser.error("Database parent directory does not exist")
    connection = connect(arguments.db)
    try:
        changed = globals()[arguments.command](connection)
        print(f"{arguments.command}: {'applied' if changed else 'already at requested state'}")
        return 0
    except (sqlite3.Error, OSError, ValueError) as error:
        print(f"{arguments.command} failed: {error}", file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
