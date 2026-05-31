from __future__ import annotations

import base64
import json
import logging
import re
import unicodedata
from datetime import date
from typing import Optional

import requests

logger = logging.getLogger(__name__)


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "untitled"


def make_mdx_frontmatter(
    title: str,
    slug: str,
    description: str,
    posted_date: date,
    category: Optional[str],
    album_ids: list[str],
    artist_ids: list[str],
    post_id: str,
    album_cover_url: Optional[str] = None,
    rating: Optional[float] = None,
    rating_scale: int = 5,
    best_new: bool = False,
    recommended_track_ids: Optional[list[str]] = None,
    music_review: Optional[dict] = None,
) -> str:
    cat = (category or "default").strip() or "default"
    lines = [
        "---",
        f"title: {title!r}",
        f"slug: {slug!r}",
        f"description: {(description or '')!r}",
        f"date: {posted_date.isoformat()}",
        f"category: {cat!r}",
        "draft: false",
        f"albumIds: {json.dumps(album_ids or [], ensure_ascii=False)}",
        f"artistIds: {json.dumps(artist_ids or [], ensure_ascii=False)}",
        f"postId: {post_id!r}",
        f"albumCover: {json.dumps(album_cover_url or '', ensure_ascii=False)}",
        f"rating: {rating if rating is not None else 'null'}",
        f"ratingScale: {rating_scale}",
        f"bestNew: {'true' if best_new else 'false'}",
        f"recommendedTrackIds: {json.dumps(recommended_track_ids or [], ensure_ascii=False)}",
    ]
    if music_review:
        # JSON is a YAML subset (single-line flow style), so this parses
        # cleanly as a nested map under the `musicReview:` key. zod sees the
        # full object on the read page.
        lines.append(f"musicReview: {json.dumps(music_review, ensure_ascii=False)}")
    lines.extend(["---", ""])
    return "\n".join(lines)


def content_path(content_dir: str, posted_date: date, slug: str) -> str:
    """The canonical MDX path for a post. Single source of truth shared by the
    publish (PUT) and un-publish (DELETE) flows so they always agree."""
    return f"{content_dir}/{posted_date.isoformat()}--{slug}/index.mdx"


def _contents_url(owner: str, repo: str, path: str) -> str:
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"


def _github_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "myblog-backend",
        "Content-Type": "application/json",
    }


def github_put_file(
    owner: str,
    repo: str,
    branch: str,
    path: str,
    content_utf8: str,
    token: str,
) -> requests.Response:
    url = _contents_url(owner, repo, path)
    headers = _github_headers(token)

    r_get = requests.get(url, headers=headers, params={"ref": branch})
    sha = r_get.json().get("sha") if r_get.status_code == 200 else None

    payload: dict = {
        "message": f"chore(post): create or update '{path.split('/')[-1]}'",
        "content": base64.b64encode(content_utf8.encode("utf-8")).decode("utf-8"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    return requests.put(url, headers=headers, data=json.dumps(payload))


def github_delete_file(
    owner: str,
    repo: str,
    branch: str,
    path: str,
    token: str,
) -> dict:
    """Delete a file from the content repo. **Idempotent**: if the file is
    already absent (GET → 404), this is a no-op success — so un-publishing a
    post that was never published (e.g. a draft) is safe. Raises RuntimeError
    on any other GitHub error so the caller can surface a 502."""
    url = _contents_url(owner, repo, path)
    headers = _github_headers(token)

    r_get = requests.get(url, headers=headers, params={"ref": branch})
    if r_get.status_code == 404:
        return {"ok": True, "path": path, "deleted": False, "reason": "absent"}
    sha = r_get.json().get("sha") if r_get.status_code == 200 else None
    if not sha:
        raise RuntimeError(
            f"GitHub API error resolving sha {r_get.status_code}: {r_get.text[:500]}"
        )

    payload = {
        "message": f"chore(post): remove '{path.split('/')[-1]}'",
        "sha": sha,
        "branch": branch,
    }
    r = requests.delete(url, headers=headers, data=json.dumps(payload))
    if r.status_code != 200:
        raise RuntimeError(f"GitHub API error {r.status_code}: {r.text[:500]}")
    return {"ok": True, "path": path, "deleted": True}


def publish_to_github(
    *,
    owner: str,
    repo: str,
    branch: str,
    content_dir: str,
    token: str,
    title: str,
    slug: str,
    description: str,
    posted_date: date,
    category: Optional[str],
    album_ids: list[str],
    artist_ids: list[str],
    post_id: str,
    album_cover_url: Optional[str],
    rating: Optional[float],
    rating_scale: int,
    body_mdx: Optional[str],
    best_new: bool = False,
    recommended_track_ids: Optional[list[str]] = None,
    music_review: Optional[dict] = None,
) -> dict:
    path = content_path(content_dir, posted_date, slug)
    body_content = body_mdx.strip() if body_mdx else ""

    mdx = (
        make_mdx_frontmatter(
            title=title,
            slug=slug,
            description=description,
            posted_date=posted_date,
            category=category,
            album_ids=album_ids,
            artist_ids=artist_ids,
            post_id=post_id,
            album_cover_url=album_cover_url,
            rating=rating,
            rating_scale=rating_scale,
            best_new=best_new,
            recommended_track_ids=recommended_track_ids,
            music_review=music_review,
        )
        + body_content
        + "\n"
    )

    r = github_put_file(owner, repo, branch, path, mdx, token)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub API error {r.status_code}: {r.text[:500]}")

    return {
        "ok": True,
        "slug": slug,
        "path": path,
        "github_url": f"https://github.com/{owner}/{repo}/blob/{branch}/{path}",
    }
