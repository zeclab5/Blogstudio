# -*- coding: utf-8 -*-
"""
sheets_export.py — 촬영 목록(샷 리스트)을 구글 시트로 전송.

기존 Blogger OAuth와 같은 client_secrets.json을 쓰되, '구글 시트' 권한은 별도 토큰
(token_sheets.json)으로 따로 받습니다(기존 Blogger 토큰에는 영향 없음).
처음 한 번만 브라우저에서 권한 동의가 필요하고, 이후엔 자동입니다.

export_shot_list(blog, date, topic, shots, log) → 생성된 시트 URL
"""

import blog_core as core   # publisher 경로 등록 + 공유 OAuth(client_secrets) 경로 제공

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TOKEN_SHEETS = core.PUBLISHER_DIR / "token_sheets.json"
# 시트 전용 OAuth 클라이언트가 있으면 그것을 우선 사용(없으면 Blogger와 공유).
_SHEETS_SECRETS = core.PUBLISHER_DIR / "client_secrets_sheets.json"
SECRETS_FILE = _SHEETS_SECRETS if _SHEETS_SECRETS.exists() else core.SHARED_SECRETS


def get_sheets_credentials():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    creds = None
    if TOKEN_SHEETS.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_SHEETS), SHEETS_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not SECRETS_FILE.exists():
                raise RuntimeError(
                    f"{SECRETS_FILE} 가 없습니다. (Blogger 발행에 쓰는 client_secrets.json 필요)")
            flow = InstalledAppFlow.from_client_secrets_file(str(SECRETS_FILE), SHEETS_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_SHEETS.write_text(creds.to_json())
    return creds


def _shot_rows(blog: str, date_str: str, topic: str, shots: list) -> list:
    """시트에 넣을 2차원 값 목록(헤더+행). API 없이도 검증 가능하게 분리."""
    rows = [
        [f"촬영 목록 — {blog}"],
        [f"주제: {topic}", f"날짜: {date_str}"],
        [],
        ["#", "위치", "제목", "촬영 가이드(한국어)", "촬영 가이드(영어)",
         "영어 검색어", "찍었나요?", "사진 파일명"],
    ]
    for i, s in enumerate(shots, 1):
        rows.append([
            i, s.get("slot", ""), s.get("heading", ""),
            s.get("description_ko", ""), s.get("description_en", ""),
            s.get("search_en", ""), "", "",
        ])
    return rows


def export_shot_list(blog: str, date_str: str, topic: str, shots: list, log=print) -> str:
    """촬영 목록을 새 구글 시트로 만들고 URL을 반환."""
    creds = get_sheets_credentials()
    from googleapiclient.discovery import build
    svc = build("sheets", "v4", credentials=creds)
    title = f"촬영목록 {blog} {date_str}".strip()
    ss = svc.spreadsheets().create(
        body={"properties": {"title": title},
              "sheets": [{"properties": {"title": "촬영목록"}}]}).execute()
    sid = ss["spreadsheetId"]
    url = ss.get("spreadsheetUrl", f"https://docs.google.com/spreadsheets/d/{sid}")
    svc.spreadsheets().values().update(
        spreadsheetId=sid, range="촬영목록!A1",
        valueInputOption="RAW", body={"values": _shot_rows(blog, date_str, topic, shots)}
    ).execute()
    log(f"   📊 구글 시트 생성 완료: {url}")
    return url
