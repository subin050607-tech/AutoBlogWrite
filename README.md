# AutoBlogWrite — 네이버 블로그 자동 작성·발행

주제(키워드)만 등록하면 **글 생성 → 품질검사 → 네이버용 HTML 변환 → 발행 → 이력 기록**까지 자동으로 처리하고,
스케줄러가 사람처럼 불규칙한 간격으로 하루 발행량을 조절합니다.

## 요구사항 정리

| 구분 | 내용 |
|---|---|
| 글 생성 | Claude API로 제목·본문·태그 생성, 블로그 성격/톤/독자/분량/공통 규칙을 설정으로 지정, 주제별 메모·톤·이미지 지정 |
| 주제 관리 | 키워드 큐(SQLite), 우선순위, 파일 일괄 등록, LLM 주제 추천(`suggest`), 중복 키워드 차단 |
| 품질 보장 | 최소/최대 글자수, 소제목 수, 제목 내 키워드, 키워드 밀도 상한(스팸 방지), 금지어, **기존 제목과 유사도(중복 글 방지)** → 실패 시 문제점을 피드백으로 넣어 자동 재생성 |
| 네이버 서식 | `class`/`<style>` 이 제거되므로 인라인 스타일 HTML로 변환, 이미지 업로드·삽입, 태그, AI 작성 고지 문구 |
| 발행 | 네이버 공식 XML-RPC(MetaWeblog) API (`naver`) / HTML 파일 저장 (`file`, 검수·드라이런) |
| 스케줄 | 활동 시간대, 하루 한도, 최소 간격 + 랜덤 지터 |
| 안전장치 | 실패 주제는 `failed` + 사유 기록 후 다음 주제 진행, `preview` 는 이력/큐에 영향 없음, 비밀값은 `.env` 로 분리 |

## 설치

```bash
pip install -e .          # 또는: pip install pyyaml anthropic
autoblog init             # config.yaml, .env 생성
```

`.env` 에 입력:
- `ANTHROPIC_API_KEY`
- `NAVER_BLOG_ID` — blog.naver.com/**아이디**
- `NAVER_API_PASSWORD` — 네이버 블로그 관리 → 글 API(외부 연동) 설정에서 만드는 **API 연동 비밀번호** (로그인 비밀번호가 아닙니다)

```bash
autoblog check            # 네이버 API 연결 확인
```

## 사용법

```bash
autoblog add "제주도 3박4일 여행 코스" --category 여행 --notes "렌터카 기준" --images jeju1.jpg,jeju2.jpg
autoblog add-file topics.txt            # 한 줄에 키워드 하나 (# 주석 가능)
autoblog suggest "캠핑" -n 10 --save    # LLM 주제 추천 후 큐 등록

autoblog preview                        # 발행 없이 output/*.html 로 결과 검수
autoblog post                           # 다음 주제 1건 즉시 발행 (--id N 으로 지정)
autoblog run                            # 스케줄러 상시 실행
autoblog list --status failed           # 큐 / 실패 사유 확인
autoblog history                        # 발행 이력
```

이미지는 `images/<주제ID>/` 또는 `images/` 에서 파일명으로 찾아 네이버에 업로드합니다.
API 키 없이 흐름만 시험하려면 `config.yaml` 에 `llm.provider: mock`, `blog.publisher: file` 로 설정하세요.

cron 으로 돌리려면 `autoblog run` 대신 `*/30 * * * * autoblog post` 처럼 `post` 를 쓸 수 있습니다(이 경우 스케줄 규칙은 적용되지 않음).

## 구조

```
autoblog/
  config.py     설정/.env 로딩        generator.py  Claude·mock 글 생성
  db.py         주제 큐·발행 이력     quality.py    발행 전 품질검사
  render.py     마크다운→네이버 HTML  publishers.py 네이버 XML-RPC / 파일
  pipeline.py   1건 처리 흐름         scheduler.py  시간대·한도·간격
  cli.py        명령행
tests/          pytest (가짜 XML-RPC 서버로 발행 검증)
```

## 주의사항 (꼭 읽어주세요)

- **네이버 XML-RPC 실연동은 이 개발 환경에서 검증하지 못했습니다.** 테스트는 가짜 서버로 호출 형태만 검증합니다. 처음에는 `autoblog check` → `blog.publish: false` 또는 `preview` 로 먼저 확인하세요. 네이버가 API 사양(태그/카테고리 필드 처리 등)을 바꿨다면 `publishers.py` 의 `NaverPublisher` 만 수정하면 됩니다.
- 자동 생성 글을 **검토 없이 대량 발행**하면 네이버 검색 품질 정책(저품질·유사문서)에 불리할 수 있습니다. 하루 한도를 낮게 유지하고 `preview` 로 샘플을 확인하세요.
- 생성 글의 사실관계(장소·가격·영업시간 등)는 모델이 확인할 수 없으니 `--notes` 로 근거 정보를 주거나 직접 검수하세요. 기본값으로 AI 작성 고지 문구가 붙습니다.
- 네이버 이용약관·AI 콘텐츠 관련 정책은 변경될 수 있으니 직접 확인하세요.

## 테스트

```bash
pip install -e .[dev] && pytest
```
