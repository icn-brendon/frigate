"""Peewee migrations -- 036_add_stream_quality.py.

Idempotent variant: if interrupted (e.g. SIGKILL during a long ALTER TABLE
rewrite), re-running this migration must not error with
``duplicate column name: stream_quality``. We probe ``PRAGMA table_info``
for the column first and skip the ``ADD COLUMN`` when it is already
present. Backfill of legacy NULL rows is done in bounded chunks so a
many-million-row ``recordings`` table cannot block startup.
"""

import peewee as pw

SQL = pw.SQL


def _has_stream_quality_column(database) -> bool:
    rows = database.execute_sql('PRAGMA table_info("recordings")').fetchall()
    return any(row[1] == "stream_quality" for row in rows)


def _has_stream_quality_index(database) -> bool:
    rows = database.execute_sql(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='recordings' "
        "AND name='recordings_stream_quality'"
    ).fetchall()
    return len(rows) > 0


def migrate(migrator, database, fake=False, **kwargs):
    # Idempotent column add: SQLite has no "ADD COLUMN IF NOT EXISTS".
    if not _has_stream_quality_column(database):
        migrator.sql(
            'ALTER TABLE "recordings" ADD COLUMN "stream_quality" '
            "VARCHAR(10) NOT NULL DEFAULT 'sub'"
        )

    # Idempotent index create. CREATE INDEX IF NOT EXISTS is supported by
    # SQLite, but we also guard so a future re-run on a partially upgraded
    # DB does not spuriously rebuild it.
    if not _has_stream_quality_index(database):
        migrator.sql(
            'CREATE INDEX IF NOT EXISTS "recordings_stream_quality" '
            'ON "recordings" ("camera", "stream_quality", "start_time")'
        )

    # Backfill any legacy NULL rows to 'sub' in chunks so a huge table does
    # not lock the writer for long. The NOT NULL DEFAULT clause covers
    # newly-inserted rows and rows present at ALTER TABLE time, but a
    # partially-applied earlier attempt (or a recovery import that bypassed
    # the default) could leave NULLs.
    chunk = 5000
    while True:
        cursor = database.execute_sql(
            "UPDATE recordings SET stream_quality='sub' WHERE rowid IN "
            "(SELECT rowid FROM recordings WHERE stream_quality IS NULL "
            f"LIMIT {chunk})"
        )
        # SQLite reports affected rows via cursor.rowcount; if unavailable
        # (some peewee/SQLite combos return -1) fall back to a count probe.
        affected = getattr(cursor, "rowcount", -1)
        if affected is None or affected < 0:
            row = database.execute_sql(
                "SELECT COUNT(*) FROM recordings WHERE stream_quality IS NULL"
            ).fetchone()
            if not row or row[0] == 0:
                break
            # If we cannot trust rowcount and there are still NULLs after
            # an UPDATE that should have hit them, bail out to avoid an
            # infinite loop on a misbehaving driver.
            break
        if affected == 0:
            break


def rollback(migrator, database, fake=False, **kwargs):
    # No-op: SQLite does not support DROP COLUMN prior to 3.35, and even
    # where supported a rollback would lose the per-row stream_quality
    # tagging that the cleanup logic depends on. Leave the schema in
    # place; downgrades should be handled by the operator restoring a
    # pre-migration DB snapshot.
    pass
