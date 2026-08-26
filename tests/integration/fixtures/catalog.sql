-- OPS-integration-db-locality Step 1 — the minimum catalog the integration
-- suite needs, seeded by the suite itself instead of borrowed from whatever
-- rows the target database happens to hold.
--
-- Before this file, six of nine integration test files did
--   SELECT id FROM albums LIMIT n
-- and skipped when the answer was short. That works only because the Neon test
-- branch is a copy of production; against an empty database the guards skip and
-- the run reports green with most of the suite gone. See the RFC's Current state.
--
-- Contract with tests/integration/catalog.py:
--   * every row's `spotify_id` / `slug` starts with `fixture-`, which is how the
--     helper finds the ids back — no UUID is duplicated between SQL and Python
--   * `ORDER BY spotify_id` (or `slug`) is the tests' stable ordering, so the
--     trailing -1/-2/-3 suffixes are the index, not the insertion order
--   * statements are INSERT-only and separated by `;`. The helper strips `--`
--     comments before splitting, so a semicolon in prose here is harmless.
--
-- Executed inside the calling test's outer transaction, which is rolled back on
-- teardown: nothing here is ever committed, on any engine. That is what lets the
-- same file run against the shared Neon branch without polluting it.

INSERT INTO artists (id, name, spotify_id, popularity)
VALUES
  ('0a270000-0000-4000-8000-000000000001', 'Fixture Artist One',   'fixture-artist-1', 50),
  ('0a270000-0000-4000-8000-000000000002', 'Fixture Artist Two',   'fixture-artist-2', 40),
  ('0a270000-0000-4000-8000-000000000003', 'Fixture Artist Three', 'fixture-artist-3', 30)
;

-- Five albums: the widest fixture takes LIMIT 5, the tightest needs >= 2.
-- release_date and cover_url are populated because AlbumBrief carries them and
-- a null there would exercise a different branch than production data does.
INSERT INTO albums (id, title, spotify_id, release_date, cover_url, album_type, total_tracks, popularity)
VALUES
  ('0a1b0000-0000-4000-8000-000000000001', 'Fixture Album One',   'fixture-album-1', DATE '2024-01-15', 'https://fixture.invalid/1.jpg', 'album',  10, 60),
  ('0a1b0000-0000-4000-8000-000000000002', 'Fixture Album Two',   'fixture-album-2', DATE '2023-06-02', 'https://fixture.invalid/2.jpg', 'album',   8, 55),
  ('0a1b0000-0000-4000-8000-000000000003', 'Fixture Album Three', 'fixture-album-3', DATE '2022-11-30', 'https://fixture.invalid/3.jpg', 'album',  12, 45),
  ('0a1b0000-0000-4000-8000-000000000004', 'Fixture Album Four',  'fixture-album-4', DATE '2021-03-09', 'https://fixture.invalid/4.jpg', 'single',  4, 35),
  ('0a1b0000-0000-4000-8000-000000000005', 'Fixture Album Five',  'fixture-album-5', DATE '2020-08-21', 'https://fixture.invalid/5.jpg', 'album',   9, 25)
;

-- Artist credits. Album One carries a credit for Artist One specifically:
-- test_tracked_artist_service_db resolves an album THROUGH album_artists and
-- skips when the artist has no credit, so this row is load-bearing, not decor.
INSERT INTO album_artists (album_id, artist_id, role)
VALUES
  ('0a1b0000-0000-4000-8000-000000000001', '0a270000-0000-4000-8000-000000000001', 'primary'),
  ('0a1b0000-0000-4000-8000-000000000002', '0a270000-0000-4000-8000-000000000002', 'primary'),
  ('0a1b0000-0000-4000-8000-000000000003', '0a270000-0000-4000-8000-000000000003', 'primary'),
  ('0a1b0000-0000-4000-8000-000000000004', '0a270000-0000-4000-8000-000000000001', 'primary'),
  ('0a1b0000-0000-4000-8000-000000000005', '0a270000-0000-4000-8000-000000000002', 'primary')
;

-- Three tracks, all with a non-null album_id: the 오늘의 곡 queue FKs both
-- track_id and album_id, and its fixture needs >= 2 such pairs.
INSERT INTO tracks (id, album_id, title, spotify_id, track_no, disc_no, duration_sec)
VALUES
  ('07c40000-0000-4000-8000-000000000001', '0a1b0000-0000-4000-8000-000000000001', 'Fixture Track One',   'fixture-track-1', 1, 1, 211),
  ('07c40000-0000-4000-8000-000000000002', '0a1b0000-0000-4000-8000-000000000001', 'Fixture Track Two',   'fixture-track-2', 2, 1, 187),
  ('07c40000-0000-4000-8000-000000000003', '0a1b0000-0000-4000-8000-000000000002', 'Fixture Track Three', 'fixture-track-3', 1, 1, 245)
;

-- Tier-0 genres only (parent_id NULL). test_post_genres_db filters on
-- `parent_id IS NULL` and takes LIMIT 3, so a tier-1 child here would be
-- invisible to it and a fourth tier-0 row would make the selection ambiguous.
INSERT INTO genres (id, slug, label, position)
VALUES
  ('09e40000-0000-4000-8000-000000000001', 'fixture-genre-1', 'Fixture Genre One',   901),
  ('09e40000-0000-4000-8000-000000000002', 'fixture-genre-2', 'Fixture Genre Two',   902),
  ('09e40000-0000-4000-8000-000000000003', 'fixture-genre-3', 'Fixture Genre Three', 903)
;
