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
) -> str:
    cat = (category or "default").strip() or "default"
    return "\n".join(
        [
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
            "---",
            "",
        ]
    )


def github_put_file(
    owner: str,
    repo: str,
    branch: str,
    path: str,
    content_utf8: str,
    token: str,
) -> requests.Response:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "myblog-backend",
        "Content-Type": "application/json",
    }

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
) -> dict:
    path = f"{content_dir}/{posted_date.isoformat()}--{slug}/index.mdx"
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
