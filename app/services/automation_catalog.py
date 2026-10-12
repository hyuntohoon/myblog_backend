"""Static job catalog for the owner's automation page (FEAT-automation-admin 2b).

This is the Step 1 inventory (RFC tables A–F, 2026-10-12) as code. It says what
each job IS — purpose, trigger, executor, what it drains — and what the
inventory found it to be (`declared_state`). It is NOT runtime state: nothing
here may make a row look healthy. Liveness comes only from an adapter with an
`observed_at`, and a row with no fresh evidence renders unknown.

Changing a row here is changing the inventory; keep the RFC's tables in step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Optional, Tuple

CATALOG_AS_OF = date(2026, 10, 12)

# RFC "Design notes fixed for 2a–2e": a heartbeat is stale after 2 × the agent's
# schedule interval, minimum 15 minutes.
MIN_STALE_AFTER_SECONDS = 15 * 60


def stale_after(interval_seconds: int) -> int:
    return max(2 * interval_seconds, MIN_STALE_AFTER_SECONDS)


DAY = 24 * 60 * 60

# Translator versions the catalog knows. Versions found in the table but not
# listed here are still reported, unlabelled — the page must not hide a queue
# because the catalog has not heard of it.
TRANSLATION_WORK_TABLE = "lyrics_translation_work"
GPT_LYRICS_VERSION = "chatgpt/lyrics-ko-v1"
GPT_GENIUS_VERSION = "chatgpt/genius-ko-v1"
CLAUDE_DEMAND_VERSION = "claude.sonnet/v2"


def translation_queue_key(translator_version: str) -> str:
    return f"{TRANSLATION_WORK_TABLE}:{translator_version}"


@dataclass(frozen=True)
class TranslationVersion:
    version: str
    label: str
    frozen: bool


TRANSLATION_VERSIONS: Tuple[TranslationVersion, ...] = (
    TranslationVersion(GPT_LYRICS_VERSION, "GPT lyrics translation", frozen=False),
    TranslationVersion(GPT_GENIUS_VERSION, "GPT Genius annotation translation", frozen=False),
    # OPS-project-stabilization Step 2D froze the Claude demand rows.
    TranslationVersion(CLAUDE_DEMAND_VERSION, "Claude demand translation (legacy)", frozen=True),
)

TRANSLATION_WAITING_STATES: Tuple[str, ...] = ("ready", "retryable_error")


@dataclass(frozen=True)
class QueueDef:
    """One DB-backed queue, read as an aggregate by a single SELECT.

    `sql` must return rows of (state, n, oldest_waiting, last_update,
    stale_running) — one row per state — and nothing else. It is a constant;
    no caller input reaches it.
    """

    key: str
    table: str
    label: str
    frozen: bool
    waiting_states: Tuple[str, ...]
    sql: str


QUEUES: Tuple[QueueDef, ...] = (
    QueueDef(
        key="lyrics_album_jobs",
        table="lyrics_album_jobs",
        label="Lyrics album demand jobs",
        frozen=False,
        waiting_states=("pending",),
        sql="""
            SELECT CASE WHEN cancelled THEN 'cancelled'
                        WHEN enumeration_complete THEN 'complete'
                        ELSE 'pending' END AS state,
                   count(*) AS n,
                   min(created_at) FILTER (WHERE NOT cancelled AND NOT enumeration_complete),
                   max(updated_at),
                   NULL::bigint
            FROM lyrics_album_jobs GROUP BY 1
        """,
    ),
    QueueDef(
        key="lyrics_album_tracks",
        table="lyrics_album_tracks",
        label="Lyrics album tracks awaiting a source",
        frozen=False,
        waiting_states=("source_pending",),
        # No created_at on this table: "oldest waiting" is the oldest last touch
        # of a pending row.
        sql="""
            SELECT source_state, count(*),
                   min(updated_at) FILTER (WHERE source_state = 'source_pending'),
                   max(updated_at),
                   NULL::bigint
            FROM lyrics_album_tracks GROUP BY 1
        """,
    ),
    QueueDef(
        key="lyrics_artist_discographies",
        table="lyrics_artist_discographies",
        label="Artist discography enumeration",
        frozen=False,
        waiting_states=("enumerating",),
        sql="""
            SELECT CASE WHEN complete THEN 'complete' ELSE 'enumerating' END,
                   count(*),
                   min(created_at) FILTER (WHERE NOT complete),
                   max(updated_at),
                   NULL::bigint
            FROM lyrics_artist_discographies GROUP BY 1
        """,
    ),
    QueueDef(
        key="album_research",
        table="album_research",
        label="Album research notes",
        frozen=False,
        waiting_states=("queued",),
        # A running row older than 20 minutes is reclaimable by the worker's own
        # rule (models.AlbumResearch), so it is what "stuck" means here.
        sql="""
            SELECT status, count(*),
                   min(requested_at) FILTER (WHERE status = 'queued'),
                   GREATEST(max(requested_at), max(started_at), max(finished_at)),
                   count(*) FILTER (WHERE status = 'running'
                                    AND started_at < now() - interval '20 minutes')
            FROM album_research GROUP BY 1
        """,
    ),
    QueueDef(
        key="genre_backfill_requests",
        table="genre_backfill_requests",
        label="Genre backfill requests (장르 채우기)",
        frozen=False,
        waiting_states=("pending",),
        sql="""
            SELECT CASE WHEN finished_at IS NOT NULL THEN 'finished'
                        WHEN claimed_at IS NOT NULL THEN 'claimed'
                        ELSE 'pending' END,
                   count(*),
                   min(requested_at) FILTER (WHERE finished_at IS NULL AND claimed_at IS NULL),
                   GREATEST(max(requested_at), max(claimed_at), max(finished_at)),
                   NULL::bigint
            FROM genre_backfill_requests GROUP BY 1
        """,
    ),
    # The GPT worker's other two inputs (gpt_queue_worker.CANDIDATES): manual
    # lyrics requests and pending Genius annotations. Rows the worker's filter
    # can never pick up are counted under a separate `*_ineligible` state, so a
    # permanent backlog does not read as a stuck worker. The retired Claude pollers
    # drained the same tables, so the RFC's Step 1 table C called them frozen; they
    # are not — the GPT worker reads and publishes to both since 2026-10-09.
    QueueDef(
        key="track_lyrics_translations",
        table="track_lyrics_translations",
        label="Manual lyrics translation requests",
        frozen=False,
        waiting_states=("requested",),
        sql="""
            SELECT CASE WHEN x.status = 'requested' AND t.spotify_id IS NULL
                        THEN 'requested_ineligible' ELSE x.status END,
                   count(*),
                   min(x.requested_at) FILTER (WHERE x.status = 'requested' AND t.spotify_id IS NOT NULL),
                   max(x.updated_at),
                   NULL::bigint
            FROM track_lyrics_translations x LEFT JOIN tracks t ON t.id = x.track_id
            GROUP BY 1
        """,
    ),
    QueueDef(
        key="track_genius_annotations",
        table="track_genius_annotations",
        label="Genius annotation translations",
        frozen=False,
        waiting_states=("pending",),
        sql="""
            SELECT CASE WHEN a.translation_status = 'pending' AND NOT e.eligible
                        THEN 'pending_ineligible' ELSE a.translation_status END,
                   count(*),
                   min(a.created_at) FILTER (WHERE a.translation_status = 'pending' AND e.eligible),
                   max(a.updated_at),
                   NULL::bigint
            FROM track_genius_annotations a
            CROSS JOIN LATERAL (SELECT (
                btrim(coalesce(a.body_source, '')) <> ''
                AND EXISTS (SELECT 1 FROM tracks t WHERE t.id = a.track_id AND t.spotify_id IS NOT NULL)
                AND EXISTS (SELECT 1 FROM track_genius_songs g
                            WHERE g.track_id = a.track_id AND g.match_status = 'matched')
            ) AS eligible) e
            GROUP BY 1
        """,
    ),
)

# Per (translator_version, status) — folded into one queue per version.
TRANSLATION_WORK_SQL = """
    SELECT translator_version, status, count(*),
           min(created_at) FILTER (WHERE status IN ('ready', 'retryable_error')),
           max(updated_at),
           count(*) FILTER (WHERE status = 'running' AND lease_until < now())
    FROM lyrics_translation_work GROUP BY 1, 2
"""

HEARTBEATS_SQL = """
    SELECT agent_key, observed_at, enabled, last_outcome, details
    FROM automation_heartbeats ORDER BY agent_key
"""


Group = Literal["aws_schedule", "aws_scheduler", "aws_queue_message", "local_agent", "other_scheduler"]
Location = Literal["aws", "mac", "github"]
DeclaredState = Literal["active", "disabled", "obsolete", "manual_only", "consumer_only", "dropped"]


@dataclass(frozen=True)
class JobDef:
    key: str
    group: Group
    name: str
    purpose: str
    trigger: str
    executor: str
    location: Location
    declared_state: DeclaredState
    timezone: Optional[str] = None
    drains: Tuple[str, ...] = ()
    queue_keys: Tuple[str, ...] = ()
    ai_provider: Optional[str] = None
    note: Optional[str] = None
    # Local agents only: the short launchd label (`com.myblog.` removed) the
    # agent writes as `automation_heartbeats.agent_key`, and its staleness bound.
    heartbeat_key: Optional[str] = None
    stale_after_seconds: Optional[int] = None
    controls: Tuple[str, ...] = field(default=())  # none until Step 3


def _cron(key: str, name: str, purpose: str, rule: str, schedule: str, *, state: DeclaredState = "active", drains=(), queue_keys=(), note=None, executor="blogWorkerLambda"):
    return JobDef(
        key=key,
        group="aws_schedule",
        name=name,
        purpose=purpose,
        trigger=f"EventBridge rule {rule} · {schedule}",
        timezone="UTC",
        executor=executor,
        location="aws",
        declared_state=state,
        drains=tuple(drains),
        queue_keys=tuple(queue_keys),
        note=note,
    )


def _message(key: str, name: str, producers: str, *, state: DeclaredState = "active", drains=(), queue_keys=(), note=None):
    return JobDef(
        key=key,
        group="aws_queue_message",
        name=name,
        purpose=f"blogSQS message consumed by blogWorkerLambda. Producers: {producers}",
        trigger="SQS blogSQS (batch 10, redrive album-sync-dlq after 3 receives)",
        executor="blogWorkerLambda",
        location="aws",
        declared_state=state,
        drains=tuple(drains),
        queue_keys=tuple(queue_keys),
        note=note,
    )


def _agent(key: str, name: str, purpose: str, schedule: str, interval_seconds: int, *, state: DeclaredState, drains=(), queue_keys=(), ai_provider=None, note=None, executor="launchd on the owner's Mac"):
    return JobDef(
        key=key,
        group="local_agent",
        name=name,
        purpose=purpose,
        trigger=f"launchd com.myblog.{key} · {schedule}",
        timezone="Asia/Seoul",
        executor=executor,
        location="mac",
        declared_state=state,
        drains=tuple(drains),
        queue_keys=tuple(queue_keys),
        ai_provider=ai_provider,
        note=note,
        heartbeat_key=key,
        stale_after_seconds=stale_after(interval_seconds),
    )


_GPT_QUEUES = (
    translation_queue_key(GPT_LYRICS_VERSION),
    translation_queue_key(GPT_GENIUS_VERSION),
    "track_lyrics_translations",
    "track_genius_annotations",
    # CANDIDATES' album branch links source_pending/linked rows to work rows.
    "lyrics_album_tracks",
)

JOBS: Tuple[JobDef, ...] = (
    # ── A. AWS schedules (EventBridge default bus) ─────────────────────────────
    _cron("musicbrainz_alias_fill", "MusicBrainz alias fill", "Fill artist aliases from MusicBrainz", "worker-musicbrainz-alias-generation", "rate(15 min)"),
    _cron("spotify_listening", "Spotify listening sync", "Record the owner's Spotify listening", "worker-spotify-listening-sync", "rate(1 h)"),
    _cron("spotify_member_poll", "Spotify member poll", "Poll connected members' Spotify activity", "worker-spotify-member-poll", "rate(15 min)"),
    _cron("lastfm_recent_tracks", "Last.fm recent tracks", "Record recent tracks from Last.fm", "worker-lastfm-recent-tracks", "rate(15 min)"),
    _cron("release_upcoming_poll_musicbrainz", "Upcoming releases (MusicBrainz)", "Poll MusicBrainz for upcoming releases", "worker-release-upcoming-mb", "rate(1 h)"),
    _cron("release_upcoming_poll_itunes", "Upcoming releases (iTunes)", "Poll iTunes for upcoming releases", "worker-release-upcoming-itunes", "rate(30 min)"),
    _cron("album_ingest", "Album catalog ingest", "Ingest new albums into the catalog", "worker-album-catalog-ingest", "rate(1 day)"),
    _cron("spotify_saved_tracks_incremental", "Spotify saved tracks (incremental)", "Sync newly saved Spotify tracks", "worker-spotify-saved-tracks-incremental", "cron(0 18 * * ? *)"),
    _cron("spotify_saved_tracks_full", "Spotify saved tracks (full)", "Full resync of saved Spotify tracks", "worker-spotify-saved-tracks-full", "cron(0 19 ? * SUN *)"),
    _cron("lyrics_incremental", "Lyrics incremental collection", "Collect lyrics for newly synced tracks", "worker-lyrics-incremental", "rate(15 min)"),
    _cron("lyrics_demand_source", "Lyrics demand collection", "Turn member demand into album lyrics jobs", "worker-lyrics-demand-source", "rate(15 min)", drains=("lyrics_album_jobs", "lyrics_album_tracks"), queue_keys=("lyrics_album_jobs", "lyrics_album_tracks")),
    _cron("lyrics_reassessment", "Lyrics reassessment", "Re-check lyrics that need another look", "worker-lyrics-reassessment", "rate(1 day)"),
    _cron("genius_fetch", "Genius fetch", "Fetch Genius songs and annotations", "worker-genius-fetch", "cron(30 * * * ? *)"),
    _cron("artist_photo_backfill", "Artist photo backfill", "Fill missing artist photos", "worker-artist-photo-backfill", "cron(0 20 ? * SUN *)"),
    _cron("isrc_backfill", "ISRC backfill", "Fill missing track ISRCs", "worker-isrc-backfill", "cron(0 21 ? * SUN *)"),
    _cron("youtube_ref_refresh", "YouTube reference refresh", "Refresh YouTube playback references", "worker-youtube-ref-refresh", "cron(10 4 * * ? *)"),
    _cron("backend_warm_ping", "Backend warm ping", "GET /health to keep the backend Lambda warm", "backend-api-warm-ping", "rate(5 min)", executor="ratemymusic-api", note="Keep-warm, not a job."),
    _cron("music_warm_ping", "Music warm ping", "GET /api/music/feed/new-releases to keep the music Lambda warm", "music-api-warm-ping", "rate(5 min)", executor="musicApi", note="Keep-warm, not a job."),
    # ── F. EventBridge Scheduler (console-made, not in infra/) ─────────────────
    JobDef(
        key="scheduler_start_blog_db",
        group="aws_scheduler",
        name="StartBlogDB",
        purpose="Pre-Neon office-hours DB start",
        trigger="EventBridge Scheduler StartBlogDB · cron(0 9 ? * TUE-FRI *)",
        timezone="Asia/Seoul",
        executor="Lambda mydbhandler (deleted)",
        location="aws",
        declared_state="obsolete",
        note="Still fires against a deleted Lambda; retry 0, no DLQ. Deleting it is the owner's call.",
    ),
    JobDef(
        key="scheduler_stop_blog_db",
        group="aws_scheduler",
        name="StopBlogDB",
        purpose="Pre-Neon office-hours DB stop",
        trigger="EventBridge Scheduler StopBlogDB · cron(0 18 ? * TUE-FRI *)",
        timezone="Asia/Seoul",
        executor="Lambda mydbhandler (deleted)",
        location="aws",
        declared_state="obsolete",
        note="Still fires against a deleted Lambda; retry 0, no DLQ. Deleting it is the owner's call.",
    ),
    # ── B. blogSQS message types ───────────────────────────────────────────────
    _message("sqs_album_sync", "Album sync", "music sync-requests, backend catalog sync, worker fan-out, manual import script"),
    _message("sqs_spotify_refresh", "Spotify refresh", "backend library manual refresh"),
    _message("sqs_spotify_library_sync", "Spotify library sync", "backend buckets / me"),
    _message("sqs_spotify_saved_tracks_sync", "Spotify saved tracks sync", "none besides the EventBridge rules", state="consumer_only"),
    _message("sqs_spotify_follow_import", "Spotify follow import", "backend tracked-artists; worker self-chain (one delayed rerun)"),
    _message("sqs_spotify_follow_ingest", "Spotify follow ingest", "worker fan-out"),
    _message("sqs_lyrics_member_bootstrap", "Lyrics member bootstrap", "backend integrations (member connects Spotify)"),
    _message("sqs_lyrics_discography_enumerate", "Discography enumeration", "worker self-chain (delayed)", drains=("lyrics_artist_discographies",), queue_keys=("lyrics_artist_discographies",)),
    _message("sqs_lyrics_incremental", "Lyrics incremental (after album sync)", "worker after album sync"),
    _message("sqs_genius_fetch_album", "Genius fetch for one album", "local research-poller nudge", state="disabled", note="Disabled with its producer (research-poller)."),
    _message("sqs_manual_overrides", "Lyrics reassessment / demand source / artist photo with overrides", "no producer on main", state="manual_only"),
    _message("sqs_dropped_jobs", "isrc_backfill / youtube_ref_refresh / disc_no_backfill", "no producer", state="dropped", note="The consumer has no branch for these: logged as \"Unknown message format\" and deleted unrun. Only a direct Lambda invoke runs them."),
    # ── C/D. Local launch agents (owner's Mac) ─────────────────────────────────
    _agent("gpt-translate-poller", "GPT translation worker", "Translate lyrics and Genius annotations with the local GPT worker", "every 60 s", 60, state="active", drains=(TRANSLATION_WORK_TABLE, "track_lyrics_translations", "track_genius_annotations", "lyrics_album_tracks"), queue_keys=_GPT_QUEUES, ai_provider="OpenAI ChatGPT plan (local worker)", note="Plan usage on chatgpt.com is external; the page can only show the worker's own totals."),
    _agent("buckit-nightly", "Buckit nightly drafts", "Draft reports from checked review-bucket memos", "daily 03:00", DAY, state="disabled", ai_provider="Claude CLI (subscription)", note="Not reloaded since the 2026-09-27 reboot; OPS-project-stabilization Step 2D gates the reload."),
    _agent("research-poller", "Album research poller", "Drain album research requests", "every 60 s", 60, state="disabled", drains=("album_research",), queue_keys=("album_research",), ai_provider="Claude CLI (subscription)", note="Not reloaded since the 2026-09-27 reboot; the bucket research_mode producer is still live."),
    _agent("genre-heal-poller", "Genre heal poller", "Drain 장르 채우기 requests through the genre backfill pipeline", "every 30 min", 1800, state="disabled", drains=("genre_backfill_requests",), queue_keys=("genre_backfill_requests",), ai_provider="Claude CLI (subscription)", note="Not reloaded since the 2026-09-27 reboot; the 장르 채우기 button is still live."),
    _agent("genre-backfill", "Genre backfill", "Daily genre pipeline over the catalog (shared_db backfill_genres.py)", "daily 04:00", DAY, state="disabled", ai_provider="Claude CLI (subscription)", note="Not reloaded since the 2026-09-27 reboot."),
    _agent("lyrics-translate-poller", "Legacy lyrics translation poller", "Legacy Claude lyrics translation", "every 60 s", 60, state="obsolete", drains=("track_lyrics_translations", TRANSLATION_WORK_TABLE), queue_keys=(translation_queue_key(CLAUDE_DEMAND_VERSION),), ai_provider="Claude CLI (subscription)", note="Retired by Chat translation: uninstalled, and the script returns before DB work. The GPT worker now drains track_lyrics_translations; the claude.sonnet/v2 work rows stay frozen."),
    _agent("genius-translate-poller", "Legacy Genius translation poller", "Legacy Claude Genius annotation translation", "every 60 s", 60, state="obsolete", drains=("track_genius_annotations",), ai_provider="Claude CLI (subscription)", note="Retired: uninstalled, and the script returns before DB work. The GPT worker now drains its table."),
    # ── E. Other schedulers ────────────────────────────────────────────────────
    JobDef(
        key="verifier_drift",
        group="other_scheduler",
        name="Cognito verifier drift check",
        purpose="Diff the backend and music auth.py copies on main",
        trigger="GitHub Actions verifier-drift.yml · cron(20 18 * * *)",
        timezone="UTC",
        executor="GitHub Actions (myblog-workspace)",
        location="github",
        declared_state="active",
        note="A repository guard, not a site job.",
    ),
)
