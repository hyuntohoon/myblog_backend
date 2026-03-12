# myblog_backend

> **MyBlog + Music Review** 프로젝트의 블로그 core API — 글·카테고리 CRUD + Cognito 기반 관리자 인증

🔗 **전체 프로젝트 README:** [MyBlog + Music Review](https://github.com/hyuntohoon/myblog_front#관련-리포지토리)

---

## 개요

블로그 도메인(글/카테고리)의 CRUD API와 관리자 권한 처리를 담당합니다. 음악 동기화와 무관하게 안정적으로 운영되어야 하는 core 데이터 영역입니다.

---

## 주요 기능

- **Posts CRUD** — 관리자만 생성·수정, 일반 사용자는 조회
- **Categories 관리** — 카테고리 생성·조회·수정
- **글 메타데이터** — 앨범·아티스트 연결, 평점(0~10), 커버 URL 저장
- **인증·권한** — Cognito JWT 검증을 통한 관리자 전용 엔드포인트 보호

---

## API 엔드포인트

| Method | Path          | 설명          | 인증        |
| ------ | ------------- | ------------- | ----------- |
| `GET`  | `/posts`      | 글 목록 조회  | -           |
| `GET`  | `/posts/:id`  | 글 상세 조회  | -           |
| `POST` | `/posts`      | 글 생성       | Cognito JWT |
| `PUT`  | `/posts/:id`  | 글 수정       | Cognito JWT |
| `GET`  | `/categories` | 카테고리 목록 | -           |
| `POST` | `/categories` | 카테고리 생성 | Cognito JWT |

---

## 기술 스택

| 항목         | 기술                     |
| ------------ | ------------------------ |
| 배포         | AWS Lambda + API Gateway |
| 인증         | AWS Cognito (JWT)        |
| 데이터베이스 | Amazon RDS (PostgreSQL)  |

---

## 서비스 연동

```
myblog_front → myblog_backend : 글/카테고리 CRUD
myblog_backend → RDS          : 데이터 읽기/쓰기
```

---

## 환경 변수

| 변수                   | 설명                  |
| ---------------------- | --------------------- |
| `DATABASE_URL`         | RDS 접속 URL          |
| `COGNITO_USER_POOL_ID` | Cognito User Pool ID  |
| `COGNITO_CLIENT_ID`    | Cognito App Client ID |
| `AWS_REGION`           | AWS 리전              |

---

## 왜 분리했는가

블로그 데이터(글/카테고리)는 음악 동기화와 별개로 **안정적으로 운영**되어야 합니다. Cognito JWT 기반 인증·권한 경계가 뚜렷해서 API를 분리하는 것이 보안상 안전합니다. Music API의 Spotify 장애가 글 조회·작성에 영향을 주지 않습니다.

---

## 관련 리포지토리

| 리포                                                             | 역할                          |
| ---------------------------------------------------------------- | ----------------------------- |
| [`myblog_front`](https://github.com/hyuntohoon/myblog_front)     | 정적 사이트 + 글쓰기 UI       |
| **myblog_backend** (현재)                                        | 글·카테고리 API + 인증        |
| [`myblog_music`](https://github.com/hyuntohoon/myblog_music)     | DB-first 검색 + Sync 트리거   |
| [`myblog_worker`](https://github.com/hyuntohoon/myblog_worker)   | SQS Consumer + Spotify 동기화 |
| [`myblog_publish`](https://github.com/hyuntohoon/myblog_publish) | 정적 사이트 발행              |
