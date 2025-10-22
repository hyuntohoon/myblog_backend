# app/api/routes/posts_publish.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from datetime import date
import base64, re, requests, os

router = APIRouter()  # prefix는 main.py에서 "/api/publish"로 지정

class CreatePostReq(BaseModel):
    title: str = Field(min_length=1)
    body_mdx: str = Field(min_length=1)
    category: str | None = None
    description: str = ""
    posted_date: date

def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "untitled"

def make_mdx_frontmatter(title: str, slug: str, description: str, posted_date: date, category: str | None) -> str:
    return "\n".join([
        "---",
        f"title: {title!r}",
        f"slug: {slug!r}",
        f"description: {description or ''!r}",
        f"date: {posted_date.isoformat()}",
        f"category: {category or ''!r}",
        "draft: false",
        "albumIds: []",
        "---",
        "",
    ])

@router.post("", tags=["publish"])  # 👉 POST /api/publish
def create_post(req: CreatePostReq):
    slug = slugify(req.title)
    path = f"{os.getenv('CONTENT_DIR', 'content/blog')}/{req.posted_date.isoformat()}--{slug}/index.mdx"

    mdx = make_mdx_frontmatter(
        title=req.title,
        slug=slug,
        description=req.description,
        posted_date=req.posted_date,
        category=req.category,
    ) + req.body_mdx.strip() + "\n"

    owner = os.getenv("GITHUB_REPO_OWNER")
    repo = os.getenv("GITHUB_REPO_NAME")
    branch = os.getenv("GITHUB_REPO_BRANCH", "main")
    token = os.getenv("GITHUB_TOKEN")
    print(f"[DEBUG] GitHub owner={owner}, repo={repo}, branch={branch}, token={'set' if token else 'MISSING'}")
    if not all([owner, repo, token]):
        raise HTTPException(500, detail="Missing GitHub environment variables")

    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    payload = {
        "message": f"chore: create post '{slug}'",
        "content": base64.b64encode(mdx.encode("utf-8")).decode("utf-8"),
        "branch": branch,
    }

    r = requests.put(url, json=payload, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    if r.status_code not in (200, 201):
        raise HTTPException(r.status_code, detail=f"GitHub API error: {r.text}")

    return {
        "ok": True,
        "slug": slug,
        "path": path,
        "github_url": f"https://github.com/{owner}/{repo}/blob/{branch}/{path}",
    }