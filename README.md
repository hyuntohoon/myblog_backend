# myblog_backend

> **MyBlog + Music Review** 프로젝트의 블로그 core API — 글·카테고리 CRUD + Cognito 기반 관리자 인증

🔗 **전체 프로젝트 README:** [MyBlog + Music Review](https://github.com/hyuntohoon/myblog_front#관련-리포지토리)

---

## 개요

블로그 도메인(글/카테고리)의 CRUD API와 관리자 권한 처리를 담당합니다. 음악 동기화와 무관하게 안정적으로 운영되어야 하는 core 데이터 영역입니다.

---

## 주요 기능

- **Posts CRUD** — 관리자만 생성·수정·삭제, 일반 사용자는 조회. 발행글 편집·아카이브·복원(restore) 지원
- **Categories 관리** — 카테고리 생성·조회
- **글 메타데이터** — 앨범·아티스트 연결, 평점(0~5, 0.5 단위), 커버 URL 저장
- **Review buckets** — 평론 작성 전 단계 to-review 큐. 사용자 생성 칸반 칼럼 + 아이템 + 드래그앤드롭 reorder + 인라인 작성/발행 (FEAT-review-bucket-board, 2026-06-03)
- **인증·권한** — Cognito JWT 검증을 통한 관리자 전용 엔드포인트 보호
- **Publishing** — `POST /api/publish` 가 글 MDX 를 myblog_front 의 content repo 에 GitHub API 로 커밋 → GitHub Actions 가 Astro 빌드 후 S3 + CloudFront 갱신 (ARCH-11 으로 옛 myblog_publish 서비스에서 흡수)

---

## API 엔드포인트

| Method  | Path                       | 설명                | 인증        |
| ------- | -------------------------- | ------------------- | ----------- |
| `GET`   | `/api/posts`               | 글 목록 조회        | -           |
| `GET`   | `/api/posts/:id`           | 글 상세 조회        | -           |
| `POST`  | `/api/posts`               | 글 생성             | Cognito JWT |
| `PUT`   | `/api/posts/:id`           | 글 수정             | Cognito JWT |
| `PATCH` | `/api/posts/:id/restore`   | 아카이브된 글 복원  | Cognito JWT |
| `DELETE`| `/api/posts/:id`           | 글 삭제 (아카이브 → hard delete) | Cognito JWT |
| `GET`   | `/api/categories`          | 카테고리 목록       | -           |
| `POST`  | `/api/categories`          | 카테고리 생성       | Cognito JWT |
| `POST`  | `/api/publish`             | 글 발행 (MDX 커밋)  | Cognito JWT |
| `POST`  | `/api/metrics/batch`       | 좋아요·댓글 카운트  | -           |
| `GET`   | `/api/buckets`             | 평론 버킷 + 아이템 트리 조회 | -        |
| `POST`  | `/api/buckets`             | 버킷 생성 (`{name, color?}`) | Cognito JWT |
| `PATCH` | `/api/buckets/:bucket_id`  | 버킷 이름/색/position/`is_done` 갱신 | Cognito JWT |
| `DELETE`| `/api/buckets/:bucket_id`  | 버킷 삭제 (아이템 cascade)  | Cognito JWT |
| `POST`  | `/api/buckets/:bucket_id/items` | 앨범 담기 (`{album_id, note?}`, 자동 position) | Cognito JWT |
| `PATCH` | `/api/buckets/:bucket_id/items/:item_id` | note/status/post_id 갱신 | Cognito JWT |
| `DELETE`| `/api/buckets/:bucket_id/items/:item_id` | 아이템 제거 | Cognito JWT |
| `PUT`   | `/api/buckets/reorder`     | 드래그 결과 일괄 반영 (`{buckets:[{id, item_ids:[...]}]}`) | Cognito JWT |

쓰기·발행 엔드포인트는 API Gateway 의 Cognito authorizer 를 통과한 뒤 Lambda 로 들어옴. 조회 엔드포인트는 CloudFront 의 `x-origin-verify` edge guard 경유 (자세한 흐름: 워크스페이스 `CLAUDE.md` 의 "Auth — two entry points" 참조).

---

## 기술 스택

| 항목         | 기술                              |
| ------------ | --------------------------------- |
| 배포         | AWS Lambda + API Gateway          |
| 인증         | AWS Cognito (JWT)                 |
| 데이터베이스 | Neon Serverless Postgres          |
| 도메인 모델  | `myblog-shared-db` (git-pinned)   |

---

## 서비스 연동

```
myblog_front → myblog_backend : 글/카테고리 CRUD + 발행
myblog_backend → Neon         : 데이터 읽기/쓰기 (SQLAlchemy + psycopg)
myblog_backend → GitHub API   : /api/publish 시 MDX 커밋
```

---

## 환경 변수

| 변수                   | 설명                                                |
| ---------------------- | --------------------------------------------------- |
| `SECRETS_ARN`          | AWS Secrets Manager `myblog/backend` 의 ARN (prod). cold-start 1회 fetch + `@lru_cache` |
| `DATABASE_URL`         | Neon 접속 URL (`postgresql+psycopg://...`) — local dev 시 직접 주입 |
| `COGNITO_USER_POOL_ID` | Cognito User Pool ID                                |
| `COGNITO_CLIENT_ID`    | Cognito App Client ID                               |
| `EDGE_SECRET`          | CloudFront → Lambda 진입 시 검증되는 `x-origin-verify` 값 |
| `GITHUB_TOKEN`         | `/api/publish` 가 content repo 에 MDX 커밋할 때 사용  |
| `AWS_REGION`           | AWS 리전                                            |

> 로컬에서는 `ENV=local|dev` 면 Cognito + edge guard 가 우회됩니다. prod 운영 값은 모두 Secrets Manager 에 보관 — 평문 commit 금지.

---

## 왜 분리했는가

블로그 데이터(글/카테고리)는 음악 동기화와 별개로 **안정적으로 운영**되어야 합니다. Cognito JWT 기반 인증·권한 경계가 뚜렷해서 API를 분리하는 것이 보안상 안전합니다. Music API의 Spotify 장애가 글 조회·작성에 영향을 주지 않습니다.

---

## 관련 리포지토리

| 리포                                                                   | 역할                                  |
| ---------------------------------------------------------------------- | ------------------------------------- |
| [`myblog_front`](https://github.com/hyuntohoon/myblog_front)           | 정적 사이트 + 글쓰기 UI               |
| **myblog_backend** (현재)                                              | 글·카테고리 API + 인증 + 발행         |
| [`myblog_music`](https://github.com/hyuntohoon/myblog_music)           | DB-first 검색 + Sync 트리거           |
| [`myblog_worker`](https://github.com/hyuntohoon/myblog_worker)         | SQS Consumer + Spotify 동기화         |
| [`myblog_shared_db`](https://github.com/hyuntohoon/myblog_shared_db)   | 공유 SQLAlchemy 모델 (git-pinned)     |

> 옛 `myblog_publish` 서비스는 ARCH-11 으로 본 레포에 흡수되었고 업스트림은 archived 됨.
