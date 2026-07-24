# Blogstudio

세 개의 한국문화 블로그(Blogger)를 자동으로 기획·작성·발행하는 로컬 프로그램. 캘린더 기반 GUI(`blog_studio/`)에서 로컬 LLM(Ollama)으로 글을 생성하고, `publisher/`가 Blogger API·Playwright로 실제 발행·이미지 업로드를 처리한다.

전체 기능 설명은 [`blog_studio/오토블로그_프로그램_정리.md`](blog_studio/오토블로그_프로그램_정리.md) 참고.

## 폴더 구조
```
blog_studio/   GUI + 글 생성·스케줄 로직
publisher/     Blogger 발행 + 이미지 업로드(Playwright)
```
두 폴더는 항상 형제 폴더로 함께 있어야 한다(`blog_studio`가 `publisher`를 상대경로로 참조).

## 최초 설치(윈도우·맥 공통)
1. Python 3.10+ 설치
2. 의존성 설치:
   ```bash
   pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client requests pillow playwright
   playwright install chromium
   ```
   (Claude API로 글을 생성하려면 `pip install anthropic`도 추가)
3. Ollama 설치·실행(로컬 LLM) — https://ollama.com
4. 맥에서 사진 태그(XMP)를 쓰려면: `brew install exiftool`
5. **`publisher/client_secrets.json`을 직접 준비**(Google Cloud Console에서 OAuth 데스크톱 클라이언트 발급) — 이 파일은 저장소에 포함되지 않는다(공개 저장소라 절대 커밋 안 함). 컴퓨터마다 각자 발급받거나, 신뢰할 수 있는 사설 채널로 직접 옮길 것.
6. `blog_studio/블로그스튜디오.command`(맥) 또는 `블로그스튜디오.bat`(윈도우) 실행 → 처음 실행 시 블로그를 등록하고 구글 로그인.

## 주의
- `blog_studio/profiles/`, `publisher/token.json`, `publisher/browser_profile/` 등은 컴퓨터마다 로컬로 생성되는 사용자 데이터·인증정보라 git에 포함하지 않는다(`.gitignore` 참고). 윈도우·맥을 오갈 때 코드는 git으로 동기화하고, 각 컴퓨터는 자체적으로 구글 로그인해서 쓴다.
