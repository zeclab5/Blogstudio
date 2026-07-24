# SEO 개선 로드맵 (작성 2026-06-28)

## 0. 먼저 짚을 것 — 두 분석 자료의 한계

- **Gemini 공유링크**(`https://share.gemini.google/pK0M9fQkVYZU`)는 로그인 화면만 나오고 실제 평가 내용을 가져올 수 없었습니다(인증 필요). 붙여주신 본문 보고서로 대체 분석했습니다.
- 그 보고서는 스스로 **"실제 블로그 접속이 제한되어 일부 내용은 화면 분석을 기반으로 합니다"**라고 밝히고 있습니다. 즉 표에 나온 SEO 점수·일부 URL·작성자명 등은 추정/생성된 값일 수 있습니다.
- 그래서 `k-culture-dictionary.blogspot.com`의 실제 글 3개(최신글 포함)를 직접 열어 사실을 확인하고, `blog_studio`/`publisher` 코드도 같이 점검했습니다. 아래는 그 결과로 보정된 진단입니다.

## 1. 이미 해결되어 있는 것 (다시 손댈 필요 없음)

| 보고서 지적 | 실제 코드/라이브 확인 결과 |
|---|---|
| Lazy 로딩 미적용 | `publish_today.py:168`에 `loading="lazy"` 이미 적용됨 |
| 메타 디스크립션 부재 | `searchDescription`을 발행 시 API로 항상 저장함 (`publish_today.py:217~244`). 페이지 텍스트만 보는 도구로는 `<head>`가 안 보여서 "없다"로 오인하기 쉬움 |
| FAQ/실용정보 없음 | 라이브 최신글(6/26~6/27)에 **FAQ + 실용정보 표 모두 존재** 확인. `blog_core.py`의 `_gen_extras`(1241~1284행 부근)가 이미 생성 |
| 라벨/태그 부족 | 라이브에서 태그 50개 이상 확인됨 |
| 한/영 상호링크 없음 | `_lang_link_header`(`blog_core.py:2521`)로 이미 양방향 링크 중 |

## 2. 실제로 비어 있는 것 (진짜 작업 대상)

라이브 글 직접 열람 + 코드 확인으로 둘 다 일치한 진짜 공백:

1. **본문 내부 링크 0개** — 시리즈/관련 글로 이어지는 링크가 본문 안에 전혀 없음(사이드바 "인기 게시물"만 있음)
2. **이미지 파일명이 의미 없음** — `upload_via_browser.py:214`에서 한글 파일명을 정규식으로 ASCII만 남기는데, 한글 파일명은 전부 걸러져 `photo_12345.jpg`식 무의미한 이름이 됨
3. **WebP 변환 없음** — 업로드 전 압축은 하지만 포맷은 JPG 유지
4. **Schema(JSON-LD) 전혀 없음** — Article/FAQPage/Breadcrumb 구조화 데이터 없음
5. **저자 소개(E-E-A-T) 없음** — "ZEO/누리" 기여자 이름만 있고 약력·신뢰 신호 없음
6. **브레드크럼/카테고리 메뉴 없음** — 라벨 위젯만 있고 상단 메뉴·계층 내비게이션 없음
7. **시리즈 다음/이전 편이 텍스트 언급뿐, 실제 링크가 아님** — `_series_block`이 AI 프롬프트용 문맥일 뿐 `<a href>`로 안 이어짐 (단, `entry["en_url"]`/`entry["ko_url"]`은 이미 저장되고 있어 — 이어주는 데이터는 이미 존재함)
8. **hreflang 신호 없음** — ko/en을 구글에 명시적으로 알리는 표준 태그 없음(수동 링크만)

## 검증 현황 (2026-06-29)

**결정적 통합 테스트 통과(라이브 발행 불필요)** — LLM·네트워크를 가짜로 끼워 `_generate_multi` 본문 조립 전체를 검증:
- 본문 순서 `핵심요약 → 들어가는말 → 목차 → 소주제(h2 id) → 실용정보 → FAQ → 맺음말 → 참고자료 → ◀이전편 → 관련글` 모두 존재·정렬 ✅
- 관련 글 박스가 시리즈 이전/다음 편 URL을 제외(중복 방지) ✅
- 헬퍼 단위 테스트: 관련글·TOC·요약·시리즈내비·형제검색·역주입로직(멱등)·슬러그충돌감지·구분자·권위링크·Article JSON-LD 전부 ✅

**라이브 발행로만 최종 확인 가능(3가지)** — 실제 Blogger API/공개 발행 필요:
1. JSON-LD `<script>`를 Blogger가 본문에 보존하는지(미보존 시 `seo_schema` OFF)
2. "다음 편 ▶" 역주입(`getByPath`+`update`)이 이전 라이브 글에 실제 반영되는지
3. 슬러그 충돌 시 URL `_숫자`가 실제로 사라지는지

→ **시리즈 2편 연속 발행**로 위 3가지를 한 번에 확인 가능. (발행은 공개 콘텐츠 게시라 사용자가 직접 로그인·실행)

## 2-B. 두 번째 보고서(Gemini 정밀본)로 추가/확정된 것 (2026-06-28 갱신)

두 번째 보고서는 라이브 본문을 실제로 읽은 정황이 뚜렷(URL 난수·오방색·산복도로 등 정확). 코드/라이브와 대조한 결과:

- **(신규·확정) URL 끝 난수 `_숫자` 접미사** — `_url_matches_slug`(publish_today.py:265)가 `slug in fname`이면 통과시켜, 슬러그 충돌 시 Blogger가 붙인 `...-en_01960023327`가 영구히 남음. 충돌 원인=같은 주제 재발행 또는 35자 잘림 동일화. → 충돌 감지 시 슬러그에 구분자(-2 등) 부여 후 재발행.
- **(신규·유효) 본문 최상단 핵심요약(역피라미드) 없음** — 긴 서론으로 시작. 글머리 TL;DR 1~2문장 필요.
- **(신규·유효) 목차(TOC)·점프링크 없음** — 소제목 `<h2>`들을 모은 앵커 목차 블록을 도입부 뒤에 자동 삽입 가능(코드로).
- **(신규·유효) 아웃바운드 권위 링크 없음** — 문화재청/유네스코 등 출처 외부링크 부재. 신뢰 신호로 쉬운 개선.
- **(신규·전략) "Dictionary" 정체성 불일치** — 이름은 사전인데 긴 에세이뿐. k-culture-dictionary는 v6에서 원래 "백과/정보형"이므로, **짧은 용어정의(glossary) 글**(온돌·단청·한복·템플푸드·K-드라마 클리셰 등)이 이름·검색의도에 더 맞음. 에세이는 k-arts-travel로 분리.
- **(보고서 오류) 메타 디스크립션·Alt 누락** — 둘 다 이미 코드가 처리 중(searchDescription 저장+재검증, alt 자동생성). 단 일부 글은 본문 이미지가 0장인 별개 문제 있음.
- **(비권장) WordPress/Next.js 이전** — 비개발자 사용자에게 과도·위험. 지적된 SEO 문제는 전부 Blogger 본문/테마 안에서 해결 가능하므로 **이전 불필요**.

## 3. 단계별 실행 계획

### Phase 1 — 1주 내, 순수 코드 수정 (가장 효율 높음)

- [x] **관련 글 내부 링크 박스** ✅(2026-06-28) — `past_titles()`에 `en_url`/`ko_url` 추가(blog_core.py), `_related_block()` 헬퍼 신설, `_generate_multi` 맺음말 뒤에 한/영 "관련 글/Related Posts" 박스 삽입. 과거 발행 글 중 URL 있는 것 최신 4개. 미발행 글 자동 제외. (라벨 매칭 정밀화는 추후)
- [x] **목차(TOC) + 점프 링크** ✅(2026-06-28) — `_toc_block()` 헬퍼 신설, 도입부 뒤에 한/영 목차 삽입, 소주제 `<h2 id="sN">` 앵커 부여. 소주제 3개 미만이면 생략.
- [x] **시리즈 이전/다음 편 실링크화** ✅(2026-06-28) — A단계: 생성 시 `generate_post`가 이전 편 entry의 `en_url/ko_url`을 찾아 series_ctx에 보강 → `_series_nav_block()`이 "◀ 이전 편" 실링크 삽입(맺음말 뒤·관련글 앞). B단계: `publish_date` 발행 성공 후 `_inject_next_into_prev()`가 이전 편 라이브 글에 "다음 편 ▶" 역주입(Blogger `getByPath`+`update`, `<!-- SERIES_NEXT -->` 센티넬·`next_link_injected` 플래그로 멱등, searchDescription 복구, 실패해도 발행 무영향). 양방향 완성·LLM 호출 없음.
- [x] **글머리 TL;DR 핵심 요약(역피라미드)** ✅(2026-06-28) — `_outline_prompt`에 `summary_ko/summary_en`(1~2문장, 결론 먼저) 필드+규칙 추가, `_summary_block()` 헬퍼 신설, 본문 최상단(도입부 앞)에 "핵심 요약/In Short" 박스 삽입. 개요에 필드 누락 시 자동 생략. 본문 순서: 요약→들어가는말→목차→소주제.
- [x] **아웃바운드 권위 링크(참고 자료)** ✅(2026-06-28) — `factcheck.grounding_sources()`가 네이버 백과사전 결과의 **실제 출처 링크**(지어낸 URL 아님)를 수집 → `_authority_block()`이 맺음말 뒤에 "참고 자료/References" 박스(`target=_blank rel=noopener`). 매 글 같은 포털을 박는 templated 풋프린트 위험 없음. **네이버 키 있을 때만**(없으면 생략).
- [x] **제목 키워드 앞배치** ✅ 기존 충족 — `_outline_prompt`가 이미 en/ko 제목 모두 "핵심 키워드를 맨 앞에" + SEO 규칙줄로 지시 중. 프롬프트 비대화(출력 불안정) 방지 위해 중복 문구 미추가.
- [x] **URL 난수 접미사 방지** ✅(2026-06-28) — `publish_with_slug`(publish_today.py) 재작성: 발행 후 URL에 `_숫자`(Blogger 충돌 표식)가 생기면 그 글을 지우고 슬러그에 구분자(`-2`,`-3`..)를 `-en/-ko` 앞에 넣어 최대 3회 재발행(`_is_slug_collision`/`_differentiate_slug`). 단순 잘림은 충돌로 보지 않아 불필요 재발행 없음.
- [~] ~~이미지 파일명 SEO화~~ — **보류**: Blogger는 업로드 파일명을 버리고 불투명한 CDN URL을 발급해 효과 거의 없음(WordPress 전용 기법). alt·캡션은 이미 처리됨.

### Phase 2 — 2~4주, 중간 난이도

- [x] **Schema(JSON-LD) 삽입 — BlogPosting** ✅(2026-06-29) — `_article_jsonld()`가 발행 시점(사진 URL·발행일 확정)에 headline/description/inLanguage/datePublished/image/author/publisher로 `<script type="application/ld+json">`를 한·영 본문에 추가(`publish_date`). `settings.seo_schema`(기본 True) 토글로 끌 수 있음. author는 `settings.author_name` 있을 때만. ⚠️ **Blogger가 본문 `<script>`를 보존하는지는 라이브 발행으로 확인 필요**(미보존 시 토글 OFF). FAQPage는 2023년 이후 일반 사이트 리치결과 제한이라 보류.
- [~] ~~WebP 변환~~ — **보류**(2026-06-29): Blogger CDN(googleusercontent)이 원본 포맷과 무관하게 브라우저에 맞춰 WebP로 자동 전송하고, 코드가 이미 `/s1600/`로 크기 통일 → 원본 WebP화해도 실제 전송물·LCP 변화 없음. 반면 새 에디터 업로드는 JPEG 경로에 맞춰져 있어 WebP는 업로드 깨질 위험. (이미지 파일명과 같은 WordPress 전용 항목)
- [x] **저자 소개 블록(E-E-A-T)** ✅(2026-06-29) — `_author_block()`이 본문 맨 끝에 "글쓴이/About the Author" 박스(이름+소개, LLM 호출 없음). 설정 `author_name`·`author_bio_ko`·`author_bio_en`. 이름/소개 없으면 생략, 영문 글에 한국어 소개만 있으면 생략(언어 혼입 방지). JSON-LD의 author(`author_name`)와 짝. **GUI 설정바에 입력칸 3개 + 'JSON-LD 넣기' 체크박스(seo_schema 토글)** 추가.

### Phase 3 — 코드가 아니라 Blogger 테마/레이아웃에서 사용자가 직접 (1회성)

- [ ] 상단 메뉴/카테고리 페이지 만들기 (Blogger 레이아웃 편집기)
- [ ] Google Search Console에 사이트맵 제출
- [ ] 테마에서 포스트 제목 태그를 H1로 (테마 HTML 수정 — 테마 깨질 위험 있어 백업 후 신중히)

### Phase 4 — 장기

- [ ] **기존 발행 글 일괄 보강 도구**: `update_post.py`를 활용해 과거 글에도 관련 글 링크·Schema·저자 소개를 역삽입하는 배치 기능 → GUI에 `[🔧 기존 글 SEO 보강]` 버튼
- [ ] **시리즈 허브 페이지**: 시리즈마다 전체 회차 목차+링크를 모은 글 1개씩 생성

## 참고: 검증에 쓴 실제 라이브 URL
- https://k-culture-dictionary.blogspot.com/2026/06/korea-travel-tips-cultural-en.html (최신글, FAQ 확인됨)
- https://k-culture-dictionary.blogspot.com/2026/06/jeonju-bibimbap-korean-food-en_01960023327.html
