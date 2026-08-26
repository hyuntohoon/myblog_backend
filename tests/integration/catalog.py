"""OPS-integration-db-locality Step 1 — seed the integration suite's catalog
instead of borrowing it.

Six of the nine integration test files used to open with

    rows = db.execute(text("SELECT id FROM albums LIMIT 5")).all()
    if len(ids) < 3:
        pytest.skip("need ≥3 albums in test DB")

which reads whatever the target database happens to hold. That is only ever true
because the Neon test branch is a copy of production, and it fails *silently*:
against an empty database every one of those guards skips, and the CI skip-guard
greps for `TEST_DB_URL|test branch|not deployed|schema`, none of which match
"need ≥3 albums in test DB". The suite would report green with most of itself
gone.

`seed_catalog(db)` inserts `fixtures/catalog.sql` into the caller's ALREADY-OPEN
transaction — the one each test file's `db` fixture rolls back on teardown. So:

  * nothing is committed, on any engine — the shared Neon branch is not polluted,
    and two concurrent runs cannot see each other's rows
  * the same call works identically against Neon and a local Postgres, which is
    what makes the two-engine parity check in the RFC meaningful
  * a test's catalog no longer depends on row-ordering luck in a shared database

Ids are NOT duplicated here. The SQL owns them; this module reads back what the
SQL actually inserted, keyed on the `fixture-` prefix and ordered by the natural
key, so the two files cannot drift apart.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

from sqlalchemy import text

_SQL_PATH = Path(__file__).parent / "fixtures" / "catalog.sql"


@dataclass(frozen=True)
class Catalog:
    """Ids of the seeded rows, each ordered by its natural key so index N is
    stable across runs and engines (album_ids[0] is always `fixture-album-1`)."""

    album_ids: List[str]
    artist_ids: List[str]
    track_ids: List[str]
    genre_ids: List[str]
    #: (track_id, album_id) pairs — what the 오늘의 곡 queue fixture wants.
    track_album_pairs: List[tuple]


def _strip_comments(raw: str) -> str:
    """Drop `--` line comments, ignoring `--` that appears inside a string literal.

    Comment-aware rather than convention-based on purpose: the first version of
    this split the raw text on `;` and told the .sql file to keep semicolons out
    of its comments. The .sql file promptly broke that rule in its own header
    ("a copy of production; against an empty database…") and every seeded test
    errored. A rule a sibling file must remember is not a contract, it is a trap.
    """
    out = []
    for line in raw.splitlines():
        in_str = False
        cut = len(line)
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "'":
                in_str = not in_str
            elif not in_str and ch == "-" and line[i : i + 2] == "--":
                cut = i
                break
            i += 1
        out.append(line[:cut])
    return "\n".join(out)


def _statements() -> List[str]:
    """Split the fixture file into individual statements.

    psycopg3 uses the extended query protocol, which rejects several statements
    in one execute(), so the file is sent one statement at a time.
    """
    body = _strip_comments(_SQL_PATH.read_text(encoding="utf-8"))
    return [s.strip() for s in body.split(";") if s.strip()]


def seed_catalog(db) -> Catalog:
    """Insert the fixture catalog into `db`'s open transaction and return its ids.

    Idempotent within a transaction is NOT claimed: call once per test (the
    fixtures below do), because a second call would violate the `spotify_id`
    unique constraints — which is the correct, loud failure for a double-seed.
    """
    for stmt in _statements():
        db.execute(text(stmt))
    db.flush()

    def ids(sql: str) -> List[str]:
        return [str(r[0]) for r in db.execute(text(sql)).all()]

    pairs = [
        (str(r[0]), str(r[1]))
        for r in db.execute(
            text(
                "SELECT id, album_id FROM tracks "
                "WHERE spotify_id LIKE 'fixture-track-%' AND album_id IS NOT NULL "
                "ORDER BY spotify_id"
            )
        ).all()
    ]

    return Catalog(
        album_ids=ids(
            "SELECT id FROM albums WHERE spotify_id LIKE 'fixture-album-%' ORDER BY spotify_id"
        ),
        artist_ids=ids(
            "SELECT id FROM artists WHERE spotify_id LIKE 'fixture-artist-%' ORDER BY spotify_id"
        ),
        track_ids=[p[0] for p in pairs],
        genre_ids=ids(
            "SELECT id FROM genres WHERE slug LIKE 'fixture-genre-%' AND parent_id IS NULL ORDER BY slug"
        ),
        track_album_pairs=pairs,
    )
