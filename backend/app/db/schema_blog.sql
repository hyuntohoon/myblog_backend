-- =====================
-- ENUMS
-- =====================
CREATE TYPE post_status AS ENUM ('draft', 'published', 'archived');
CREATE TYPE review_subject AS ENUM ('album', 'track');

-- =====================
-- CATEGORIES & TAGS
-- =====================
CREATE TABLE categories (
                            id BIGSERIAL PRIMARY KEY,
                            name TEXT NOT NULL UNIQUE,
                            slug TEXT NOT NULL UNIQUE,
                            created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE tags (
                      id BIGSERIAL PRIMARY KEY,
                      name TEXT NOT NULL UNIQUE,
                      slug TEXT NOT NULL UNIQUE
);

-- =====================
-- POSTS
-- =====================
CREATE TABLE posts (
                       id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                       slug TEXT NOT NULL UNIQUE,
                       title TEXT NOT NULL,
                       description TEXT NOT NULL DEFAULT '',
                       body_mdx TEXT NOT NULL,
                       body_text TEXT,
                       posted_date DATE NOT NULL,
                       last_updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                       status post_status NOT NULL DEFAULT 'published',
                       category_id BIGINT REFERENCES categories(id) ON DELETE SET NULL,
                       search_index BOOLEAN NOT NULL DEFAULT TRUE,
                       extra JSON NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_posts_posted_date ON posts(posted_date);
CREATE INDEX idx_posts_category_id ON posts(category_id);
CREATE INDEX idx_posts_status ON posts(status);

-- =====================
-- POST TAGS
-- =====================
CREATE TABLE post_tags (
                           post_id UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                           tag_id BIGINT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                           PRIMARY KEY (post_id, tag_id)
);

-- =====================
-- METRICS / COMMENTS / LIKES
-- =====================
CREATE TABLE post_metrics (
                              post_id UUID PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
                              likes INT NOT NULL DEFAULT 0,
                              comments_count INT NOT NULL DEFAULT 0,
                              views INT NOT NULL DEFAULT 0, -- ✅ 조회수 추가
                              updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE post_comments (
                               id BIGSERIAL PRIMARY KEY,
                               post_id UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                               author_name TEXT,
                               author_email TEXT,
                               content TEXT NOT NULL,
                               created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_post_comments_post_id ON post_comments(post_id);
CREATE INDEX idx_post_comments_created_at ON post_comments(created_at);

CREATE TABLE post_likes (
                            post_id UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                            user_id TEXT NOT NULL,
                            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (post_id, user_id)
);

-- =====================
-- MUSIC CATALOG
-- =====================
CREATE TABLE artists (
                         id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                         name TEXT NOT NULL UNIQUE,
                         ext_refs JSON NOT NULL DEFAULT '{}',
                         created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE albums (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        title TEXT NOT NULL,
                        release_date DATE,
                        cover_url TEXT,
                        ext_refs JSON NOT NULL DEFAULT '{}',
                        views INT NOT NULL DEFAULT 0, -- ✅ 조회수 추가
                        created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE album_artists (
                               album_id UUID NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
                               artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
                               role TEXT,
                               PRIMARY KEY (album_id, artist_id)
);

CREATE TABLE tracks (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        album_id UUID NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
                        title TEXT NOT NULL,
                        track_no INT,
                        duration_sec INT,
                        ext_refs JSON NOT NULL DEFAULT '{}',
                        views INT NOT NULL DEFAULT 0, -- ✅ 조회수 추가
                        created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_tracks_album_id ON tracks(album_id);
CREATE INDEX idx_tracks_track_no ON tracks(track_no);

CREATE TABLE track_artists (
                               track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                               artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
                               role TEXT,
                               PRIMARY KEY (track_id, artist_id)
);

-- =====================
-- REVIEWS
-- =====================
CREATE TABLE post_reviews (
                              id BIGSERIAL PRIMARY KEY,
                              post_id UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                              subject review_subject NOT NULL,
                              album_id UUID REFERENCES albums(id) ON DELETE SET NULL,
                              track_id UUID REFERENCES tracks(id) ON DELETE SET NULL,
                              rating_value NUMERIC(3,1),
                              rating_scale SMALLINT NOT NULL DEFAULT 10,
                              notes TEXT,
                              extra JSON NOT NULL DEFAULT '{}',
                              created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                              CHECK (
                                  (subject = 'album' AND album_id IS NOT NULL AND track_id IS NULL)
                                      OR
                                  (subject = 'track' AND track_id IS NOT NULL AND album_id IS NULL)
                                  )
);
CREATE INDEX idx_post_reviews_post_id ON post_reviews(post_id);
CREATE INDEX idx_post_reviews_album_id ON post_reviews(album_id);
CREATE INDEX idx_post_reviews_track_id ON post_reviews(track_id);

-- =====================
-- OUTBOX / PUBLISHING
-- =====================
CREATE TABLE outbox_events (
                               id BIGSERIAL PRIMARY KEY,
                               type TEXT NOT NULL,
                               payload JSON NOT NULL,
                               created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                               processed_at TIMESTAMP,
                               retry_count INT NOT NULL DEFAULT 0
);
CREATE INDEX idx_outbox_processed_at ON outbox_events(processed_at);

CREATE TABLE publishing_runs (
                                 id BIGSERIAL PRIMARY KEY,
                                 post_id UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                                 commit_sha TEXT,
                                 status TEXT NOT NULL DEFAULT 'queued',
                                 triggered_at TIMESTAMP NOT NULL DEFAULT NOW(),
                                 finished_at TIMESTAMP
);
CREATE INDEX idx_publishing_runs_post_id ON publishing_runs(post_id);
CREATE INDEX idx_publishing_runs_triggered_at ON publishing_runs(triggered_at);

-- =====================
-- OPTIONAL: 운영 로그 (간단)
-- =====================
CREATE TABLE op_logs (
                         id BIGSERIAL PRIMARY KEY,
                         occurred_at TIMESTAMP NOT NULL DEFAULT NOW(),
                         name TEXT NOT NULL,      -- ex: 'spotify_search', 'spotify_album_fetch'
                         status_code INT,         -- HTTP status
                         note TEXT                -- error or params summary
);
CREATE INDEX idx_op_logs_occurred_at ON op_logs(occurred_at);