"""The shape of the queue database, and how a database gets to the current one.

Kept apart from the operations that use it, because a schema change and a queue
behaviour change are different things to review, and a broker upgraded in place
is the one case where the two must agree exactly.
"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    sha TEXT NOT NULL,
    ref TEXT NOT NULL,
    command TEXT NOT NULL,
    labels TEXT NOT NULL,
    timeout INTEGER NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL,
    device TEXT,
    lease_expires REAL,
    exit_code INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    delivery TEXT,
    status_sha TEXT,
    last_error TEXT,
    available_at REAL,
    log TEXT,
    steps TEXT,
    pending_status TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_delivery ON jobs(delivery) WHERE delivery IS NOT NULL;
CREATE TABLE IF NOT EXISTS agents (
    device TEXT PRIMARY KEY,
    labels TEXT NOT NULL,
    max_concurrency INTEGER NOT NULL,
    last_seen REAL NOT NULL,
    build TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER);
INSERT OR IGNORE INTO meta (key, value) VALUES ('reclaims', 0);
"""

# columns added after the first release, in the order they were needed. Every one
# of them is nullable or defaulted, so a database written before it still opens
# and still reads, and the code decides what a missing value means.
ADDED_COLUMNS = (
    ("log", "TEXT"),
    ("status_sha", "TEXT"),
    ("last_error", "TEXT"),
    ("available_at", "REAL"),
    ("steps", "TEXT"),
    ("pending_status", "TEXT"),
    ("status_seq", "INTEGER NOT NULL DEFAULT 0"),
)

# the agents table predates the build column. Its default is the empty build,
# which reads as "this device did not say" rather than as a version, because a
# device that cannot name itself must not read as current.
AGENT_ADDED_COLUMNS = (("build", "TEXT NOT NULL DEFAULT '{}'"),)


def prepare(connection: sqlite3.Connection) -> None:
    """Bring an open database up to the current shape.

    Additive only. A queue that has to be rewritten to be upgraded is a queue
    that cannot be upgraded while it is holding someone's CI run.
    """
    connection.executescript(SCHEMA)
    for table, columns in (("jobs", ADDED_COLUMNS), ("agents", AGENT_ADDED_COLUMNS)):
        present = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for column, kind in columns:
            if column not in present:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
