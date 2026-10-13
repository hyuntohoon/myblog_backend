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
from typing import Dict, Literal, Optional, Tuple

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
    # Local agents: the short launchd label (`com.myblog.` removed) the agent
    # writes as `automation_heartbeats.agent_key`. EventBridge rows: the rule
    # name the AWS adapter reads (2d). Both carry the staleness bound.
    heartbeat_key: Optional[str] = None
    aws_rule: Optional[str] = None
    interval_seconds: Optional[int] = None  # EventBridge rows: the nominal schedule gap
    stale_after_seconds: Optional[int] = None
    controls: Tuple[str, ...] = field(default=())  # none until Step 3


def _cron(key: str, name: str, purpose: str, rule: str, schedule: str, interval_seconds: int, *, state: DeclaredState = "active", drains=(), queue_keys=(), note=None, executor="blogWorkerLambda"):
    # `interval_seconds` is the schedule's nominal gap (a weekly cron is 7 days);
    # the AWS adapter (2d) judges delivery staleness by the same 2 × interval,
    # min 15 min rule as the heartbeats.
    return JobDef(
        aws_rule=rule,
        interval_seconds=interval_seconds,
        stale_after_seconds=stale_after(interval_seconds),
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
    _cron("musicbrainz_alias_fill", "MusicBrainz alias fill", "Fill artist aliases from MusicBrainz", "worker-musicbrainz-alias-generation", "rate(15 min)", 900),
    _cron("spotify_listening", "Spotify listening sync", "Record the owner's Spotify listening", "worker-spotify-listening-sync", "rate(1 h)", 3600),
    _cron("spotify_member_poll", "Spotify member poll", "Poll connected members' Spotify activity", "worker-spotify-member-poll", "rate(15 min)", 900),
    _cron("lastfm_recent_tracks", "Last.fm recent tracks", "Record recent tracks from Last.fm", "worker-lastfm-recent-tracks", "rate(15 min)", 900),
    _cron("release_upcoming_poll_musicbrainz", "Upcoming releases (MusicBrainz)", "Poll MusicBrainz for upcoming releases", "worker-release-upcoming-mb", "rate(1 h)", 3600),
    _cron("release_upcoming_poll_itunes", "Upcoming releases (iTunes)", "Poll iTunes for upcoming releases", "worker-release-upcoming-itunes", "rate(30 min)", 1800),
    _cron("album_ingest", "Album catalog ingest", "Ingest new albums into the catalog", "worker-album-catalog-ingest", "rate(1 day)", 86400),
    _cron("spotify_saved_tracks_incremental", "Spotify saved tracks (incremental)", "Sync newly saved Spotify tracks", "worker-spotify-saved-tracks-incremental", "cron(0 18 * * ? *)", 86400),
    _cron("spotify_saved_tracks_full", "Spotify saved tracks (full)", "Full resync of saved Spotify tracks", "worker-spotify-saved-tracks-full", "cron(0 19 ? * SUN *)", 604800),
    _cron("lyrics_incremental", "Lyrics incremental collection", "Collect lyrics for newly synced tracks", "worker-lyrics-incremental", "rate(15 min)", 900),
    _cron("lyrics_demand_source", "Lyrics demand collection", "Turn member demand into album lyrics jobs", "worker-lyrics-demand-source", "rate(15 min)", 900, drains=("lyrics_album_jobs", "lyrics_album_tracks"), queue_keys=("lyrics_album_jobs", "lyrics_album_tracks")),
    _cron("lyrics_reassessment", "Lyrics reassessment", "Re-check lyrics that need another look", "worker-lyrics-reassessment", "rate(1 day)", 86400),
    _cron("genius_fetch", "Genius fetch", "Fetch Genius songs and annotations", "worker-genius-fetch", "cron(30 * * * ? *)", 3600),
    _cron("artist_photo_backfill", "Artist photo backfill", "Fill missing artist photos", "worker-artist-photo-backfill", "cron(0 20 ? * SUN *)", 604800),
    _cron("isrc_backfill", "ISRC backfill", "Fill missing track ISRCs", "worker-isrc-backfill", "cron(0 21 ? * SUN *)", 604800),
    _cron("youtube_ref_refresh", "YouTube reference refresh", "Refresh YouTube playback references", "worker-youtube-ref-refresh", "cron(10 4 * * ? *)", 86400),
    _cron("backend_warm_ping", "Backend warm ping", "GET /health to keep the backend Lambda warm", "backend-api-warm-ping", "rate(5 min)", 300, executor="ratemymusic-api", note="Keep-warm, not a job."),
    _cron("music_warm_ping", "Music warm ping", "GET /api/music/feed/new-releases to keep the music Lambda warm", "music-api-warm-ping", "rate(5 min)", 300, executor="musicApi", note="Keep-warm, not a job."),
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


# ── AWS resources the adapter reads (FEAT-automation-admin 2d) ──────────────────
#
# These names must match what `infra/` declares: the backend role's read grant
# (`infra/iam_roles.tf` backend_automation_read) is scoped to exactly these
# rules, alarms and queues, so a name added here without the infra change is
# AccessDenied (and renders unknown), never a wider grant.


@dataclass(frozen=True)
class AwsRule:
    name: str
    job_key: str
    interval_seconds: int
    stale_after_seconds: int
    # Only worker crons have a FailedInvocations alarm (infra/monitoring.tf
    # for_each over worker_cron); the warm pings deliberately do not.
    alarm: Optional[str]


@dataclass(frozen=True)
class AwsFunction:
    name: str
    label: str


@dataclass(frozen=True)
class AwsQueue:
    name: str
    role: Literal["work", "dead_letter"]
    label: str


AWS_RULES: Tuple[AwsRule, ...] = tuple(
    AwsRule(
        name=j.aws_rule,
        job_key=j.key,
        interval_seconds=j.interval_seconds,
        stale_after_seconds=j.stale_after_seconds,
        alarm=f"{j.aws_rule}-failed-invocations" if j.executor == "blogWorkerLambda" else None,
    )
    for j in JOBS
    if j.aws_rule is not None and j.interval_seconds is not None and j.stale_after_seconds is not None
)

AWS_FUNCTIONS: Tuple[AwsFunction, ...] = (
    AwsFunction("ratemymusic-api", "Backend API"),
    AwsFunction("musicApi", "Music API"),
    AwsFunction("blogWorkerLambda", "Worker (crons + blogSQS)"),
)

AWS_QUEUES: Tuple[AwsQueue, ...] = (
    AwsQueue("blogSQS", "work", "Worker job queue"),
    AwsQueue("album-sync-dlq", "dead_letter", "blogSQS dead letters (after 3 receives)"),
    AwsQueue("worker-cron-dlq", "dead_letter", "Failed async cron invocations"),
)

AWS_ALARMS: Tuple[str, ...] = (
    *(r.alarm for r in AWS_RULES if r.alarm),
    *(f"{f.name}-errors" for f in AWS_FUNCTIONS),
    *(f"{f.name}-throttles" for f in AWS_FUNCTIONS),
    "album-sync-dlq-messages",
    "worker-cron-dlq-messages",
)


# ── queue detail (FEAT-automation-admin 2f) ─────────────────────────────────────
#
# Why rows wait and whether a queue drains, per queue. Same rules as above: the
# SQL is constant (the only bind parameters are `:limit` and, for translation
# work, `:version`), and it runs inside the snapshot's read-only transaction
# shape.
#
# Reasons leave the database as CODES only. `lyrics_translation_work.last_reason`
# holds `engine_validation: <exception text>` for the legacy Claude rows, and
# `album_research.error` / `track_lyrics_translations.error` are raw exception
# text, so the code is cut out in SQL and anything that is not code-shaped comes
# back as `unrecognized`. The raw text never reaches Python, let alone the page.

OLDEST_WAITING_LIMIT = 5
FLOW_WINDOW_HOURS = 24
UNRECOGNIZED_REASON = "unrecognized"

_CODE = "[a-z][a-z0-9_]{0,63}"


def reason_code_sql(column: str) -> str:
    """SQL that maps a reason column to its code, `unrecognized`, or NULL.

    A value is a code when it is exactly `_CODE`, or `_CODE` followed by `:`
    and free text (the writer's own `code: detail` form) — then only the code
    is returned. `column` is always a constant from this module.
    """
    return (
        f"CASE WHEN {column} IS NULL THEN NULL"
        f" WHEN {column} ~ '^{_CODE}(:|$)' THEN substring({column} from '^{_CODE}')"
        f" ELSE '{UNRECOGNIZED_REASON}' END"
    )


_NO_REASON_COLUMN = "This table has no reason column."
_RAW_ERROR_ONLY = "This table records failures only as free text (`error`), which is not shown."

_IN_WINDOW = f"now() - interval '{FLOW_WINDOW_HOURS} hours'"


@dataclass(frozen=True)
class QueueDetailDef:
    """Detail reads for one queue.

    * `reasons_sql` → rows of (state, reason, n, deferred). `state` uses the
      same expression as the snapshot's `QueueDef.sql`, so the two agree;
      `deferred` counts rows whose `next_attempt_at` is still ahead (NULL when
      the table has no retry schedule).
    * `flow_sql` → one row of (entered, left) over the last
      `FLOW_WINDOW_HOURS`. A column is NULL when the table cannot measure it.
    * `oldest_sql` → up to `:limit` waiting rows of (id, title, state, reason,
      attempts, waiting_since, next_attempt_at), oldest first.
    """

    reasons_sql: str
    flow_sql: str
    oldest_sql: str
    entered_basis: str
    left_basis: str
    wait_basis: str
    reasons_note: Optional[str] = None  # None = the table records reason codes


_JOB_STATE = ("CASE WHEN j.cancelled THEN 'cancelled' WHEN j.enumeration_complete THEN 'complete' "
              "ELSE 'pending' END")
_TLT_STATE = ("CASE WHEN x.status = 'requested' AND t.spotify_id IS NULL "
              "THEN 'requested_ineligible' ELSE x.status END")
_GENIUS_ELIGIBLE = """
    CROSS JOIN LATERAL (SELECT (
        btrim(coalesce(a.body_source, '')) <> ''
        AND EXISTS (SELECT 1 FROM tracks t2 WHERE t2.id = a.track_id AND t2.spotify_id IS NOT NULL)
        AND EXISTS (SELECT 1 FROM track_genius_songs g
                    WHERE g.track_id = a.track_id AND g.match_status = 'matched')
    ) AS eligible) e
"""
_GENIUS_STATE = ("CASE WHEN a.translation_status = 'pending' AND NOT e.eligible "
                 "THEN 'pending_ineligible' ELSE a.translation_status END")
_GENRE_STATE = ("CASE WHEN finished_at IS NOT NULL THEN 'finished' "
                "WHEN claimed_at IS NOT NULL THEN 'claimed' ELSE 'pending' END")

QUEUE_DETAILS: Dict[str, QueueDetailDef] = {
    "lyrics_album_jobs": QueueDetailDef(
        reasons_sql=f"""
            SELECT {_JOB_STATE}, {reason_code_sql('j.last_reason')}, count(*),
                   count(*) FILTER (WHERE j.next_attempt_at > now())
            FROM lyrics_album_jobs j GROUP BY 1, 2
        """,
        flow_sql=f"""
            SELECT count(*) FILTER (WHERE created_at >= {_IN_WINDOW}),
                   count(*) FILTER (WHERE (cancelled OR enumeration_complete) AND updated_at >= {_IN_WINDOW})
            FROM lyrics_album_jobs
        """,
        oldest_sql=f"""
            SELECT j.spotify_album_id, al.title, 'pending', {reason_code_sql('j.last_reason')},
                   NULL::int, j.created_at, j.next_attempt_at
            FROM lyrics_album_jobs j LEFT JOIN albums al ON al.id = j.album_id
            WHERE NOT j.cancelled AND NOT j.enumeration_complete
            ORDER BY j.created_at, j.id LIMIT :limit
        """,
        entered_basis="Jobs created in the window.",
        left_basis="Approximate: jobs now complete or cancelled whose last update is in the window.",
        wait_basis="Since the job was created.",
    ),
    "lyrics_album_tracks": QueueDetailDef(
        reasons_sql=f"""
            SELECT source_state, {reason_code_sql('last_reason')}, count(*),
                   count(*) FILTER (WHERE next_attempt_at > now())
            FROM lyrics_album_tracks GROUP BY 1, 2
        """,
        flow_sql=f"""
            SELECT NULL::bigint,
                   count(*) FILTER (WHERE source_state <> 'source_pending' AND updated_at >= {_IN_WINDOW})
            FROM lyrics_album_tracks
        """,
        oldest_sql=f"""
            SELECT lt.job_id::text || ':' || lt.track_id::text, t.title, lt.source_state, {reason_code_sql('lt.last_reason')},
                   NULL::int, lt.updated_at, lt.next_attempt_at
            FROM lyrics_album_tracks lt LEFT JOIN tracks t ON t.id = lt.track_id
            WHERE lt.source_state = 'source_pending'
            ORDER BY lt.updated_at, lt.job_id, lt.track_id LIMIT :limit
        """,
        entered_basis="Not measurable: the table has no created_at.",
        left_basis="Approximate: tracks now linked or not_required whose last update is in the window.",
        wait_basis="Since the row was last touched (the table has no created_at).",
    ),
    "lyrics_artist_discographies": QueueDetailDef(
        reasons_sql=f"""
            SELECT CASE WHEN complete THEN 'complete' ELSE 'enumerating' END,
                   {reason_code_sql('last_reason')}, count(*),
                   count(*) FILTER (WHERE next_attempt_at > now())
            FROM lyrics_artist_discographies GROUP BY 1, 2
        """,
        flow_sql=f"""
            SELECT count(*) FILTER (WHERE created_at >= {_IN_WINDOW}),
                   count(*) FILTER (WHERE complete AND last_complete_at >= {_IN_WINDOW})
            FROM lyrics_artist_discographies
        """,
        oldest_sql=f"""
            SELECT d.spotify_artist_id, ar.name, 'enumerating', {reason_code_sql('d.last_reason')},
                   NULL::int, d.created_at, d.next_attempt_at
            FROM lyrics_artist_discographies d LEFT JOIN artists ar ON ar.spotify_id = d.spotify_artist_id
            WHERE NOT d.complete
            ORDER BY d.created_at, d.spotify_artist_id LIMIT :limit
        """,
        entered_basis="Artists added in the window (a re-opened artist keeps its original created_at and is not counted).",
        left_basis="Artists whose enumeration completed in the window.",
        wait_basis="Since the artist was first added (a re-opened artist keeps that date).",
    ),
    "album_research": QueueDetailDef(
        reasons_sql="SELECT status, NULL::text, count(*), NULL::bigint FROM album_research GROUP BY 1",
        flow_sql=f"""
            SELECT count(*) FILTER (WHERE requested_at >= {_IN_WINDOW}),
                   count(*) FILTER (WHERE finished_at >= {_IN_WINDOW})
            FROM album_research
        """,
        oldest_sql="""
            SELECT r.id::text, al.title, r.status, NULL::text, NULL::int, r.requested_at, NULL::timestamptz
            FROM album_research r LEFT JOIN albums al ON al.id = r.album_id
            WHERE r.status = 'queued'
            ORDER BY r.requested_at, r.id LIMIT :limit
        """,
        entered_basis="Requests made in the window.",
        left_basis="Requests finished (done or failed) in the window.",
        wait_basis="Since the request was made.",
        reasons_note=_RAW_ERROR_ONLY,
    ),
    "genre_backfill_requests": QueueDetailDef(
        reasons_sql=f"SELECT {_GENRE_STATE}, NULL::text, count(*), NULL::bigint FROM genre_backfill_requests GROUP BY 1",
        flow_sql=f"""
            SELECT count(*) FILTER (WHERE requested_at >= {_IN_WINDOW}),
                   count(*) FILTER (WHERE finished_at >= {_IN_WINDOW})
            FROM genre_backfill_requests
        """,
        oldest_sql="""
            SELECT id::text, NULL::text, 'pending', NULL::text, NULL::int, requested_at, NULL::timestamptz
            FROM genre_backfill_requests
            WHERE finished_at IS NULL AND claimed_at IS NULL
            ORDER BY requested_at, id LIMIT :limit
        """,
        entered_basis="Requests made in the window.",
        left_basis="Requests finished in the window.",
        wait_basis="Since the request was made.",
        reasons_note=_NO_REASON_COLUMN,
    ),
    "track_lyrics_translations": QueueDetailDef(
        reasons_sql=f"""
            SELECT {_TLT_STATE}, NULL::text, count(*), NULL::bigint
            FROM track_lyrics_translations x LEFT JOIN tracks t ON t.id = x.track_id
            GROUP BY 1
        """,
        flow_sql=f"""
            SELECT count(*) FILTER (WHERE requested_at >= {_IN_WINDOW}
                                    AND (translated_at IS NULL OR requested_at < translated_at)),
                   NULL::bigint
            FROM track_lyrics_translations
        """,
        oldest_sql="""
            SELECT x.track_id::text, t.title, 'requested', NULL::text, NULL::int,
                   x.requested_at, NULL::timestamptz
            FROM track_lyrics_translations x JOIN tracks t ON t.id = x.track_id
            WHERE x.status = 'requested' AND t.spotify_id IS NOT NULL
            ORDER BY x.requested_at, x.track_id LIMIT :limit
        """,
        # The GPT worker publishes album-demand translations into this table too
        # (gpt_translation_store._publish inserts them straight as 'done', with
        # requested_at = translated_at), so those are excluded from "entered" and
        # "left" cannot tell a drained request from a publish.
        entered_basis="Requests made in the window (results the GPT worker published without a request are excluded).",
        left_basis="Not measurable: GPT album translations publish into this table too, so a finished row cannot be told apart from a drained request.",
        wait_basis="Since the request was made.",
        reasons_note=_RAW_ERROR_ONLY,
    ),
    "track_genius_annotations": QueueDetailDef(
        reasons_sql=f"""
            SELECT {_GENIUS_STATE}, NULL::text, count(*), NULL::bigint
            FROM track_genius_annotations a {_GENIUS_ELIGIBLE}
            GROUP BY 1
        """,
        flow_sql=f"""
            SELECT count(*) FILTER (WHERE created_at >= {_IN_WINDOW}),
                   count(*) FILTER (WHERE translation_status = 'done' AND translated_at >= {_IN_WINDOW})
            FROM track_genius_annotations
        """,
        oldest_sql=f"""
            SELECT a.genius_annotation_id::text, t.title, 'pending', NULL::text, NULL::int,
                   a.created_at, NULL::timestamptz
            FROM track_genius_annotations a {_GENIUS_ELIGIBLE}
            LEFT JOIN tracks t ON t.id = a.track_id
            WHERE a.translation_status = 'pending' AND e.eligible
            ORDER BY a.created_at, a.genius_annotation_id LIMIT :limit
        """,
        entered_basis="Annotations fetched in the window.",
        left_basis="Annotations translated in the window (a re-fetch bumps updated_at, so translated_at is used; failures carry no timestamp).",
        wait_basis="Since the annotation was fetched.",
        reasons_note=_NO_REASON_COLUMN,
    ),
}

# One definition for every translator version; `:version` selects the slice.
TRANSLATION_WORK_DETAIL = QueueDetailDef(
    reasons_sql=f"""
        SELECT status, {reason_code_sql('last_reason')}, count(*),
               count(*) FILTER (WHERE next_attempt_at > now())
        FROM lyrics_translation_work WHERE translator_version = :version
        GROUP BY 1, 2
    """,
    flow_sql=f"""
        SELECT count(*) FILTER (WHERE created_at >= {_IN_WINDOW}),
               count(*) FILTER (WHERE status IN ('done', 'cancelled') AND updated_at >= {_IN_WINDOW})
        FROM lyrics_translation_work WHERE translator_version = :version
    """,
    oldest_sql=f"""
        SELECT w.id::text, t.title, w.status, {reason_code_sql('w.last_reason')}, w.attempts,
               w.created_at, w.next_attempt_at
        FROM lyrics_translation_work w LEFT JOIN tracks t ON t.id = w.track_id
        WHERE w.translator_version = :version AND w.status IN ('ready', 'retryable_error')
        ORDER BY w.created_at, w.id LIMIT :limit
    """,
    entered_basis="Work rows created in the window.",
    left_basis="Approximate: work rows now done or cancelled whose last update is in the window.",
    wait_basis="Since the work row was created.",
)

# The CHECK on lyrics_translation_work.translator_version (ck_lyrics_work_version).
MAX_TRANSLATOR_VERSION_LENGTH = 128
