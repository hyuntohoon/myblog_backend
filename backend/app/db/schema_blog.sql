-- 확장: UUID 생성
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ===== Enums =====
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'post_status') THEN
CREATE TYPE post_status AS ENUM ('draft', 'published', 'archived');
END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'review_subject') THEN
CREATE TYPE review_subject AS ENUM ('album', 'track');
END IF;
END$$;

-- ===== Categories & Tags =====
CREATE TABLE IF NOT EXISTS categories (
                                          id          BIGSERIAL PRIMARY KEY,
                                          name        TEXT NOT NULL UNIQUE,
                                          slug        TEXT NOT NULL UNIQUE,
                                          created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );

CREATE TABLE IF NOT EXISTS tags (
                                    id          BIGSERIAL PRIMARY KEY,
                                    name        TEXT NOT NULL UNIQUE,
                                    slug        TEXT NOT NULL UNIQUE
);

-- ===== Posts =====
CREATE TABLE IF NOT EXISTS posts (
                                     id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug             TEXT NOT NULL UNIQUE,             -- '/blog/:slug'
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    body_mdx         TEXT NOT NULL,
    body_text        TEXT,
    posted_date      DATE NOT NULL,
    last_updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status           post_status NOT NULL DEFAULT 'published',
    category_id      BIGINT REFERENCES categories(id) ON DELETE SET NULL,
    search_index     BOOLEAN NOT NULL DEFAULT TRUE,
    extra            JSONB NOT NULL DEFAULT '{}'::jsonb
    );

-- M:N: Posts - Tags
CREATE TABLE IF NOT EXISTS post_tags (
                                         post_id  UUID   NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    tag_id   BIGINT NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
    PRIMARY KEY (post_id, tag_id)
    );

-- ===== Metrics / Comments / Likes =====
CREATE TABLE IF NOT EXISTS post_metrics (
                                            post_id        UUID PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
    likes          INTEGER NOT NULL DEFAULT 0,
    comments_count INTEGER NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    );

CREATE TABLE IF NOT EXISTS post_comments (
                                             id           BIGSERIAL PRIMARY KEY,
                                             post_id      UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    author_name  TEXT,
    author_email TEXT,
    content      TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    );

CREATE TABLE IF NOT EXISTS post_likes (
                                          post_id    UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, user_id)
    );

-- ===== Music Catalog =====
CREATE TABLE IF NOT EXISTS artists (
                                       id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,                          -- (나중) UNIQUE(lower(name)) 인덱스 권장
    ext_refs    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );

CREATE TABLE IF NOT EXISTS albums (
                                      id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title        TEXT NOT NULL,
    release_date DATE,
    cover_url    TEXT,
    ext_refs     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    );

CREATE TABLE IF NOT EXISTS album_artists (
                                             album_id  UUID NOT NULL REFERENCES albums(id)  ON DELETE CASCADE,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    role      TEXT,
    PRIMARY KEY (album_id, artist_id)
    );

CREATE TABLE IF NOT EXISTS tracks (
                                      id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    album_id     UUID NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    track_no     INTEGER,
    duration_sec INTEGER,
    ext_refs     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    );

CREATE TABLE IF NOT EXISTS track_artists (
                                             track_id  UUID NOT NULL REFERENCES tracks(id)  ON DELETE CASCADE,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    role      TEXT,
    PRIMARY KEY (track_id, artist_id)
    );

-- ===== Reviews (polymorphic: album or track) =====
CREATE TABLE IF NOT EXISTS post_reviews (
                                            id             BIGSERIAL PRIMARY KEY,
                                            post_id        UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    subject        review_subject NOT NULL,            -- 'album' | 'track'
    album_id       UUID REFERENCES albums(id) ON DELETE SET NULL,
    track_id       UUID REFERENCES tracks(id) ON DELETE SET NULL,
    rating_value   NUMERIC(3,1),                       -- 0.0 ~ 10.0
    rating_scale   SMALLINT NOT NULL DEFAULT 10,
    notes          TEXT,
    extra          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_review_target CHECK (
(subject = 'album' AND album_id IS NOT NULL AND track_id IS NULL) OR
(subject = 'track' AND track_id IS NOT NULL AND album_id IS NULL)
    ),
    CONSTRAINT chk_rating_range CHECK (
                                          rating_value IS NULL OR (rating_value >= 0 AND rating_value <= rating_scale)
    )
    );

-- ===== Ops (Outbox & Publishing) =====
CREATE TABLE IF NOT EXISTS outbox_events (
                                             id           BIGSERIAL PRIMARY KEY,
                                             type         TEXT NOT NULL,                         -- e.g., 'PostCreated'
                                             payload      JSONB NOT NULL,
                                             created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    retry_count  INTEGER NOT NULL DEFAULT 0
    );

CREATE TABLE IF NOT EXISTS publishing_runs (
                                               id            BIGSERIAL PRIMARY KEY,
                                               post_id       UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    commit_sha    TEXT,
    status        TEXT NOT NULL DEFAULT 'queued',       -- queued|running|succeeded|failed
    triggered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
    );