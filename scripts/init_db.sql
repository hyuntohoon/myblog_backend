CREATE EXTENSION IF NOT EXISTS citext;

-- Enums
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'post_status') THEN
        CREATE TYPE post_status AS ENUM ('draft','published','archived');
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'visibility') THEN
        CREATE TYPE visibility AS ENUM ('public','private','unlisted');
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'reaction_type') THEN
        CREATE TYPE reaction_type AS ENUM ('like','bookmark','star');
    END IF;
END $$;

-- users
CREATE TABLE IF NOT EXISTS users (
  id            BIGSERIAL PRIMARY KEY,
  email         CITEXT UNIQUE,
  username      CITEXT UNIQUE,
  display_name  TEXT,
  password_hash TEXT,
  role          TEXT DEFAULT 'user',
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at    TIMESTAMPTZ
);

-- 활성 사용자 부분 인덱스 (soft delete 고려)
CREATE INDEX IF NOT EXISTS idx_users_active ON users (id) WHERE deleted_at IS NULL;

-- posts
CREATE TABLE IF NOT EXISTS posts (
  id           BIGSERIAL PRIMARY KEY,
  author_id    BIGINT REFERENCES users(id) ON DELETE CASCADE,
  slug         CITEXT NOT NULL UNIQUE,
  title        TEXT NOT NULL,
  excerpt      TEXT,
  status       post_status NOT NULL DEFAULT 'draft',
  visibility   visibility  NOT NULL DEFAULT 'public',
  published_at TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_posts_author        ON posts(author_id);
CREATE INDEX IF NOT EXISTS idx_posts_status        ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_published_at  ON posts(published_at);

-- post_bodies
CREATE TABLE IF NOT EXISTS post_bodies (
  post_id      BIGINT PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
  content_md   TEXT,
  content_html TEXT,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- tags
CREATE TABLE IF NOT EXISTS tags (
  id         BIGSERIAL PRIMARY KEY,
  name       CITEXT NOT NULL UNIQUE,
  slug       CITEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- post_tags
CREATE TABLE IF NOT EXISTS post_tags (
  post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  tag_id  BIGINT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (post_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_post_tags_tag ON post_tags(tag_id);

-- comments
CREATE TABLE IF NOT EXISTS comments (
  id           BIGSERIAL PRIMARY KEY,
  post_id      BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  user_id      BIGINT REFERENCES users(id) ON DELETE SET NULL,
  parent_id    BIGINT REFERENCES comments(id) ON DELETE SET NULL,
  content      TEXT NOT NULL,
  author_name  TEXT,
  author_email CITEXT,
  is_approved  BOOLEAN NOT NULL DEFAULT TRUE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_comments_post   ON comments(post_id);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_id);

-- post_reactions
CREATE TABLE IF NOT EXISTS post_reactions (
  post_id    BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind       reaction_type NOT NULL DEFAULT 'like',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (post_id, user_id, kind)
);

-- media
CREATE TABLE IF NOT EXISTS media (
  id         BIGSERIAL PRIMARY KEY,
  owner_id   BIGINT REFERENCES users(id) ON DELETE SET NULL,
  bucket     TEXT NOT NULL,
  object_key TEXT NOT NULL,
  mime_type  TEXT,
  width      INT,
  height     INT,
  size_bytes BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (bucket, object_key)
);

-- post_media
CREATE TABLE IF NOT EXISTS post_media (
  post_id  BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  media_id BIGINT NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  kind     TEXT DEFAULT 'cover',
  sort     INT DEFAULT 0,
  PRIMARY KEY (post_id, media_id)
);
