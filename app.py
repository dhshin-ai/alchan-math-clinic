import base64
import io
import json
import os
import re
import ast
import uuid
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
import anthropic
import streamlit as st
from utils.math_ocr import recognize_handwriting_latex
from utils.solution_db import save_problem_solution, get_problem_solution, list_registered_problems
from utils.pdf_parser import (
    is_pdf_parser_available,
    is_pdf_image_available,
    extract_pdf_text,
    is_scanned_pdf_text,
    parse_bulk_solutions,
    parse_scanned_solutions,
)
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

st.set_page_config(
    page_title="다혜T의 1:1 수학 클리닉",
    page_icon="👩‍🏫",
    layout="centered",  # 모바일 앱 셸 - 480px 폭으로 CSS에서 한 번 더 강제한다
    initial_sidebar_state="expanded",
)

# ==========================================
# 📲 PWA(홈 화면 앱 아이콘) 설정
# manifest.json과 아이콘은 static/ 폴더에 있고, .streamlit/config.toml의
# enableStaticServing = true 덕분에 /app/static/... 경로로 서빙된다 - Streamlit은
# 레포 최상위 파일을 바로 "/manifest.json"으로 서빙해주지 않으므로, 실제로 동작하는
# 이 경로를 쓴다. 학생이 사파리/크롬에서 "홈 화면에 추가"를 하면 주소창 없는
# standalone 앱처럼 열린다.
# ==========================================
st.markdown(
    """
    <link rel="manifest" href="/app/static/manifest.json">
    <meta name="theme-color" content="#3A449A">
    <link rel="apple-touch-icon" href="/app/static/icon-192.png">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-title" content="다혜T 수학">
    """,
    unsafe_allow_html=True,
)

try:
    api_key = st.secrets["ANTHROPIC_API_KEY"]
except Exception:
    api_key = None

# 이 프로젝트는 전부 Anthropic Claude(비전 포함) 하나로 통일되어 있다 - Gemini 등 다른
# 모델/SDK는 쓰지 않는다. 사진 분석(질문 클리닉/오답 첨삭/칠판 판서 추출) 전부 이 모델을 쓴다.
MODEL_NAME = "claude-sonnet-4-5"

# Streamlit Cloud 서버 시간이 UTC일 수 있으므로, 날짜/요일 표시는 항상 zoneinfo로
# 'Asia/Seoul' 기준 시각을 명시적으로 가져온다 (고정 오프셋이 아니라 이름 있는 시간대).
KST = ZoneInfo("Asia/Seoul")
WEEKDAY_KO = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]


def get_today_kst_label():
    """'2026년 10월 13일 화요일'처럼 한국 시간 기준 오늘 날짜+요일 문자열을 만든다."""
    now = datetime.now(KST)
    return f"{now.year}년 {now.month}월 {now.day}일 {WEEKDAY_KO[now.weekday()]}"

try:
    MASTER_PW = st.secrets["STUDENT_CODE"]
except Exception:
    MASTER_PW = "1234"

KAKAO_CLIENT_ID = st.secrets.get("KAKAO_CLIENT_ID", "")
KAKAO_REDIRECT_URI = st.secrets.get("KAKAO_REDIRECT_URI", "https://math.studyground123.com/")
TEACHER_DASHBOARD_PASSWORD = st.secrets.get("TEACHER_DASHBOARD_PASSWORD", "dahye1234")

PASS_GRADE_LEVELS = ["중1", "중2", "중3", "고1(원종)", "고1(수주,덕산)", "고2", "고3"]
PASS_GRADE_CUSTOM_OPTION = "✍️ 직접 입력"
MODE_OPTIONS = ["❓ 질문", "🔍 첨삭", "💮 개념PASS", "👤 마이"]

try:
    from supabase import create_client, Client  # noqa: F401 (Client는 타입 표기용)
except ImportError:
    create_client = None
    Client = None

try:
    import qrcode
except ImportError:
    qrcode = None

try:
    from streamlit_cropper import st_cropper
except ImportError:
    st_cropper = None


@st.cache_resource
def get_supabase_client():
    """Supabase 클라이언트를 프로세스당 한 번만 만들어 재사용한다 (st.cache_resource).

    SUPABASE_SERVICE_KEY는 RLS(행 단위 보안)를 우회하는 service_role 키다. 이 앱은
    Streamlit 서버에서만 실행되고 이 키가 학생의 브라우저로 전달될 일이 없으므로
    (카카오 로그인은 이 앱과 별개의 OAuth라 Supabase Auth/RLS 정책을 쓰지 않는다),
    서버 프로세스 안에서만 쓰는 이 키 하나로 충분하다. secrets.toml 또는 Streamlit
    Cloud의 Secrets에만 넣고 절대 코드/Git에 커밋하지 않는다.
    """
    if create_client is None:
        return None
    # 끝에 슬래시('/')가 붙은 URL을 PostgREST에 그대로 넘기면 모든 요청이
    # "Invalid path specified in request URL"(PGRST125)로 실패한다 - Supabase
    # 대시보드에서 URL을 복사할 때 슬래시가 같이 붙는 실수가 흔해서 여기서 안전하게
    # 제거한다.
    url = st.secrets.get("SUPABASE_URL", "").strip().rstrip("/")
    key = st.secrets.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        return None
    try:
        return create_client(url, key)
    except Exception:
        return None


supabase = get_supabase_client()


@st.cache_data(ttl=600)
def load_daily_pass_scope():
    """선생님이 학년별로 지정해 둔 '오늘의 개념 범위'를 daily_missions 테이블에서 불러온다.

    실제 Supabase 테이블은 (id, grade, concept_text, created_at)만 있고 날짜/유니크
    제약이 없는 단순 로그성 테이블이다 - 출제할 때마다 새 행이 쌓이므로, 오늘 KST 기준
    하루 범위의 행 중 학년별로 가장 최근(created_at 내림차순 첫 번째) 것만 골라 '오늘의
    미션'으로 삼는다. 없는 학년이나 Supabase 미설정 시에는 빈 문자열로 채운 기본값을
    돌려준다. 선생님이 '✍️ 직접 입력'으로 PASS_GRADE_LEVELS에 없는 학년명을 등록했을
    수도 있으므로, 그런 키도 걸러내지 않고 그대로 담아 돌려준다(학생 화면의 학년 버튼
    목록이 이 값으로 결정됨).

    수업이 끝나는 순간 학생 전원이 QR로 동시 접속하면서 DB를 반복 조회해 생기는
    병목을 막기 위해 st.cache_data(ttl=600)로 10분간 캐시한다 - 선생님이 새 미션을
    출제하면 upsert_daily_mission()이 이 캐시를 즉시 비워 다음 조회부터 바로 반영된다.
    """
    result = {grade: "" for grade in PASS_GRADE_LEVELS}
    if not supabase:
        return result
    try:
        day_start = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        res = (
            supabase.table("daily_missions")
            .select("grade, concept_text, created_at")
            .gte("created_at", day_start.isoformat())
            .lt("created_at", day_end.isoformat())
            .order("created_at", desc=True)
            .execute()
        )
        seen_grades = set()
        for row in (res.data or []):
            grade = row.get("grade")
            if grade and grade not in seen_grades:
                result[grade] = row.get("concept_text") or ""
                seen_grades.add(grade)
    except Exception:
        pass
    return result


def upsert_daily_mission(grade, mission_text):
    """선생님 대시보드의 단일 '🚀 오늘의 개념 PASS 출제' 카드에서 학년 1개의 미션을
    daily_missions에 저장한다.

    실제 테이블 컬럼은 grade/concept_text뿐이고(id/created_at는 테이블 기본값으로
    자동 채워짐) grade에 대한 유니크 제약이 없으므로 upsert가 아니라 매번 새 행을
    INSERT한다 - 같은 학년을 오늘 여러 번 출제하면 여러 행이 쌓이고, 그중 가장 최근
    것을 load_daily_pass_scope()가 '오늘의 미션'으로 골라 보여준다.
    """
    if not supabase:
        return False, "Supabase 연결 정보(SUPABASE_URL/SUPABASE_SERVICE_KEY)가 설정되지 않았습니다."
    try:
        supabase.table("daily_missions").insert(
            {"grade": grade, "concept_text": mission_text.strip()}
        ).execute()
        load_daily_pass_scope.clear()
        return True, ""
    except Exception as e:
        return False, str(e)


def generate_qr_png_bytes(url):
    """학생들이 폰 카메라로 찍으면 바로 접속되는 큰 QR코드 PNG를 만든다.

    qrcode 라이브러리가 아직 설치/배포되지 않은 환경(requirements.txt 반영 전)을
    대비해 qrcode가 없으면 None을 돌려주고, 호출하는 쪽에서 안내 문구로 대체한다.
    """
    if qrcode is None or not url:
        return None
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def fetch_pass_records_for_date(date_str, grade="전체"):
    """선생님 대시보드 '📊 개념 PASS 실시간 현황'용: 특정 날짜(KST 기준 하루)의
    pass_records를 users와 조인해서 최신순으로 가져온다. grade가 '전체'가 아니면
    그 학년(PASS 학년 = users.pass_grade)만 남긴다.

    '데이터 0건'과 '조회 실패'를 구분해야 하므로 (성공 여부, 행 목록, 에러 메시지)
    튜플로 돌려준다.

    users.pass_grade 컬럼이 라이브 환경에 아직 없는 경우(42703)를 대비해, 먼저
    pass_grade까지 포함한 조인을 시도하고 실패하면 nickname만 조인해서 한 번 더
    시도한다 - 그러면 목록 자체는 깨지지 않고 나오되, 학년 필터/학년 칼럼은
    컬럼이 생기기 전까지만 비어 보인다. users.student_grade는 '질문/첨삭' 탭의
    과목명(예: 공통수학1)이라 PASS 학년(중2, 고1(원종) 등)과 값 자체가 다르므로
    대체값으로 쓰지 않는다 - 그 컬럼은 이전 작업에서 이미 users 테이블에 없는
    것으로 확인되어 제거했다.
    """
    if not supabase:
        return False, [], "Supabase 연결 정보(SUPABASE_URL/SUPABASE_SERVICE_KEY)가 설정되지 않았습니다."
    try:
        day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=KST)
        day_end = day_start + timedelta(days=1)

        def _query(select_str):
            return (
                supabase.table("pass_records")
                .select(select_str)
                .gte("created_at", day_start.isoformat())
                .lt("created_at", day_end.isoformat())
                .order("created_at", desc=True)
                .execute()
            )

        try:
            res = _query("*, users(nickname, pass_grade)")
        except Exception:
            res = _query("*, users(nickname)")

        rows = res.data or []
        if grade and grade != "전체":
            rows = [r for r in rows if _embedded_user(r).get("pass_grade") == grade]
        return True, rows, ""
    except Exception as e:
        return False, [], str(e)


def get_kakao_login_url(state=None):
    """카카오 로그인 동의 화면으로 이동하는 인가 URL을 만든다.

    state에 현재 선택된 모드(selected_mode)를 실어 보내면, 카카오가 리다이렉트로
    그대로 돌려주는 state 파라미터를 통해 로그인 후에도 원래 보고 있던 모드로
    복귀할 수 있다 (세션 상태가 리다이렉트 왕복 중 끊겨도 안전하게 동작).
    """
    url = (
        "https://kauth.kakao.com/oauth/authorize"
        f"?client_id={KAKAO_CLIENT_ID}"
        f"&redirect_uri={quote(KAKAO_REDIRECT_URI, safe='')}"
        "&response_type=code"
    )
    if state:
        url += f"&state={quote(state, safe='')}"
    return url


def exchange_kakao_code(code):
    """인가 코드를 액세스 토큰으로 교환한 뒤, 카카오 닉네임과 고유 회원번호를 가져온다."""
    token_res = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": KAKAO_CLIENT_ID,
            "redirect_uri": KAKAO_REDIRECT_URI,
            "code": code,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        timeout=5,
    )
    token_res.raise_for_status()
    access_token = token_res.json()["access_token"]

    user_res = requests.get(
        "https://kapi.kakao.com/v2/user/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=5,
    )
    user_res.raise_for_status()
    user_json = user_res.json()

    kakao_id = str(user_json.get("id"))
    account = user_json.get("kakao_account", {}) or {}
    profile = account.get("profile", {}) or {}
    nickname = (
        profile.get("nickname")
        or (user_json.get("properties") or {}).get("nickname")
        or "익명 학생"
    )
    return kakao_id, nickname


def _serialize_messages(messages):
    """메시지 리스트(clinic/review 공용)를 JSON/JSONB로 저장 가능한 형태로 변환한다 (이미지는 base64)."""
    out = []
    for msg in messages:
        m = {"role": msg["role"], "content": msg["content"]}
        if msg.get("api_text"):
            m["api_text"] = msg["api_text"]
        if msg.get("images"):
            m["images"] = [
                {"data": base64.b64encode(img["bytes"]).decode("utf-8"), "mime": img.get("mime", "image/jpeg")}
                for img in msg["images"]
            ]
        out.append(m)
    return out


def _deserialize_messages(raw_messages):
    """Supabase JSONB에서 읽은 메시지 리스트를 다시 앱 형태(이미지는 bytes)로 복원한다."""
    out = []
    for msg in raw_messages or []:
        m = {"role": msg["role"], "content": msg["content"]}
        if msg.get("api_text"):
            m["api_text"] = msg["api_text"]
        if msg.get("images"):
            m["images"] = [
                {"bytes": base64.b64decode(img["data"]), "mime": img.get("mime", "image/jpeg")}
                for img in msg["images"]
            ]
        out.append(m)
    return out


def _iso_to_kst_label(iso_str, fmt="%Y-%m-%d %H:%M"):
    """Supabase가 돌려주는 UTC ISO 타임스탬프를, 앱 전체가 기대해 온 'KST 포맷 문자열'로 바꾼다.

    question_archive의 created_at / pass_records의 completed_at은 기존 코드 곳곳에서
    문자열 슬라이싱([:10] 등)으로 날짜를 뽑아 쓰므로, DB로 바꾼 뒤에도 이 포맷 계약을
    그대로 지켜야 나머지 코드(스트릭 계산, 날짜별 그룹핑 등)를 건드릴 필요가 없다.
    """
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).strftime(fmt)
    except Exception:
        return str(iso_str)[:16].replace("T", " ")


def load_student_data(kakao_id):
    """카카오 고유 회원번호로 Supabase에 저장된 학생의 과목/오답노트/PASS 기록을 불러온다."""
    if not supabase:
        return None
    try:
        user_res = supabase.table("users").select("*").eq("kakao_id", kakao_id).execute()
        user_rows = user_res.data or []
        if not user_rows:
            return None
        user_row = user_rows[0]

        chat_res = (
            supabase.table("chat_history")
            .select("*")
            .eq("kakao_id", kakao_id)
            .order("created_at", desc=False)
            .execute()
        )
        archive = [
            {
                "id": row["id"],
                "feature": row.get("feature", ""),
                "created_at": _iso_to_kst_label(row.get("created_at")),
                "messages": _deserialize_messages(row.get("messages")),
            }
            for row in (chat_res.data or [])
        ]

        pass_res = (
            supabase.table("pass_records")
            .select("*")
            .eq("kakao_id", kakao_id)
            .order("created_at", desc=False)
            .execute()
        )
        pass_records = [
            {
                "topic": row.get("topic", ""),
                "concept_summary": row.get("concept_summary") or row.get("topic", ""),
                "completed_at": _iso_to_kst_label(row.get("created_at")),
            }
            for row in (pass_res.data or [])
        ]

        return {
            "kakao_id": kakao_id,
            "nickname": user_row.get("nickname", ""),
            # 실제 users 테이블에 student_grade 컬럼이 없어 늘 None - select("*")라
            # 에러는 안 나지만, persist_current_student()가 더는 이 값을 쓰지 않으므로
            # 다음 로그인 때 과목을 다시 선택해야 한다(의도된 동작).
            "student_grade": user_row.get("student_grade"),
            "pass_grade": user_row.get("pass_grade", ""),
            "question_archive": archive,
            "pass_records": pass_records,
            "updated_at": user_row.get("updated_at", ""),
        }
    except Exception:
        return None


def persist_current_student():
    """로그인한 학생의 users 행(닉네임/PASS용 학년)을 upsert한다.

    대화 기록(chat_history)과 PASS 기록(pass_records)은 발생하는 그 순간
    upsert_question_archive_entry() / insert_pass_record()가 각각 자기 테이블에
    바로 써 넣으므로, 여기서는 users 한 줄만 최신 상태로 맞추면 된다.

    student_grade('질문/첨삭' 과목)는 넣지 않는다 - 실제 Supabase users 테이블에 그
    컬럼이 없어서(42703) 포함시키면 이 upsert 전체가 매번 조용히 실패했다. pass_grade도
    같은 문제가 확인됐다 - 컬럼이 없는 환경에서는 pass_grade를 포함한 upsert 전체가
    매번 실패해 nickname/updated_at조차 저장되지 않는다. 그래서 먼저 pass_grade까지
    포함해서 시도하고, 그 한 번이 실패하면 pass_grade를 뺀 payload로 즉시 재시도한다 -
    컬럼이 없어도 최소한 nickname/updated_at은 저장되고, 컬럼이 생기면(아래 ALTER TABLE)
    코드를 안 고쳐도 pass_grade도 다시 저장되기 시작한다.
    """
    kakao_id = st.session_state.get("kakao_id")
    if not (supabase and kakao_id):
        return
    payload = {
        "kakao_id": kakao_id,
        "nickname": st.session_state.student_name,
        "pass_grade": st.session_state.get("pass_grade", ""),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("users").upsert(payload).execute()
    except Exception:
        try:
            payload.pop("pass_grade", None)
            supabase.table("users").upsert(payload).execute()
        except Exception:
            pass


def upsert_question_archive_entry(state_prefix, feature_label, messages_key):
    """현재 살아있는 대화(messages_key)를 question_archive 카드 + chat_history 행에 생성/갱신한다.

    사진을 새로 올리면 새 카드(= 새 chat_history 행)를 만들고, 그 뒤 같은 문제로
    이어지는 후속 대화(힌트 요청 등)는 같은 행을 계속 UPDATE한다 - 카드 1개 = 질문 1건
    = chat_history 행 1개. 세션의 question_archive 리스트도 함께 갱신해서, 이번
    DB 쓰기가 끝나기 전에도(또는 실패하더라도) 화면에는 바로 반영되게 한다.
    """
    entry_id_key = f"{state_prefix}_archive_entry_id"
    entry_id = st.session_state.get(entry_id_key)
    messages_snapshot = list(st.session_state[messages_key])
    kakao_id = st.session_state.get("kakao_id")

    if entry_id:
        for entry in st.session_state.question_archive:
            if entry["id"] == entry_id:
                entry["messages"] = messages_snapshot
                break
        if supabase and kakao_id:
            try:
                supabase.table("chat_history").update({
                    "messages": _serialize_messages(messages_snapshot),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", entry_id).execute()
            except Exception:
                pass
        return

    new_id = str(uuid.uuid4())
    st.session_state[entry_id_key] = new_id
    st.session_state.question_archive.append({
        "id": new_id,
        "feature": feature_label,
        "created_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "messages": messages_snapshot,
    })
    if supabase and kakao_id:
        try:
            supabase.table("chat_history").insert({
                "id": new_id,
                "kakao_id": kakao_id,
                "feature": feature_label,
                "messages": _serialize_messages(messages_snapshot),
            }).execute()
        except Exception:
            pass


def insert_pass_record(topic, concept_summary):
    """PASS 통과 1건을 pass_records 테이블에 즉시 저장한다.

    pass_records 테이블에는 completed_at 컬럼이 없다(42703) - 통과 시각은
    Supabase 기본값으로 자동 채워지는 created_at을 그대로 쓴다.
    """
    kakao_id = st.session_state.get("kakao_id")
    if not (supabase and kakao_id):
        return
    try:
        supabase.table("pass_records").insert({
            "kakao_id": kakao_id,
            "topic": topic,
            "concept_summary": concept_summary,
        }).execute()
    except Exception:
        pass


OCR_CORRECTION_BUCKET = "ocr-corrections"


def log_ocr_correction(original_text, edited_text, image_bytes, kakao_id):
    """학생이 'Quick-Edit' 화면에서 OCR 판독 결과를 실제로 고쳤을 때만 호출한다 -
    original_ocr_text/user_edited_text를 나란히 ocr_corrections 테이블에 남겨서
    추후 자체 수식 OCR 모델을 학습시킬 데이터셋으로 쓴다.

    이미지는 Supabase Storage 버킷(OCR_CORRECTION_BUCKET)에 올려 공개 URL을 같이
    저장한다 - 버킷이 아직 없으면 한 번 만들어보고, 그래도 업로드가 안 되면
    image_url을 빈 문자열로 둔 채 텍스트 비교만이라도 남긴다. ocr_corrections
    테이블 자체가 아직 없는 환경(42P01)이어도 이 로깅 실패가 첨삭 기능을 막으면
    안 되므로 예외를 전부 조용히 삼킨다.
    """
    if not (supabase and kakao_id):
        return
    image_url = ""
    try:
        try:
            supabase.storage.create_bucket(OCR_CORRECTION_BUCKET, options={"public": True})
        except Exception:
            pass  # 이미 존재하거나 권한이 없으면 업로드 시도에서 자연히 드러난다
        path = f"{kakao_id}/{uuid.uuid4()}.jpg"
        supabase.storage.from_(OCR_CORRECTION_BUCKET).upload(
            path, image_bytes, file_options={"content-type": "image/jpeg"}
        )
        image_url = supabase.storage.from_(OCR_CORRECTION_BUCKET).get_public_url(path)
    except Exception:
        image_url = ""

    try:
        supabase.table("ocr_corrections").insert({
            "kakao_id": kakao_id,
            "original_ocr_text": original_text,
            "user_edited_text": edited_text,
            "image_url": image_url,
        }).execute()
    except Exception:
        pass


STUDENT_UPLOADS_BUCKET = "student_uploads"


def log_student_question(problem_code, image_bytes, ocr_text, ai_feedback):
    """학생과의 질문/첨삭 왕복이 끝날 때마다 student_questions에 1건을 남긴다 - '🔍 오답
    첨삭'의 Quick-Edit 확정/1장짜리 단일 호출뿐 아니라, '❓ 질문 클리닉'의 텍스트
    채팅 답변(이미지 없음, problem_code="일반질문")도 같은 파이프라인을 그대로 쓴다.
    선생님 대시보드가 문제번호/질문 내용/AI 피드백을 구조화된 형태로 바로 훑어볼 수
    있게 chat_history(대화 전체를 messages jsonb로 담는 범용 로그)와는 별도로 두는
    전용 로그다. image_bytes가 없으면(텍스트만 있는 질문) Storage 업로드를 건너뛴다.

    st.toast처럼 rerun 타이밍에 사라질 수 있는 위젯에 의존하지 않는다 - 단계별 진행
    상황을 st.session_state["last_save_status"]에 전부 적어 두고, 호출부가 rerun을
    하더라도 render_last_save_status_debug_card()가 다음 스크립트 실행에서 그 기록을
    그대로 다시 그린다. Supabase 클라이언트가 없으면 조용히 return하지 않고 그
    자리에서 바로 st.error()를 띄운다.

    돌려주는 값: (성공 여부, 에러 메시지) - 호출부가 추가로 분기하고 싶을 때를 위해
    남겨 두지만, 화면 표시는 이 함수가 전부 책임진다.
    """
    steps = []

    def _step(ok, text):
        steps.append({"ok": ok, "text": text})

    if not supabase:
        error_msg = "SUPABASE_URL/SUPABASE_SERVICE_KEY가 설정되지 않았거나 클라이언트 생성에 실패했습니다."
        _step(False, "Supabase 클라이언트 연결 실패")
        st.session_state["last_save_status"] = {"ok": False, "steps": steps, "error": error_msg, "row_id": None}
        st.error("🚨 Supabase 클라이언트 연결 실패! Secrets에 SUPABASE_URL과 키가 올바른지 확인하세요.")
        return False, error_msg

    _step(True, "Supabase 클라이언트 확인 완료")
    kakao_id = st.session_state.get("kakao_id")

    image_url = ""
    if not image_bytes:
        # '❓ 질문 클리닉'의 텍스트 전용 대화처럼 사진이 없는 기록도 있다 - Storage
        # 업로드를 시도할 대상이 없으므로 조용히 건너뛴다(에러 아님).
        _step(True, "이미지 없음 - Storage 업로드 생략")
    else:
        try:
            try:
                supabase.storage.create_bucket(STUDENT_UPLOADS_BUCKET, options={"public": True})
            except Exception:
                pass  # 이미 존재하거나 권한이 없으면 업로드 시도에서 자연히 드러난다
            path = f"{kakao_id or 'anonymous'}/{uuid.uuid4()}.jpg"
            supabase.storage.from_(STUDENT_UPLOADS_BUCKET).upload(
                path, image_bytes, file_options={"content-type": "image/jpeg"}
            )
            image_url = supabase.storage.from_(STUDENT_UPLOADS_BUCKET).get_public_url(path)
            _step(True, f"풀이 사진 Storage 업로드 성공 ({path})")
        except Exception as e:
            image_url = ""  # 이미지 업로드는 부가 기능 - 실패해도 텍스트 기록은 계속 남긴다
            _step(False, f"풀이 사진 Storage 업로드 실패(텍스트 기록은 계속 진행): {e}")

    try:
        insert_res = supabase.table("student_questions").insert({
            "kakao_id": kakao_id,
            "student_name": st.session_state.get("student_name") or "",
            "problem_code": problem_code or "",
            "image_url": image_url,
            "ocr_text": ocr_text or "",
            "ai_feedback": ai_feedback or "",
        }).execute()
        row_id = None
        try:
            row_id = (insert_res.data or [{}])[0].get("id")
        except Exception:
            row_id = None
        _step(True, f"student_questions Insert 성공 (Row ID: {row_id})")
        st.session_state["last_save_status"] = {"ok": True, "steps": steps, "error": "", "row_id": row_id}
        return True, ""
    except Exception as e:
        _step(False, f"student_questions Insert 실패: {e}")
        st.session_state["last_save_status"] = {"ok": False, "steps": steps, "error": str(e), "row_id": None}
        return False, str(e)


def render_last_save_status_debug_card():
    """log_student_question()이 st.session_state['last_save_status']에 남긴 단계별
    진행 기록을 st.status 박스로 보여준다.

    st.status/st.toast 같은 위젯 자체는 st.rerun() 한 번으로도 사라질 수 있어서,
    위젯을 그대로 들고 있지 않고 세션 상태에 저장된 데이터를 매 스크립트 실행마다
    새로 그린다 - 그래서 Quick-Edit 확정 버튼이 st.rerun()을 호출한 '다음' 실행에서도
    같은 내용이 그대로 남아 있다.
    """
    status_data = st.session_state.get("last_save_status")
    if not status_data:
        return
    final_ok = status_data.get("ok")
    label = "📡 데이터베이스 기록 처리 완료" if final_ok else "📡 데이터베이스 기록 처리 실패"
    with st.status(label, expanded=not final_ok, state=("complete" if final_ok else "error")):
        for step in status_data.get("steps", []):
            st.write(("✅ " if step["ok"] else "❌ ") + step["text"])
        if final_ok:
            st.success(f"✅ DB 저장 성공! (Row ID: {status_data.get('row_id')})")
        else:
            st.error(f"❌ Insert 실패 상세 원인: {status_data.get('error')}")


def _embedded_user(row):
    """PostgREST 조인(embed)으로 함께 온 users 중첩 객체를 꺼낸다.

    FK 관계상 보통 dict 하나로 오지만, 혹시 리스트로 오는 경우까지 방어적으로 처리한다.
    """
    nested = row.get("users")
    if isinstance(nested, list):
        return (nested[0] if nested else {}) or {}
    return nested or {}


def fetch_student_questions():
    """선생님 대시보드용: student_questions(손글씨 풀이 첨삭 전용 구조화 로그)를
    최신순으로 불러온다.

    '데이터 0건'과 '조회 자체가 실패함'을 구분해야 하므로(테이블이 아직 없는 환경
    42P01 포함) (성공 여부, 행 목록, 에러 메시지) 튜플로 돌려준다 - 실패를 조용히
    빈 리스트로 뭉개면 실제 오류가 나도 '기록이 0건'인 것처럼 보인다.
    """
    if not supabase:
        return False, [], "Supabase 연결 정보(SUPABASE_URL/SUPABASE_SERVICE_KEY)가 설정되지 않았습니다."
    try:
        res = (
            supabase.table("student_questions")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
        return True, (res.data or []), ""
    except Exception as e:
        return False, [], str(e)


def render_student_questions_dashboard_section():
    """선생님 대시보드의 메인 섹션 - student_questions(🔍 오답 첨삭 + ❓ 질문 클리닉
    통합 로그)를 최신순으로 보여준다.

    총 건수/오답 첨삭/일반 질문 통계를 표 위에 st.metric 뱃지로 먼저 띄워서, 대시보드에
    들어오는 순간 바로 한눈에 들어오게 한다(_is_general_question/_format_student_question_labels
    로 두 종류를 구분).
    """
    sq_ok, sq_rows, sq_err = fetch_student_questions()
    if not sq_ok:
        show_friendly_error(sq_err, "😥 학생 질문/첨삭 기록을 불러오는 중 문제가 생겼어요.")
        return
    if not sq_rows:
        st.info("아직 등록된 질문/첨삭 기록이 없습니다.")
        return

    general_count = sum(1 for row in sq_rows if _is_general_question(row))
    review_count = len(sq_rows) - general_count

    metric_c1, metric_c2, metric_c3 = st.columns(3)
    metric_c1.metric("📊 총 기록", f"{len(sq_rows)}건")
    metric_c2.metric("🔍 오답 첨삭", f"{review_count}건")
    metric_c3.metric("💬 일반 질문", f"{general_count}건")

    st.markdown("#### 📝 학생 질문/첨삭 기록 (최신순)")
    table_rows = []
    for row in sq_rows:
        feature_label, problem_label = _format_student_question_labels(row)
        table_rows.append({
            "구분": feature_label,
            "학생": row.get("student_name") or "익명 학생",
            "문제번호": problem_label,
            "시각": _iso_to_kst_label(row.get("created_at")),
            "AI 피드백": (
                (row.get("ai_feedback") or "")[:50] + "…"
                if len(row.get("ai_feedback") or "") > 50 else (row.get("ai_feedback") or "-")
            ),
        })
    st.dataframe(table_rows, use_container_width=True, hide_index=True)

    st.markdown("##### 🔍 문항별 상세 보기 (사진·질문 내용·AI 답변)")
    for row in sq_rows:
        student = row.get("student_name") or "익명 학생"
        feature_label, problem_label = _format_student_question_labels(row)
        created_label = _iso_to_kst_label(row.get("created_at"))
        with st.expander(f"👤 {student} · {feature_label} · {problem_label} · {created_label}"):
            if row.get("image_url"):
                st.image(row["image_url"], width=280)
            st.markdown("**🙋 질문 내용**" if _is_general_question(row) else "**📝 OCR 텍스트**")
            st.code(row.get("ocr_text") or "-")
            st.markdown("**🤖 AI 답변**" if _is_general_question(row) else "**🤖 AI 피드백**")
            st.markdown(row.get("ai_feedback") or "-")


SOLUTION_CONVERTER_SYSTEM_PROMPT = (
    "너는 수학 해설지를 정제하는 전문가다. 원장님이 붙여넣은 해설지 텍스트를 정확히 4단계의 "
    "'정석 사고 단계'로 구조화하고, 학생들이 이 문제에서 자주 틀리는 함정(pitfalls)을 뽑아내라.\n"
    "1단계: 핵심 성질 파악 (문제에서 주어진 조건으로부터 바로 도출되는 성질/정의)\n"
    "2단계: 관계식 수립 (1단계를 바탕으로 세우는 방정식/부등식)\n"
    "3단계: 식 전개/변형 (전개, 인수분해, 이항 등 대수적 변형 과정)\n"
    "4단계: 계산 도출 (동류항 정리 등 최종 계산과 답)\n\n"
    "반드시 아래 JSON 형식으로만 출력하라 (다른 설명, 머리말, 코드블록 표시 없이 JSON 그 자체만):\n"
    "{\n"
    '  "reference_steps": [\n'
    '    {"step": 1, "title": "핵심 성질 파악", "content": "..."},\n'
    '    {"step": 2, "title": "관계식 수립", "content": "..."},\n'
    '    {"step": 3, "title": "식 전개/변형", "content": "..."},\n'
    '    {"step": 4, "title": "계산 도출", "content": "..."}\n'
    "  ],\n"
    '  "pitfalls": ["...", "..."]\n'
    "}"
)


def convert_solution_to_reference_steps(raw_solution_text):
    """원장님이 '📚 해설 DB 관리'에 붙여넣은 해설지 텍스트를 4단계 정석 사고 단계 + 자주 틀리는
    함정(JSON)으로 정제한다.

    모델이 ```json 코드블록으로 감싸서 답하는 경우까지 방어적으로 벗겨내고 파싱한다. 파싱
    실패 시 예외를 그대로 올려서, 호출하는 쪽(관리자 페이지)이 에러 메시지를 보여주고
    원장님이 다시 시도하게 한다 - 이 단계는 사람이 검토/수정하는 초안 생성이라, 조용히
    기본값으로 넘어가면 오히려 잘못된 해설이 그대로 저장될 위험이 있다.
    """
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL_NAME,
        max_tokens=1500,
        system=SOLUTION_CONVERTER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"다음 해설지를 정제해줘:\n\n{raw_solution_text}"}],
    )
    raw = get_response_text(response).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(cleaned)


def _render_solution_db_single_tab():
    """'✍️ 단건 수동 등록' 탭 - 문제 1개씩 해설지 텍스트를 붙여넣어 등록한다."""
    info_c1, info_c2, info_c3 = st.columns(3)
    with info_c1:
        sdb_book_title = st.text_input("교재명", value="쎈 중3-2", key="sdb_book_title")
    with info_c2:
        sdb_problem_code = st.text_input("문제 번호", value="0353", key="sdb_problem_code")
    with info_c3:
        sdb_chapter_name = st.text_input("단원명", placeholder="예: 원과 직선", key="sdb_chapter_name")

    raw_solution_text = st.text_area(
        "해설지 텍스트 붙여넣기",
        key="sdb_raw_solution_text",
        height=140,
        placeholder="해설지에서 복사한 텍스트를 그대로 붙여넣으세요.",
    )

    if st.button(
        "✨ 정석 사고 단계로 자동 변환", key="sdb_convert_btn", disabled=not raw_solution_text.strip()
    ):
        if not api_key:
            st.error("API 키가 설정되지 않았습니다.")
        else:
            with st.spinner("해설지를 정석 사고 단계로 정제하는 중입니다..."):
                try:
                    converted = convert_solution_to_reference_steps(raw_solution_text)
                    st.session_state["_sdb_steps_pending"] = json.dumps(
                        converted.get("reference_steps", []), ensure_ascii=False, indent=2
                    )
                    st.session_state["_sdb_pitfalls_pending"] = json.dumps(
                        converted.get("pitfalls", []), ensure_ascii=False, indent=2
                    )
                    st.success("✅ 변환 완료! 아래 내용을 검토하고 필요하면 직접 수정해 주세요.")
                    st.rerun()
                except Exception as e:
                    show_friendly_error(e, "😥 자동 변환 중 문제가 생겼어요. 원문을 다듬어 다시 시도해 주세요.")

    # mission_text_input과 같은 이유로, 위젯이 이미 생성된 뒤에는 그 키의 session_state를
    # 직접 바꿀 수 없다 - _sdb_*_pending에 적어두고 rerun하면 위젯 생성 '전'인 여기서 반영된다.
    if "_sdb_steps_pending" in st.session_state:
        st.session_state["sdb_steps_text"] = st.session_state.pop("_sdb_steps_pending")
    if "_sdb_pitfalls_pending" in st.session_state:
        st.session_state["sdb_pitfalls_text"] = st.session_state.pop("_sdb_pitfalls_pending")

    steps_text = st.text_area(
        "정석 사고 단계 (JSON, 검토/수정 가능)", key="sdb_steps_text", height=220,
        placeholder='[{"step": 1, "title": "핵심 성질 파악", "content": "..."}, ...]',
    )
    pitfalls_text = st.text_area(
        "자주 틀리는 함정 (JSON, 검토/수정 가능)", key="sdb_pitfalls_text", height=120,
        placeholder='["...", "..."]',
    )

    if st.button("💾 DB에 정석 해설 등록", key="sdb_save_btn", type="primary", use_container_width=True):
        if not sdb_book_title.strip() or not sdb_problem_code.strip():
            st.warning("교재명과 문제 번호를 입력해 주세요.")
        else:
            try:
                reference_steps = json.loads(steps_text) if steps_text.strip() else []
                pitfalls = json.loads(pitfalls_text) if pitfalls_text.strip() else []
            except Exception as e:
                st.error(f"😥 JSON 형식이 올바르지 않아요: {e}")
            else:
                save_ok, save_err = save_problem_solution(
                    sdb_book_title, sdb_problem_code, sdb_chapter_name, reference_steps, pitfalls
                )
                if save_ok:
                    st.success(f"✅ {sdb_book_title} - {sdb_problem_code} 정석 해설을 등록했어요!")
                    st.rerun()
                else:
                    st.error(f"😥 저장 중 문제가 생겼어요.\n\n상세 에러: {save_err}")


def _render_solution_db_bulk_tab():
    """'📄 PDF 대량 등록 (Bulk)' 탭 - 해설지 PDF 한 권을 통째로 올려 문항 단위로 쪼개서
    한꺼번에 등록한다."""
    st.caption("해설지 PDF를 통째로 올리면 문항별로 쪼개서 한꺼번에 DB에 등록할 수 있어요.")

    bulk_c1, bulk_c2 = st.columns(2)
    with bulk_c1:
        bulk_book_title = st.text_input("교재명", value="쎈 중3-2", key="sdb_bulk_book_title")
    with bulk_c2:
        bulk_chapter_name = st.text_input("단원명", placeholder="예: 원과 직선", key="sdb_bulk_chapter_name")

    if not is_pdf_parser_available() and not is_pdf_image_available():
        st.warning(
            "PDF 파싱 라이브러리(PyPDF2/pypdfium2)가 아직 이 환경에 설치되지 않았어요. "
            "선생님에게 문의해 주세요."
        )
        return

    uploaded_pdf = st.file_uploader("해설지 PDF 업로드", type=["pdf"], key="sdb_bulk_pdf")

    if st.button(
        "🚀 전체 문항 자동 추출 및 등록",
        key="sdb_bulk_run_btn",
        type="primary",
        use_container_width=True,
        disabled=not uploaded_pdf,
    ):
        if not api_key:
            st.error("API 키가 설정되지 않았습니다.")
        elif not bulk_book_title.strip():
            st.warning("교재명을 입력해 주세요.")
        else:
            pdf_bytes = uploaded_pdf.getvalue()

            # 1단계: 텍스트 추출을 먼저 시도해 보고, 페이지당 글자수가 너무 적으면
            # (스캔본/이미지 PDF로 판단) 바로 Vision 모드로 전환한다.
            pdf_text = ""
            if is_pdf_parser_available():
                try:
                    pdf_text = extract_pdf_text(pdf_bytes)
                except Exception:
                    pdf_text = ""
            is_scanned = is_scanned_pdf_text(pdf_text)

            progress_bar = st.progress(0.0)

            def _on_progress(done, total):
                if total:
                    progress_bar.progress(done / total)

            problems = None
            try:
                if is_scanned:
                    st.info("ℹ️ 스캔본(이미지) PDF가 감지되어 Vision AI 모드로 자동 전환하여 문항을 분석합니다...")
                    if not is_pdf_image_available():
                        st.error(
                            "스캔본(이미지) PDF로 보이는데 Vision 변환 라이브러리(pypdfium2)가 "
                            "설치되지 않아 처리할 수 없어요. 선생님에게 문의해 주세요."
                        )
                    else:
                        st.caption("스캔본 모드는 처리 시간/비용 때문에 앞쪽 5페이지까지만 분석해요.")
                        with st.spinner(
                            "PDF를 이미지로 변환하고 Vision AI로 문항을 분석하는 중입니다. "
                            "(시간이 다소 소요됩니다)..."
                        ):
                            problems = parse_scanned_solutions(
                                pdf_bytes, MODEL_NAME, api_key, progress_callback=_on_progress,
                            )
                else:
                    with st.spinner("PDF를 분석하여 문항별로 쪼개고 있습니다. (시간이 다소 소요됩니다)..."):
                        problems = parse_bulk_solutions(
                            pdf_text, MODEL_NAME, api_key, progress_callback=_on_progress,
                        )
            except Exception as e:
                show_friendly_error(e, "😥 PDF 분석 중 문제가 생겼어요.")
            progress_bar.empty()

            if problems is not None:
                if not problems:
                    st.warning(
                        "문항을 하나도 추출하지 못했어요. 문항 번호를 알아보기 어려운 화질이거나 "
                        "해설 서식이 특이한 PDF일 수 있어요."
                    )
                else:
                    st.success(f"✅ {len(problems)}개 문항을 추출했어요! 아래 목록을 확인하고 등록해 주세요.")
                    st.session_state["_sdb_bulk_problems"] = problems
                    st.session_state["_sdb_bulk_book_title"] = bulk_book_title
                    st.session_state["_sdb_bulk_chapter_name"] = bulk_chapter_name

    pending_problems = st.session_state.get("_sdb_bulk_problems")
    if pending_problems:
        st.markdown(f"##### 🔍 추출된 {len(pending_problems)}개 문항")
        st.dataframe(
            [
                {
                    "문제번호": p.get("problem_code", "-"),
                    "단계 수": len(p.get("reference_steps") or []),
                    "함정 수": len(p.get("pitfalls") or []),
                }
                for p in pending_problems
            ],
            use_container_width=True,
            hide_index=True,
        )
        with st.expander("JSON 원본 보기"):
            st.code(json.dumps(pending_problems, ensure_ascii=False, indent=2), language="json")

        if st.button(
            f"💾 {len(pending_problems)}개 문항 전체 DB에 등록",
            key="sdb_bulk_save_btn",
            type="primary",
            use_container_width=True,
        ):
            saved_book_title = st.session_state.get("_sdb_bulk_book_title", bulk_book_title)
            saved_chapter_name = st.session_state.get("_sdb_bulk_chapter_name", bulk_chapter_name)
            success_count = 0
            failed = []
            with st.spinner(f"{len(pending_problems)}개 문항을 등록하는 중입니다..."):
                for problem in pending_problems:
                    code = str(problem.get("problem_code", "")).strip()
                    if not code:
                        failed.append(("(번호 없음)", "problem_code가 비어 있어 건너뜀"))
                        continue
                    save_ok, save_err = save_problem_solution(
                        saved_book_title, code, saved_chapter_name,
                        problem.get("reference_steps", []), problem.get("pitfalls", []),
                    )
                    if save_ok:
                        success_count += 1
                    else:
                        failed.append((code, save_err))

            if failed:
                st.warning(f"⚠️ {success_count}개 등록 완료, {len(failed)}개는 실패/건너뜀.")
                with st.expander("실패/건너뜀 목록 보기"):
                    for code, err in failed:
                        st.caption(f"- {code}: {err}")
            else:
                st.success(f"✅ {success_count}개 문항을 모두 등록했어요!")
            st.session_state.pop("_sdb_bulk_problems", None)
            st.rerun()


def render_solution_db_admin_section():
    """원장님용 '📚 해설 DB 관리' - 교과과정 표준 해설을 등록/수정하고 현황을 확인한다.

    등록된 해설은 손글씨 풀이 첨삭의 2단계 검증에서 '바이블'로 쓰인다
    (app.py의 get_problem_solution()/format_reference_solution() 참고).
    """
    st.markdown("#### 📚 해설 DB 관리")
    st.caption(
        "교과과정 표준 해설을 등록해두면, 손글씨 첨삭에서 학생 풀이를 이 해설과 "
        "대조해 더 정밀하게 채점해요. 등록 안 된 문제는 기존 방식으로 자동 전환돼요."
    )

    tab_single, tab_bulk = st.tabs(["✍️ 단건 수동 등록", "📄 PDF 대량 등록 (Bulk)"])
    with tab_single:
        _render_solution_db_single_tab()
    with tab_bulk:
        _render_solution_db_bulk_tab()

    st.markdown("##### 📋 등록 현황")
    registered = list_registered_problems()
    if not registered:
        st.info("아직 등록된 문제가 없어요.")
    else:
        st.dataframe(
            [
                {
                    "교재명": row.get("book_title", "-"),
                    "문제번호": row.get("problem_code", "-"),
                    "단원명": row.get("chapter_name", "-"),
                    "단계 수": len(row.get("reference_steps") or []),
                    "함정 수": len(row.get("pitfalls") or []),
                    "등록/수정": _iso_to_kst_label(row.get("updated_at") or row.get("created_at")),
                }
                for row in registered
            ],
            use_container_width=True,
            hide_index=True,
        )


def render_teacher_dashboard():
    """학생들에게는 보이지 않는, 비밀번호로만 들어오는 선생님 전용 대시보드.

    학생 화면은 480px 폰 폭으로 고정하지만, 이 관리자 화면은 표/여러 칼럼 폼을
    다루는 데스크톱 도구라 그 제약이 오히려 방해가 된다. inject_custom_css()보다
    늦게(이 함수가 호출되는 시점에) 적용되는 스타일이라 동일 우선순위(!important)
    규칙 중 나중 것이 이긴다는 CSS 캐스케이드 규칙으로 폭을 다시 넓힌다.
    """
    st.markdown(
        "<style>.main .block-container { max-width: 1100px !important; }</style>",
        unsafe_allow_html=True,
    )

    # ==========================================
    # 🛠️ DB 통신 상태 점검 - Supabase 연결/쓰기 정상 동작이 이미 확인됐으므로 평소에는
    # 접어 두고, 나중에 다시 의심스러울 때만 펼쳐서 바로 재확인할 수 있게 둔다.
    # ==========================================
    with st.expander("🛠️ DB 통신 상태 점검 (문제 있을 때만 펼쳐서 확인)"):
        if st.button(
            "🚨 DB 강제 연결 및 쓰기 테스트 (student_questions)",
            type="primary",
            key="teacher_db_force_test_btn",
        ):
            if not supabase:
                st.error("🚨 Supabase Client 객체가 None입니다. Secrets 설정을 확인하세요.")
            else:
                try:
                    res = (
                        supabase.table("student_questions")
                        .insert({
                            "problem_code": "TEST-000",
                            "ai_feedback": "선생님 모드 직접 테스트",
                            "ocr_text": "연결 확인",
                        })
                        .execute()
                    )
                    st.success(f"✅ DB 쓰기 성공! 반환 데이터: {res.data}")
                except Exception as e:
                    st.error(f"❌ DB 통신 실패 에러 원문:\n{repr(e)}")

    st.markdown("---")

    top_c1, top_c2 = st.columns([4, 1])
    with top_c1:
        st.subheader("👨‍🏫 선생님 대시보드")
        st.caption("학생별 질문 클리닉 대화 내역(사진 포함)과 PASS 기록을 한눈에 확인하세요.")
    with top_c2:
        if st.button("🔒 종료", use_container_width=True, key="teacher_dashboard_exit"):
            st.session_state.teacher_mode = False
            st.session_state.pop("teacher_pw_input", None)
            st.rerun()

    st.markdown("---")

    render_student_questions_dashboard_section()

    st.markdown("---")

    st.markdown("#### 🚀 오늘의 개념 PASS 출제")
    st.caption("학년을 고르고 미션 내용을 입력한 뒤 출제하면, 학생들이 찍고 들어올 QR코드가 바로 아래에 뜹니다.")

    mission_grade_select = st.selectbox(
        "① 대상 학년",
        PASS_GRADE_LEVELS + [PASS_GRADE_CUSTOM_OPTION],
        key="mission_grade_select",
    )
    mission_grade_custom = ""
    if mission_grade_select == PASS_GRADE_CUSTOM_OPTION:
        mission_grade_custom = st.text_input(
            "학년 직접 입력", placeholder="예: 고1(신규반)", key="mission_grade_custom"
        )

    # mission_text_input 위젯이 생성된 '이후'(아래 사진 추출/출제 처리 코드)에는 그 키의
    # session_state를 직접 바꿀 수 없다(Streamlit 제약) - 대신 _mission_text_pending에
    # 적어두고 st.rerun()하면, 다음 실행에서 위젯이 생성되기 '전'인 바로 이 지점에서
    # 안전하게 반영된다.
    if "_mission_text_pending" in st.session_state:
        st.session_state["mission_text_input"] = st.session_state.pop("_mission_text_pending")

    mission_text = st.text_area(
        "② 미션 내용",
        key="mission_text_input",
        height=100,
        placeholder="예: 근의 공식 증명하기",
    )

    with st.expander("📸 ③ 칠판 판서 사진으로 채우기 (선택)"):
        st.caption("칠판 사진을 올리고 'AI 추출'을 누르면 위 ② 미션 내용 칸이 자동으로 채워져요. 저장 전에 한 번 확인해 주세요!")
        board_photo = st.file_uploader(
            "오늘 칠판 판서 사진 업로드", type=["jpg", "jpeg", "png"], key="mission_board_photo"
        )
        if st.button("🔍 AI로 미션 내용 추출하기", key="mission_extract_btn", disabled=not board_photo):
            if not api_key:
                st.error("API 키가 설정되지 않았습니다.")
            else:
                client = anthropic.Anthropic(api_key=api_key)
                photo_bytes = board_photo.getvalue()
                with st.spinner("칠판 판서를 분석하는 중..."):
                    try:
                        response = client.messages.create(
                            model=MODEL_NAME,
                            max_tokens=400,
                            messages=[{
                                "role": "user",
                                "content": [
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": board_photo.type,
                                            "data": base64.b64encode(photo_bytes).decode("utf-8"),
                                        },
                                    },
                                    {
                                        "type": "text",
                                        "text": (
                                            "이 칠판 판서 사진 속 핵심 수학 개념·공식·증명 포인트를 학생들의 "
                                            "'오늘의 개념 PASS' 구술시험 범위로 쓸 수 있게 1~2문장으로 간결하게 "
                                            "요약해라. 다른 설명 없이 요약 내용만 출력해라."
                                        ),
                                    },
                                ],
                            }],
                        )
                        extracted = get_response_text(response)
                        st.session_state["_mission_text_pending"] = extracted
                        st.success("✅ 칠판 판서에서 핵심 개념을 추출했어요! 위 ② 미션 내용을 확인하고 출제해 주세요.")
                        st.rerun()
                    except Exception as e:
                        show_friendly_error(e, "😥 칠판 판서 분석 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.")

    if st.button("🚀 ④ 출제 및 QR코드 생성", key="mission_submit_btn", use_container_width=True, type="primary"):
        mission_grade = (
            mission_grade_custom.strip()
            if mission_grade_select == PASS_GRADE_CUSTOM_OPTION
            else mission_grade_select
        )
        if mission_grade_select == PASS_GRADE_CUSTOM_OPTION and not mission_grade_custom.strip():
            st.warning("직접 입력을 선택했다면 학년명을 입력해 주세요.")
        elif not mission_text.strip():
            st.warning("미션 내용을 입력해 주세요.")
        else:
            mission_ok, mission_err = upsert_daily_mission(mission_grade, mission_text)
            if mission_ok:
                st.session_state["quick_mission_just_set"] = {
                    "grade": mission_grade,
                    "mission": mission_text.strip(),
                }
                st.session_state["_mission_text_pending"] = ""
                st.success(f"✅ {mission_grade} 미션을 출제했어요! 학생들에게 아래 QR코드를 보여주세요.")
                st.rerun()
            else:
                st.error(f"😥 미션 저장 중 문제가 생겼어요.\n\n상세 에러: {mission_err}")

    just_set = st.session_state.get("quick_mission_just_set")
    if just_set:
        st.info(f"📌 현재 출제: **{just_set['grade']} - {just_set['mission']}**")
        app_url = st.secrets.get("APP_URL", "") or KAKAO_REDIRECT_URI
        # ?tab=pass를 붙여야 학생이 QR을 찍었을 때 기본 화면이 아니라 바로
        # '💮 개념 PASS' 탭으로 떨어진다 (app.py 상단의 query_params 감지 로직 참고).
        qr_url = app_url + ("&tab=pass" if "?" in app_url else "?tab=pass")
        qr_bytes = generate_qr_png_bytes(qr_url)
        if qr_bytes:
            qr_c1, qr_c2, qr_c3 = st.columns([1, 2, 1])
            with qr_c2:
                st.image(qr_bytes, use_container_width=True)
            st.caption(f"📱 학생들이 이 QR을 카메라로 찍으면 바로 '개념 PASS' 화면으로 들어가요 → {qr_url}")
        else:
            st.warning(
                "QR코드 생성 라이브러리(qrcode)가 아직 이 환경에 설치되지 않았어요. "
                f"학생들에게 이 주소를 직접 알려주세요 → {qr_url}"
            )

    st.markdown("---")

    st.markdown("#### 📊 개념 PASS 실시간 현황")
    report_c1, report_c2 = st.columns([1, 1])
    with report_c1:
        report_date = st.date_input(
            "날짜", value=datetime.now(KST).date(), key="pass_report_date"
        )
    with report_c2:
        report_grade = st.selectbox(
            "학년", ["전체"] + PASS_GRADE_LEVELS, key="pass_report_grade"
        )

    report_ok, report_rows, report_err = fetch_pass_records_for_date(
        report_date.isoformat(), report_grade
    )
    if not report_ok:
        show_friendly_error(report_err, "😥 PASS 현황을 불러오는 중 문제가 생겼어요.")
    elif not report_rows:
        st.info("조건에 맞는 PASS 기록이 없습니다.")
    else:
        st.caption(f"총 {len(report_rows)}건")
        st.dataframe(
            [
                {
                    "닉네임": _embedded_user(r).get("nickname") or "익명 학생",
                    "학년": _embedded_user(r).get("pass_grade") or "-",
                    "통과 시각": _iso_to_kst_label(r.get("created_at"), fmt="%H:%M"),
                    "통과 미션": r.get("topic", "-"),
                }
                for r in report_rows
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("---")
    render_solution_db_admin_section()


def send_telegram_alert(msg_type, content_preview):
    """학생 질문 제출 시 선생님 폰으로 실시간 텔레그램 알림을 쏜다.

    토큰/챗ID는 코드에 하드코딩하지 않고 st.secrets에서만 읽는다.
    (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID 를 .streamlit/secrets.toml 또는
    배포 플랫폼의 Secrets에 설정해야 동작한다.)
    """
    token = st.secrets.get("TELEGRAM_TOKEN", "")
    chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    text = (
        f"📱 *[다혜 쌤 AI 클리닉 실시간 알림]*\n\n"
        f"📌 *유형:* {msg_type}\n"
        f"💬 *내용:* {content_preview[:150]}"
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    try:
        requests.post(url, json=payload, timeout=2)
    except Exception:
        pass


def send_telegram_photo(photo_bytes, student_name="익명 학생", extra_info=""):
    """학생이 올린 문제 사진 원본을 텔레그램으로 전송합니다."""
    token = st.secrets.get("TELEGRAM_TOKEN", "")
    chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    caption = (
        f"📱 *[다혜 쌤 AI 클리닉 - 새 문제 접수]*\n\n"
        f"👤 *학생:* {student_name}\n"
        f"💬 *메모:* {extra_info[:100]}"
    )

    files = {"photo": ("problem.jpg", photo_bytes, "image/jpeg")}
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}

    try:
        requests.post(url, data=data, files=files, timeout=5)
    except Exception:
        pass


def log_to_slack_and_gsheet(student_name, student_grade, mode, student_text, ai_reply, skip_telegram=False):
    slack_webhook_url = st.secrets.get("SLACK_WEBHOOK_URL", "")
    slack_bot_token = st.secrets.get("SLACK_BOT_TOKEN", "")
    slack_channel_id = st.secrets.get("SLACK_CHANNEL_ID", "")
    gsheet_url = st.secrets.get("GSHEET_WEBHOOK_URL", "")

    # 사진 업로드 건은 send_telegram_photo가 이미 원본 사진 + 학생 이름을 텔레그램으로
    # 보냈으므로, 여기서 중복으로 텍스트 알림을 또 쏘지 않는다.
    if not skip_telegram:
        send_telegram_alert(mode, student_text if student_text else "(사진 업로드)")

    kst = timezone(timedelta(hours=9))
    now_str = datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S")

    thread_ts = st.session_state.get("slack_thread_ts", None)

    slack_text = (
        f"🔔 *[알찬학원 수학 클리닉]*\n"
        f"• *학생*: {student_name} ({student_grade})\n"
        f"• *시각*: {now_str}\n"
        f"• *모드*: {mode}\n"
        f"• *최신 입력*: {student_text if student_text else '(사진 업로드)'}\n\n"
        f"🤖 *AI 마지막 답변*:\n{ai_reply}"
    )

    if slack_bot_token and slack_channel_id:
        try:
            headers = {
                "Authorization": f"Bearer {slack_bot_token}",
                "Content-Type": "application/json; charset=utf-8"
            }
            payload = {
                "channel": slack_channel_id,
                "text": slack_text
            }
            if thread_ts:
                payload["thread_ts"] = thread_ts

            res = requests.post("https://slack.com/api/chat.postMessage", headers=headers, json=payload, timeout=3)
            res_json = res.json()
            if res_json.get("ok") and not thread_ts:
                st.session_state.slack_thread_ts = res_json.get("ts")
        except Exception:
            pass
    elif slack_webhook_url:
        try:
            slack_payload = {"text": slack_text}
            if thread_ts:
                slack_payload["thread_ts"] = thread_ts
            requests.post(slack_webhook_url, json=slack_payload, timeout=3)
        except Exception:
            pass

    if gsheet_url:
        try:
            gsheet_payload = {
                "timestamp": now_str,
                "student_name": f"{student_name} ({student_grade})",
                "mode": mode,
                "prompt": student_text if student_text else "(사진 최초 제출)",
                "response": ai_reply
            }
            requests.post(gsheet_url, json=gsheet_payload, timeout=3)
        except Exception:
            pass


def inject_custom_css():
    st.markdown(
        """
    <style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
    @import url('https://fonts.googleapis.com/css2?family=Gowun+Dodum&display=swap');

    html, body, .stApp {
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }

    [data-testid="stIcon"], i, [class*="icon"], [class*="material"] {
        font-family: inherit !important;
    }

    .stApp {
        background-color: #F8FAFC;
    }

    /* 사이드바는 완전히 숨기지 않고 기본 펼침(expanded) 상태로 둔다 - 로그아웃 버튼이
       여기 들어있다. "선생님 모드" 진입은 태블릿/모바일에서 사이드바를 열기 번거로운
       문제 때문에 '👤 마이페이지' 화면 맨 아래로 옮겼다. */

    /* ==========================================
       📱 모바일 앱 셸 - Streamlit 기본 크롬 숨기기 + 폰 화면 폭으로 고정
       ========================================== */
    #MainMenu,
    [data-testid="stToolbar"],
    [data-testid="stDecoration"],
    [data-testid="stStatusWidget"],
    footer,
    .viewerBadge_container__1QSob,
    [class*="viewerBadge"] {
        display: none !important;
    }

    /* stHeader 자체는 display:none으로 지우지 않는다 - 사이드바를 접었다가 다시 펼치는
       토글 버튼(▶)이 Streamlit 버전에 따라 이 헤더 영역 안에 같이 렌더링되는데, 헤더를
       통째로 숨기면 PC에서 사이드바를 한 번 접으면 다시 펼 방법이 사라져 버린다. 보기
       싫은 메뉴/툴바 등은 위에서 각각 개별적으로 숨기고, 헤더 자체는 투명하게 깔아
       공간만 접어 둔다. */
    header[data-testid="stHeader"] {
        background: transparent !important;
        height: auto !important;
    }

    /* 본문을 스마트폰 폭(480px)으로 가운데 정렬해서, PC에서도 폰 화면처럼 보이게 한다.
       하단 고정 탭바(아래 stRadio 스타일)에 마지막 콘텐츠가 가리지 않도록 아래쪽
       여백도 넉넉히 둔다. */
    .main .block-container {
        max-width: 480px !important;
        margin: 0 auto !important;
        padding-top: 1.2rem !important;
        padding-left: 1rem !important;
        padding-right: 1rem !important;
        padding-bottom: 100px !important;
    }

    /* ==========================================
       🧭 하단 고정 탭바 (st.radio를 스마트폰 앱의 Bottom Tab Bar로 변신시킨다)
       ========================================== */
    div[data-testid="stRadio"] {
        position: fixed !important;
        left: 0 !important;
        right: 0 !important;
        bottom: 0 !important;
        width: 100% !important;
        max-width: 480px !important;
        margin: 0 auto !important;
        background-color: #FFFFFF !important;
        border-top: 1px solid #E5E7EB !important;
        box-shadow: 0 -6px 18px rgba(15, 23, 42, 0.07) !important;
        padding: 0.35rem 0.3rem calc(0.35rem + env(safe-area-inset-bottom, 0px)) !important;
        z-index: 9999 !important;
    }

    /* 위젯 라벨("모드 선택")은 화면에 보일 필요가 없다 */
    div[data-testid="stRadio"] > label {
        display: none !important;
    }

    div[data-testid="stRadio"] [role="radiogroup"] {
        display: flex !important;
        flex-direction: row !important;
        justify-content: space-around !important;
        align-items: stretch !important;
        width: 100% !important;
        gap: 0.2rem !important;
    }

    div[data-testid="stRadio"] [role="radiogroup"] label {
        flex: 1 1 0 !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        background: transparent !important;
        margin: 0 !important;
        padding: 0.55rem 0.2rem !important;
        border-radius: 14px !important;
        cursor: pointer !important;
        transition: background-color 0.15s ease !important;
    }

    div[data-testid="stRadio"] [role="radiogroup"] label > div {
        font-size: 0.8rem !important;
        font-weight: 700 !important;
        color: #94A3B8 !important;
        text-align: center !important;
        white-space: nowrap !important;
    }

    /* 실제 동그라미 라디오 버튼은 완전히 제거한다. BaseWeb이 input 자체에 원형
       스타일(appearance 등)을 CSS-in-JS로 입혀서 specificity가 높을 수 있으므로,
       아래 속성 전부에 !important를 붙여 무조건 이긴다. 또한 "label의 직속 자식
       div"로만 제한하지 않고(그 사이에 감싸는 wrapper div가 있을 수 있음), label
       내부 어디에 있든 첫 번째 자식인 원형 장식 div를 descendant 선택자로 넓게
       잡아 숨긴다. */
    div[data-testid="stRadio"] [role="radiogroup"] label input {
        position: absolute !important;
        opacity: 0 !important;
        width: 1px !important;
        height: 1px !important;
        margin: 0 !important;
        padding: 0 !important;
        pointer-events: none !important;
    }
    div[role="radiogroup"] label div:first-child {
        display: none !important;
    }

    /* 선택된 탭 강조 (iPad Safari 포함 최신 브라우저는 :has() 지원) */
    div[data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) {
        background-color: #EEF2FF !important;
    }
    div[data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) > div {
        color: #3A449A !important;
    }

    h1, h2, h3 {
        color: #3A449A !important;
        font-weight: 800 !important;
        word-break: keep-all !important;
    }

    h1 {
        font-size: clamp(1.3rem, 4vw, 1.8rem) !important;
        line-height: 1.35 !important;
    }

    .guide-box {
        background-color: #FFFFFF;
        border: 2px solid #3A449A;
        border-radius: 12px;
        padding: 1.2rem 1.4rem;
        margin-bottom: 1.8rem;
        box-shadow: 0 4px 12px rgba(58, 68, 154, 0.06);
    }
    .guide-title {
        color: #3A449A;
        font-weight: 800;
        font-size: 1.15rem;
        margin-bottom: 0.6rem;
    }
    .guide-item {
        color: #1E293B;
        font-size: 0.98rem;
        margin-bottom: 0.4rem;
        line-height: 1.6;
    }

    blockquote {
        background-color: #FFFFFF !important;
        border-left: 5px solid #00A19D !important;
        border-radius: 12px !important;
        padding: 1.2rem 1.5rem !important;
        box-shadow: 0 4px 12px rgba(58, 68, 154, 0.08) !important;
        margin: 1.2rem 0 !important;
    }

    blockquote *, blockquote p, blockquote li, blockquote span, blockquote div {
        color: #000000 !important;
        font-weight: 500 !important;
        opacity: 1 !important;
    }

    .stChatMessage, .stChatMessage * {
        color: #000000 !important;
    }

    /* ==========================================
       🔓 "80% 풀이 바로 보기" - 보조적인 마지막 수단이라는 느낌으로 톤다운
       (강조색을 빼서 학생이 메인 버튼으로 착각해 바로 누르지 않도록 한다)
       ========================================== */
    div[data-testid$="_80_btn"] button {
        background-color: #F0F2F6 !important;
        color: #6B7280 !important;
        border: 1px solid #D1D5DB !important;
        box-shadow: none !important;
        font-weight: 500 !important;
    }

    div[data-testid$="_80_btn"] button:hover {
        background-color: #E5E7EB !important;
        color: #4B5563 !important;
        border-color: #D1D5DB !important;
    }

    div[data-testid$="_80_btn"] button:focus:not(:active) {
        color: #6B7280 !important;
        border-color: #D1D5DB !important;
        box-shadow: none !important;
    }

    /* ==========================================
       💬 오리지널 카카오 로그인 버튼 (공식 브랜드 컬러)
       ========================================== */
    .kakao-login-btn {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 0.45rem;
        width: 100%;
        background-color: #FEE500;
        color: #000000 !important;
        font-weight: 700;
        font-size: 1.05rem;
        text-decoration: none !important;
        border-radius: 12px;
        padding: 0.85rem 1rem;
        margin: 0.6rem 0;
        box-shadow: 0 4px 10px rgba(254, 229, 0, 0.45);
        transition: transform 0.15s ease, box-shadow 0.15s ease;
    }

    .kakao-login-btn:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 16px rgba(254, 229, 0, 0.6);
    }

    .kakao-login-btn-icon {
        font-size: 1.2rem;
        line-height: 1;
    }

    [data-testid="stFileUploader"] {
        background-color: #FFFFFF;
        border-radius: 12px;
        padding: 0.6rem;
    }

    .katex-display {
        background-color: #F1F5F9;
        padding: 0.5rem;
        border-radius: 8px;
        color: #000000 !important;
    }

    /* 캔버스(필기 칠판) 모바일 반응형 강제 적용 */
    div[data-testid="stCanvas"] iframe, div[data-testid="stCanvas"] canvas,
    [data-testid="stCustomComponentV1"] iframe,
    .st-key-math_canvas iframe, .st-key-math_canvas canvas {
        max-width: 100% !important;
        height: auto !important;
    }

    /* ==========================================
       ✍️ 아이패드 판서/노트 감성 - AI 답변 박스
       (질문 클리닉 말풍선 + 개념 PASS 피드백 공통 적용)
       ========================================== */
    .handwritten-note {
        position: relative;
        background-color: #FDFBF7;
        background-image:
            linear-gradient(#ECE6D6 1px, transparent 1px),
            linear-gradient(90deg, #ECE6D6 1px, transparent 1px);
        background-size: 26px 26px;
        border: 1px solid #ECE4D0;
        border-radius: 16px;
        box-shadow: 0 8px 20px rgba(120, 104, 68, 0.14), 0 1px 3px rgba(120, 104, 68, 0.08);
        padding: 1.1rem 1.4rem;
        margin: 0.4rem 0 0.9rem 0;
    }

    .handwritten-note,
    .handwritten-note p,
    .handwritten-note li,
    .handwritten-note span,
    .handwritten-note em {
        font-family: 'Gowun Dodum', 'Pretendard', sans-serif !important;
        color: #4A3F2E !important;
        font-size: 1.14rem !important;
        line-height: 1.85 !important;
        font-weight: 400 !important;
    }

    .handwritten-note strong {
        font-family: 'Gowun Dodum', 'Pretendard', sans-serif !important;
        color: #2E4A8F !important;
        font-weight: 700 !important;
    }

    /* 수식(LaTeX/KaTeX)은 손글씨 폰트의 영향을 받지 않고 원래 수식 폰트를 유지해 기호가 깨지지 않게 한다 */
    .handwritten-note .katex,
    .handwritten-note .katex * {
        font-family: KaTeX_Main, KaTeX_Math, "Times New Roman", serif !important;
        color: #1E293B !important;
    }

    .handwritten-note .katex-display {
        background-color: rgba(255, 255, 255, 0.55) !important;
    }

    /* AI 답변이 간혹 인용문(>) 형식을 쓸 때도 노트 배경과 어울리도록 보정 */
    .handwritten-note blockquote {
        background-color: rgba(255, 255, 255, 0.55) !important;
        box-shadow: none !important;
    }
    .handwritten-note blockquote *,
    .handwritten-note blockquote p,
    .handwritten-note blockquote li {
        font-family: 'Gowun Dodum', 'Pretendard', sans-serif !important;
        color: #4A3F2E !important;
    }

    /* ==========================================
       📅 개념 PASS 탭 상단 - 오늘 날짜/요일 배너
       ========================================== */
    .pass-date-banner {
        background: linear-gradient(135deg, #3A449A, #00A19D);
        border-radius: 16px;
        padding: 0.9rem 1.3rem;
        margin-bottom: 1rem;
        text-align: center;
        box-shadow: 0 6px 16px rgba(58, 68, 154, 0.25);
    }

    .pass-date-banner .pass-date-text {
        color: #FFFFFF;
        font-weight: 800;
        font-size: 1.25rem;
        letter-spacing: 0.01em;
    }

    @media (max-width: 480px) {
        .pass-date-banner .pass-date-text {
            font-size: 1.05rem;
        }
    }

    /* ==========================================
       🎮 듀오링고 스타일 PASS 스탬프 쿠폰판
       ========================================== */
    .stamp-board {
        background: linear-gradient(180deg, #FFFDF5, #FFF8E1);
        border: 1px solid #FFE9A8;
        border-radius: 22px;
        padding: 1.3rem 1.4rem 1.6rem;
        margin: 0.6rem 0 1.3rem 0;
        box-shadow: 0 10px 24px rgba(255, 193, 7, 0.16);
    }

    .stamp-board-title {
        font-weight: 800;
        font-size: 1.05rem;
        color: #3A449A;
        margin-bottom: 0.8rem;
    }

    .stamp-progress-track {
        width: 100%;
        height: 16px;
        background: #F1EADA;
        border-radius: 999px;
        overflow: hidden;
        box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.1);
        margin-bottom: 0.6rem;
    }

    .stamp-progress-fill {
        height: 100%;
        background: linear-gradient(90deg, #FFC800, #FF9F1C);
        border-radius: 999px;
        transition: width 0.5s ease;
    }

    .stamp-motivation {
        font-family: 'Gowun Dodum', 'Pretendard', sans-serif !important;
        font-size: 1.08rem;
        color: #8A5A00;
        font-weight: 700;
        margin-bottom: 1.1rem;
    }

    .stamp-row {
        display: flex;
        flex-wrap: wrap;
        align-items: flex-end;
        justify-content: center;
        gap: 12px;
        row-gap: 20px;
    }

    .stamp-cell {
        width: 52px;
        height: 52px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 1.5rem;
        flex-shrink: 0;
    }

    .stamp-cell.earned {
        background: radial-gradient(circle at 32% 28%, #FFEA8A, #FFC800 70%);
        box-shadow: 0 4px 0 #E2A500, 0 8px 16px rgba(255, 180, 0, 0.45);
        transform: translateY(-4px);
    }

    .stamp-cell.locked {
        background: #E5E5E5;
        color: #ADADAD;
        box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.1);
    }

    .stamp-cell.reward {
        width: 80px;
        height: 80px;
        font-size: 2.4rem;
    }

    .stamp-cell.reward.earned {
        background: radial-gradient(circle at 32% 28%, #FFF1B0, #FFC800 65%);
        box-shadow: 0 5px 0 #E2A500, 0 12px 22px rgba(255, 180, 0, 0.5);
    }

    .stamp-cell.reward.locked {
        background: #EDEDED;
        box-shadow: inset 0 2px 5px rgba(0, 0, 0, 0.12);
    }

    @media (max-width: 480px) {
        .stamp-cell {
            width: 42px;
            height: 42px;
            font-size: 1.25rem;
        }
        .stamp-cell.reward {
            width: 64px;
            height: 64px;
            font-size: 1.9rem;
        }
        .stamp-row {
            gap: 8px;
            row-gap: 16px;
        }
    }
    </style>
    """,
        unsafe_allow_html=True,
    )
inject_custom_css()


def get_response_text(response):
    text_parts = []
    for block in response.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
    full_text = "\n".join(text_parts)
    cleaned = re.sub(r"<thinking>.*?</thinking>", "", full_text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<thinking>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


def show_friendly_error(exc, friendly_message="😥 일시적인 오류가 발생했어요. 잠시 후 다시 시도해 주세요."):
    """예외가 나면 학생/선생님 화면에는 친절한 한 줄만 보여주고, 기술적인 원문은 접어서
    필요할 때만(디버깅 등) 펼쳐 보게 한다. raw 예외 문자열을 화면에 그대로 띄우지 않는다."""
    st.error(friendly_message)
    with st.expander("기술적인 오류 내용 보기"):
        st.code(str(exc))


def is_safe_matplotlib_code(code_str):
    """모델이 생성한 도형 코드를 exec 하기 전 AST 화이트리스트로 검증한다.

    - import 문 금지 (필요한 모듈은 이미 스코프에 제공됨)
    - 밑줄(_)로 시작하는 모든 속성 접근 금지 (__getattribute__/__class__ 등 우회 차단)
    - 던더 이름 금지, 파일/OS/네트워크 관련 속성 금지
    - 허용된 내장 함수 외의 이름 호출 금지 (getattr/eval/exec/open 등 차단)
    - 500만 초과 숫자 상수·지수 8 초과 금지 (메모리 폭탄 방지)
    """
    try:
        tree = ast.parse(code_str)
    except Exception:
        return False

    allowed_call_names = {
        "range", "len", "enumerate", "zip", "min", "max", "abs", "sum", "round",
        "list", "tuple", "dict", "set", "float", "int", "str", "bool",
        "sorted", "reversed", "map", "filter",
    }
    denied_attrs = {
        "secrets", "environ", "system", "popen", "getenv", "putenv", "savefig",
        "communicate", "check_output", "check_call", "run", "Popen",
        "read", "write", "open", "remove", "unlink", "rename",
        "load", "loads", "connect", "urlopen", "request",
    }

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return False
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in denied_attrs:
                return False
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            return False
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in allowed_call_names:
                return False
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            if abs(node.value) > 5_000_000:
                return False
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            rhs = node.right
            if isinstance(rhs, ast.Constant) and isinstance(rhs.value, (int, float)) and rhs.value > 8:
                return False
    return True


import streamlit.components.v1 as components


def render_jsxgraph(js_code):
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <link rel="stylesheet" type="text/css" href="https://cdn.jsdelivr.net/npm/jsxgraph/distrib/jsxgraph.css" />
        <script type="text/javascript" src="https://cdn.jsdelivr.net/npm/jsxgraph/distrib/jsxgraphcore.js"></script>
        <style>
            body {{ margin: 0; padding: 0; background-color: #f8fafc; }}
            #jxgbox {{ width: 100%; max-width: 360px; height: 300px; margin: 10px auto; border-radius: 12px; border: 2px solid #3A449A; background-color: #ffffff; }}
        </style>
    </head>
    <body>
        <div id="jxgbox" class="jxgbox"></div>
        <script type="text/javascript">
            var board = JXG.JSXGraph.initBoard('jxgbox', {{
                boundingbox: [-7, 7, 7, -7],
                axis: true,
                showCopyright: false,
                showNavigation: false,
                keepaspectratio: true
            }});
            try {{
                {js_code}
            }} catch(e) {{
                console.log(e);
            }}
        </script>
    </body>
    </html>
    """
    components.html(html_content, height=325)


def render_handwritten_note(markdown_text):
    """AI 답변을 '아이패드 판서 노트' 감성 박스(.handwritten-note)로 감싸서 렌더링한다.

    여는 태그와 닫는 태그를 반드시 같은 st.markdown() 호출 안에 넣어야 한다 - 서로 다른
    호출로 나누면 각 호출이 독립된 DOM 블록이 되어 하나의 박스로 묶이지 않는다.
    태그 바로 안쪽에 빈 줄을 둬야 내부 텍스트가 LaTeX 수식을 포함해 마크다운으로 정상 처리된다.
    """
    st.markdown(f'<div class="handwritten-note">\n\n{markdown_text}\n\n</div>', unsafe_allow_html=True)


def render_assistant_content(content):
    # JS 코드 블록(```javascript 또는 ```js) 감지 및 JSXGraph 렌더링
    pattern = r"```(?:javascript|js)\s*(.*?)\s*```"
    parts = re.split(pattern, content, flags=re.DOTALL | re.IGNORECASE)

    for i, part in enumerate(parts):
        if i % 2 == 0:
            if part.strip():
                render_handwritten_note(part)
        else:
            js_code = part.strip()
            render_jsxgraph(js_code)


def _render_clinic_message(msg):
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant":
            render_assistant_content(msg["content"])
        elif msg.get("images"):
            for img in msg["images"]:
                st.image(img["bytes"], width=320)
            if msg["content"]:
                st.caption(msg["content"])
        else:
            st.markdown(msg["content"])


GENERAL_QUESTION_PROBLEM_CODE = "일반질문"


def _is_general_question(row):
    """student_questions 행이 '🔍 오답 첨삭'의 손글씨 첨삭이 아니라 '❓ 질문 클리닉'의
    텍스트 질문인지 구분한다 (log_student_question이 problem_code="일반질문"으로
    남긴 행)."""
    return (row.get("problem_code") or "") == GENERAL_QUESTION_PROBLEM_CODE


def _format_student_question_labels(row):
    """student_questions 행 1건을 (기능 라벨, 문제 라벨) 표시용 문자열로 바꾼다 -
    마이페이지 오답노트와 선생님 대시보드가 같은 표기를 쓰도록 한곳에 모아 둔다."""
    if _is_general_question(row):
        return "❓ 질문 클리닉", "💬 일반 질문"
    problem_code = row.get("problem_code")
    problem_label = f"문제 {problem_code}번" if problem_code else "문제 번호 미인식"
    return "🔍 오답 첨삭", problem_label


def render_question_archive_cards():
    """'👤 마이페이지 > 📝 오답노트' - student_questions을 Supabase에서 직접 조회해
    날짜별로 묶어 보여준다.

    예전에는 st.session_state.question_archive(로그인 시 한 번 복원해 둔 캐시)를
    그렸는데, 그 캐시가 세션에만 있다 보니 새로고침 등으로 세션이 새로 시작되면
    로그인하기 전까지 빈 화면만 보이는 문제가 있었다. 이제는 이 함수가 호출될
    때마다 - 세션 상태가 뭘 들고 있든 상관없이 - DB를 직접 조회한다.
    """
    kakao_id = st.session_state.get("kakao_id")
    if not kakao_id:
        st.info("로그인하면 오답노트를 볼 수 있어요.")
        return
    if not supabase:
        st.error(
            "🚨 Supabase 연결 정보(SUPABASE_URL/SUPABASE_SERVICE_KEY)가 설정되지 않아 "
            "오답노트를 불러올 수 없어요."
        )
        return

    try:
        res = (
            supabase.table("student_questions")
            .select("*")
            .eq("kakao_id", kakao_id)
            .order("created_at", desc=True)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        st.error(f"🚨 오답노트를 불러오는 중 DB 오류가 발생했어요.\n\n상세 원인: {e!r}")
        return

    if not rows:
        st.info(
            "아직 저장된 기록이 없어요. '🔍 오답 첨삭'에서 사진을 올리거나 '❓ 질문 클리닉'에서 "
            "질문을 주고받으면 여기에 차곡차곡 쌓여요!"
        )
        return

    by_date = {}
    for row in rows:
        date_key = (row.get("created_at") or "")[:10] or "날짜 미상"
        by_date.setdefault(date_key, []).append(row)

    for date_key in sorted(by_date.keys(), reverse=True):
        st.markdown(f"#### 🗓️ {date_key}")
        for row in by_date[date_key]:
            feature_label, problem_label = _format_student_question_labels(row)
            created_label = _iso_to_kst_label(row.get("created_at"))
            with st.expander(f"{feature_label} · {problem_label} · {created_label}"):
                if row.get("image_url"):
                    st.image(row["image_url"], width=280)
                if row.get("ocr_text"):
                    st.markdown("**🙋 학생 질문**" if _is_general_question(row) else "**📝 판독된 풀이**")
                    st.code(row["ocr_text"])
                st.markdown("**🤖 다혜T의 답변**" if _is_general_question(row) else "**🤖 다혜T의 첨삭**")
                st.markdown(row.get("ai_feedback") or "-")


def load_system_prompt(student_grade):
    custom_notes = ""
    try:
        with open("custom_notes.txt", "r", encoding="utf-8") as f:
            custom_notes = "\n\n[선생님 전용 강의 및 단원별 풀이 노하우]:\n" + f.read()
    except FileNotFoundError:
        pass

    default_prompt = (
        "너는 친절하고 실력 있는 알찬학원 수학 선생님이다.\n"
        "말투는 학생에게 친근하고 다정한 반말('~해보자', '~했니?', '~란다')을 100% 사용해라.\n"
        "★ [이름 반복 언급 금지 경고]: 자기 자신을 3인칭('다혜 쌤은')으로 부르거나 이름을 반복하지 말고, 즉시 본론으로 들어가라.\n\n"
        # 이제 로그인 시 과목을 강제로 고르게 하지 않으므로, student_grade는 학생이
        # 직접 고른 값이 아니라 기본값(공통수학1)일 수도 있다 - "선택"이라고 단정하지
        # 않는 문구로 둔다.
        f"★ [현재 설정된 과목: {student_grade} (학생이 직접 고르지 않았을 수도 있음)]\n"
        "★ [🚨 1문 1답 인터랙티브 필수 수칙]:\n"
        "- 전체 단계를 한 번에 출력하지 말고, 출제 유형을 밝힌 후 오직 지금 풀어야 할 '첫 번째 질문(1단계)' 단 하나만 던지고 즉시 말을 마쳐라.\n\n"
        "★ [🚨 수식 서식 절대 규칙 - 한 줄로 뭉개서 쓰지 마라]:\n"
        "- 수식을 인라인($...$)으로 한 줄에 연달아 나열하지 마라. (예: '$6x+9=41-10x 16x=32$' 절대 금지)\n"
        "- 수학 공식이나 계산 과정은 반드시 독립된 줄(Display Math: $$...$$)로 작성하고, 식 하나마다 위아래로 "
        "빈 줄(엔터 두 번, \\n\\n)을 넣어서 분리해라.\n"
        "- 올바른 출력 예시:\n"
        "  $$x^2 + 6x + 9 = 41 - 10x + x^2$$\n\n"
        "  $$16x = 32$$\n\n"
        "  $$x = 2$$\n\n"
        "★ [🚨 소크라테스 튜터링 절대 규칙 - 네가 대신 풀지 마라]:\n"
        "- (아래 '80% 사이다 구출 모드'가 학생에게 명시적으로 요청된 경우가 아니라면) 네가 문제를 대신 풀어서 "
        "최종 계산식이나 정답을 단계별로 나열하지 마라.\n"
        "- 학생의 풀이 중 '오류가 발생한 특정 줄'만 정확히 짚어내고, 그 줄에서 구체적으로 어떤 항/부호/개념이 "
        "빠졌는지(예: '완전제곱식 중간항(2ab) 누락')를 짚어주되, 고친 식을 네가 대신 적어주지는 마라.\n"
        "- 질문 형식: '2번째 줄에서 $(x+3)^2$을 전개할 때 앞뒤 제곱 사이에 빠진 항이 없는지 확인해 볼까?'처럼 "
        "1개의 핵심 유도 질문만 남기고 말을 마쳐라.\n\n"
        "★ [🚨 카톡 스타일 초간결 답변 (3줄 제한 필수)]:\n"
        "- 서론/인사말('안녕~', '사진을 보니~', '문제를 정리해보면~')을 절대 쓰지 마라! 아이들이 안 읽고 스킵한다.\n"
        "- 첫 줄부터 즉시 본론(핵심 힌트나 식)으로 들어가고, 전체 길이는 무조건 3~4줄 이내로 끊어라.\n\n"
        "★ [🆘 80% 사이다 구출 모드 (질문 고문 절대 금지)]:\n"
        "- 학생이 '모르겠어', '어려워', '80% 보여줘'라고 하거나 버튼을 누르면, 절대 '다시 생각해볼까?', '어디까지 했어?'라며 되묻지 마라.\n"
        "- 즉시 복잡한 식 전개나 논리 전개의 80%를 시원하게 네가 직접 작성해서 보여줘라.\n"
        "- 학생에게는 아주 쉬운 마지막 단순 계산(덧셈, 곱셈)이나 최종 부호 결정만 남겨줘서 성취감을 느끼게 해라.\n\n"
        "★ [📈 구글 자동 인터랙티브 그래프 연동 수칙]:\n"
        "- 시각화나 그래프 확인이 필요한 문제에서는 학생이 클릭하자마자 바로 인터랙티브 그래프가 떠오르는 구글 그래프 링크를 제공해라.\n"
        "- **URL 작성 규칙:** `https://www.google.com/search?q=plot+수식` (수식의 기호 `+`는 `%2B`로, `^`는 `%5E`로 교체)\n"
        "- **작성 예시:**\n"
        "  - 이차함수 y=x^2-4x+3: [📈 구글 인터랙티브 그래프 바로 보기](https://www.google.com/search?q=plot+y%3Dx%5E2-4x%2B3)\n"
        "  - 원의 방정식 x^2+y^2=25: [📈 원의 모양 바로 확인하기](https://www.google.com/search?q=plot+x%5E2%2By%5E2%3D25)\n"
        "- **안내 지시문:** '위 링크를 누르면 손가락으로 움직여볼 수 있는 그래프가 바로 뜨니까, 꼭짓점과 절편 위치를 눈으로 확인해 봐!'라고 안내해라.\n\n"
        "★ [🚨 교과과정 표준 해법(교과서 정석 풀이) 100% 준수 수칙]:\n"
        "1. **교과서 표준 알고리즘 사용:** 대학 수학, 편법 숏컷 절대 금지.\n"
        "2. **순수 단원 성질 활용:** 순수 도형은 좌표평면 없이 도형 성질로만 풀 것.\n"
        f"3. **출제 유형 명시:** 문제 상단에 `📌 [출제 유형: {student_grade} > 대단원 > 중단원 > 세부유형]` 한 줄만 먼저 띄워라.\n\n"
        "★ [📷 이미지 OCR & 기하 도형 오류 방어 수칙]:\n"
        "- 사진이 들어오면 점, 각도 수치, 조건 키워드부터 첫 줄에서 확인받아라.\n"
        "- 안 보이는 보조선을 지어내지 말 것.\n"
        "- 학생이 세운 식이 도형의 성질/조건(접선의 길이, 각도 등)으로부터 수학적으로 타당하게 유도된 "
        "것이라면 그 식 자체를 틀렸다고 억지 부리지 마라. (예: 접선의 성질로 AF=FE=x, CE=3이므로 "
        "빗변 CF=x+3이라면, 식 (x+3)^2=4^2+(5-x)^2 은 참이다 - 이 식을 의심하지 말고, 그 다음 전개/계산 "
        "과정에서만 오류를 찾아라.)\n\n"
        "4. 학생이 답을 요구하더라도 최종 정답만은 직접 내주지 마라.\n"
        "5. 수식은 100% LaTeX 표기법(`$ ... $` 또는 `$$ ... $$`)을 사용해라."
        f"{custom_notes}"
    )
    return default_prompt


PASS_SUCCESS_TOKEN = "PASS_SUCCESS"
PASS_FAIL_TOKEN = "PASS_FAIL"


def calculate_pass_streak(pass_records, today=None):
    """PASS 기록에서 날짜만 뽑아 '현재 며칠 연속 PASS 중'인지 계산한다.

    오늘 또는 어제 PASS 기록이 있어야 스트릭이 살아있다고 인정하고, 거기서부터
    거꾸로 하루씩 비교하며 끊기는 지점까지 센다. 오늘/어제 둘 다 기록이 없으면 0.
    """
    kst = timezone(timedelta(hours=9))
    today = today or datetime.now(kst).date()

    dates = set()
    for rec in pass_records or []:
        raw = str(rec.get("completed_at", ""))[:10]
        try:
            dates.add(datetime.strptime(raw, "%Y-%m-%d").date())
        except ValueError:
            continue

    if today in dates:
        cursor = today
    elif (today - timedelta(days=1)) in dates:
        cursor = today - timedelta(days=1)
    else:
        return 0

    streak = 0
    while cursor in dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def render_streak_badge(pass_records):
    """🔥 N일 연속 PASS 배지. 스트릭이 0이면 대신 부드러운 유도 문구를 보여준다."""
    streak = calculate_pass_streak(pass_records)
    if streak > 0:
        st.markdown(
            f"""
            <div style="
                display: inline-block;
                background: linear-gradient(135deg, #FF8A3D, #FF5A5F);
                color: #FFFFFF;
                font-weight: 800;
                font-size: 1.05rem;
                padding: 0.55rem 1.3rem;
                border-radius: 999px;
                box-shadow: 0 6px 16px rgba(255, 90, 95, 0.35);
                margin-bottom: 1rem;
            ">
                🔥 {streak}일 연속 PASS 달성 중!
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.caption("💤 아직 연속 기록이 없어요. 오늘 '💮 개념 PASS'에 도전해서 스트릭을 시작해보자!")


def get_stamp_board_state(total_pass_count):
    """총 PASS 개수로 '몇 번째 10칸 스탬프판'이 지금 채워지고 있는지 계산한다.

    10개를 채우면 그 판은 완성(10/10)으로 유지되고, 11번째 PASS부터 다음 판이
    1칸부터 새로 시작된다. (예: 10개 → 1번판 10/10, 11개 → 2번판 1/10)
    """
    if total_pass_count <= 0:
        return 1, 0
    board_number = (total_pass_count - 1) // 10 + 1
    filled = total_pass_count - (board_number - 1) * 10
    return board_number, filled


def render_stamp_board(pass_records):
    """지금까지 받은 PASS 개수를 듀오링고 스타일의 10칸 디지털 스탬프 쿠폰판으로 시각화한다.

    st.columns 대신 CSS flexbox 기반 커스텀 컴포넌트(.stamp-board)를 한 번의 st.markdown
    호출로 그려서, 모바일/태블릿에서도 10칸이 찌그러지지 않고 자동 줄바꿈되게 한다.
    """
    total = len(pass_records or [])
    board_number, filled = get_stamp_board_state(total)
    progress_pct = int(filled / 10 * 100)
    remaining = max(0, 10 - filled)

    if filled >= 10:
        motivation = "🎉 10개 달성! 다혜쌤에게 보여주고 선물을 받아보세요!"
    else:
        motivation = f"다음 보상까지 {remaining}개 남았어요! 화이팅! 🔥"

    cells = []
    for i in range(10):
        earned = i < filled
        is_reward = i == 9
        classes = "stamp-cell"
        if is_reward:
            classes += " reward"
        classes += " earned" if earned else " locked"
        if is_reward:
            icon = "🎁" if earned else "🏆"
        else:
            icon = "⭐" if earned else "🔒"
        cells.append(f'<div class="{classes}">{icon}</div>')

    st.markdown(
        f"""<div class="stamp-board">
            <div class="stamp-board-title">🎟️ 나의 PASS 스탬프 쿠폰판 · {board_number}번째 판 ({filled}/10)</div>
            <div class="stamp-progress-track"><div class="stamp-progress-fill" style="width:{progress_pct}%;"></div></div>
            <div class="stamp-motivation">{motivation}</div>
            <div class="stamp-row">{''.join(cells)}</div>
        </div>""",
        unsafe_allow_html=True,
    )


def load_pass_system_prompt(pass_grade, scope_text):
    return (
        "너는 알찬학원의 '오늘의 개념 PASS' 테스트를 담당하는, 깐깐하지만 따뜻한 조교다.\n\n"
        "★ [0. 가장 중요한 절대 원칙 - 포기 금지, 최우선 강제 규칙]\n"
        "너는 포기를 모르는 집요하고 친절한 수학 선생님이다. 학생이 완전히 엉뚱한 소리를 하더라도 "
        "절대 포기하지 마라. 정답을 떠먹여 주는 수준의 힌트(예: '근의 공식 앞부분은 2a분의 -b 였지? "
        "루트 안에는 뭐가 들어갈까?')를 주어, 학생이 어떻게든 다음 시도에서 'PASS'를 받을 수 있도록 "
        "끝까지 유도해라. 대화의 끝은 무조건 'PASS(하원 이미지 출력)'여야 한다. '오늘은 여기까지 "
        "해보자' 같은 말로 중간에 포기하고 끝내는 것은 절대, 절대 금지한다.\n\n"
        f"★ [학생 학년: {pass_grade}]\n"
        f"★ [🚨 오늘 선생님이 지정한 범위 - 반드시 이 범위 안에서만 출제할 것]: {scope_text}\n"
        "- 위 지정 범위를 벗어난 다른 단원이나 개념은 절대 묻지 마라. 학생이 다른 단원 이야기를 꺼내도 지정 범위로 다시 데려와라.\n\n"
        "★ [1. 질문 던지기]\n"
        "- 위 지정 범위의 핵심 개념이나 자주 틀리는 오개념을 확인할 수 있는 질문을 오직 1개만 던져라.\n"
        "- 질문 문장 안에 답이나 핵심 키워드를 절대 미리 흘리지 마라.\n"
        "- 가능하면 '오늘 선생님이 칠판에 적어주신 내용 중 ~에 대해 설명해 보세요' 같은 식으로, 오늘 수업에서 "
        "직접 배운 내용을 짚어주는 듯한 말투로 출제해라.\n\n"
        "★ [2. 채점 원칙 - 두 가지 결과 중 하나로만 판정해라 (FAIL은 쓰지 않는다)]\n"
        "- 너무 깐깐하게 실패 처리하지 마라. 이 테스트에는 '실패로 끝내기'가 없다. 아래 두 경우만 있다.\n\n"
        "① PASS(통과) - 학생의 답변이 개념의 핵심 의미를 꿰뚫고 있을 때. 반드시 아래 3줄 형식을 정확히 "
        "지켜서 출력해라 (다른 설명 없이 이 형식 그대로):\n"
        f"  1번째 줄: '{PASS_SUCCESS_TOKEN}' 이라는 단어 단독으로만\n"
        "  2번째 줄: 'SUMMARY: ' 다음에 이 개념의 핵심을 15~30자 내외 한 문장으로 요약 (인증샷 카드에 그대로 노출됨)\n"
        "  3번째 줄부터: 이모지를 듬뿍 써서 아주 크고 신나게 칭찬하는 축하 메시지 1~2줄\n\n"
        "★ [2-1. 융통성 있는 정답 인정 - 앵무새처럼 똑같은 말 반복 요구 금지]\n"
        "- 수학적 핵심 의미나 원리가 통한다면, 학생만의 투박한 표현이라도 100% 정답으로 인정(PASS)해라. "
        "교과서 문장을 토씨 하나 틀리지 않고 그대로 써야만 정답으로 치는 태도는 절대 금지한다.\n"
        "- 이미 학생이 언급한 내용을 교과서 워딩으로 바꿔서 다시 쓰라고 요구하는 것은 절대 금지한다. "
        "같은 말을 반복시키지 마라.\n"
        "- 부족한 키워드가 딱 하나 남았다면 그것만 짚어서 물어봐라.\n\n"
        "② RETRY(기본값, 사실상의 유일한 미통과 판정) - PASS 수준이 아니면 전부 RETRY다. 핵심 키워드를 "
        "한두 개 정도만 빼먹었을 때는 물론이고, 학생이 완전히 엉뚱한 소리를 했을 때도 FAIL로 끝내지 말고 "
        "RETRY로 계속 이어가라. 절대 매몰차게 끝내지 말고, 시도할수록 힌트를 더 쉽고 더 구체적으로 "
        "낮춰서(필요하면 정답을 거의 다 알려주는 수준까지) 줘서, 학생이 다음 시도에는 반드시 PASS를 "
        "받을 수 있게 만들어라. '거의 다 왔어! [빠진 키워드/개념]을 포함해서 다시 한 번 설명해볼래?' "
        "같은 격려 섞인 말투로 힌트 한 줄을 주고 같은 질문에 다시 답할 기회를 줘라. 특별한 형식 없이 "
        "그냥 평소처럼 답하면 된다(토큰 없이 보통 문장으로).\n\n"
        f"- '{PASS_SUCCESS_TOKEN}' 토큰을 쓰지 않는 평범한 대답이면 자동으로 RETRY로 처리되니, 통과시킬 "
        "수준이 아니라면 그냥 토큰 없이 (점점 더 쉬워지는) 힌트만 담아 답하면 된다.\n"
        f"- '{PASS_FAIL_TOKEN}' 토큰은 절대 사용하지 마라. 몇 번을 시도하든 끝까지 RETRY 힌트로 끌고 가서 "
        "결국 PASS로 마무리해라.\n\n"
        "★ [3. 말투]\n"
        "- 학생에게 친근하고 다정한 반말('~해보자', '~했니?', '~란다')을 사용해라.\n"
        "- 힌트를 줄 때도 깐깐하게 핵심만 짚어주되, 끝까지 응원하는 태도를 잃지 마라."
    )


# ==========================================
# 💮 PASS 인증샷(세로 포토카드) 생성
# ==========================================
FONT_DIR = "assets/fonts"


def _pass_font(weight, size):
    """굿노트 감성 카드에 쓸 한글 폰트. 번들 폰트가 없으면(배포 환경 이슈 등) 기본 폰트로 조용히 대체한다."""
    path = os.path.join(FONT_DIR, f"Pretendard-{weight}.otf")
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _wrap_text(draw, text, font, max_width):
    """공백 기준으로 줄바꿈하고, 공백 없이 긴 토큰은 글자 단위로 강제 개행한다."""
    lines = []
    current = ""
    for word in text.split(" "):
        trial = f"{current} {word}".strip()
        if not current or draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    final_lines = []
    for line in lines:
        if draw.textlength(line, font=font) <= max_width:
            final_lines.append(line)
            continue
        buf = ""
        for ch in line:
            trial = buf + ch
            if not buf or draw.textlength(trial, font=font) <= max_width:
                buf = trial
            else:
                final_lines.append(buf)
                buf = ch
        if buf:
            final_lines.append(buf)
    return final_lines


def _fit_text(draw, text, weight, max_width, max_lines, start_size, min_size=26):
    """줄 수 제한 안에 들어올 때까지 폰트 크기를 줄여가며 줄바꿈한다."""
    size = start_size
    while size >= min_size:
        font = _pass_font(weight, size)
        lines = _wrap_text(draw, text, font, max_width)
        if len(lines) <= max_lines:
            return font, lines
        size -= 4
    font = _pass_font(weight, min_size)
    return font, _wrap_text(draw, text, font, max_width)[:max_lines]


def _draw_centered_lines(draw, lines, font, center_x, top_y, line_height, fill):
    y = top_y
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text((center_x - w / 2, y), line, font=font, fill=fill)
        y += line_height
    return y


def _draw_pass_stamp(draw, img, center_x, center_y, diameter=260):
    """빨간 원형 '도장' 스탬프를 그려서 회전시킨 뒤 카드에 합성한다. (이모지 글리프는 폰트가 지원하지 않아 직접 벡터로 그린다)"""
    stamp_red = (196, 42, 42, 255)
    size = diameter + 40
    stamp = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(stamp)
    c = size / 2
    r_outer = diameter / 2
    r_inner = r_outer - 14
    sdraw.ellipse([c - r_outer, c - r_outer, c + r_outer, c + r_outer], outline=stamp_red, width=7)
    sdraw.ellipse([c - r_inner, c - r_inner, c + r_inner, c + r_inner], outline=stamp_red, width=3)

    f_pass = _pass_font("ExtraBold", int(diameter * 0.26))
    f_hangul = _pass_font("Bold", int(diameter * 0.15))
    pass_w = sdraw.textlength("PASS", font=f_pass)
    sdraw.text((c - pass_w / 2, c - diameter * 0.22), "PASS", font=f_pass, fill=stamp_red)
    hangul_w = sdraw.textlength("합격", font=f_hangul)
    sdraw.text((c - hangul_w / 2, c + diameter * 0.04), "합격", font=f_hangul, fill=stamp_red)

    stamp = stamp.rotate(-14, expand=True, resample=Image.BICUBIC)
    img.paste(stamp, (int(center_x - stamp.width / 2), int(center_y - stamp.height / 2)), stamp)


def generate_pass_card(nickname, topic, concept_summary, date_str):
    """스마트폰 배경화면/인스타 스토리용 9:16 PASS 인증 카드를 생성해 PNG 바이트로 반환한다."""
    W, H = 1080, 1920
    bg_top = (235, 248, 246)      # 파스텔 민트
    bg_bottom = (238, 234, 250)   # 파스텔 라벤더
    grid_color = (214, 213, 231)
    ink = (42, 42, 58)
    muted = (110, 110, 130)
    accent = (58, 68, 154)        # 브랜드 인디고
    accent2 = (0, 161, 157)       # 브랜드 티얼

    img = Image.new("RGB", (W, H), bg_top)
    draw = ImageDraw.Draw(img)

    # 세로 그라디언트 배경
    for y in range(H):
        t = y / H
        draw.line(
            [(0, y), (W, y)],
            fill=tuple(int(bg_top[i] + (bg_bottom[i] - bg_top[i]) * t) for i in range(3)),
        )

    # 굿노트 감성 모눈종이 그리드
    step = 54
    for x in range(0, W + 1, step):
        draw.line([(x, 0), (x, H)], fill=grid_color, width=1)
    for y in range(0, H + 1, step):
        draw.line([(0, y), (W, y)], fill=grid_color, width=1)

    # 상단 헤더: 브랜드 라벨 + 닉네임 + 날짜
    label_font = _pass_font("Bold", 34)
    label = "오늘의 개념 PASS"
    label_w = draw.textlength(label, font=label_font)
    draw.text((W / 2 - label_w / 2, 92), label, font=label_font, fill=accent2)

    name_text = f"{nickname} 님"
    name_size = 66
    while name_size > 32:
        name_font = _pass_font("ExtraBold", name_size)
        if draw.textlength(name_text, font=name_font) <= W - 140:
            break
        name_size -= 4
    name_w = draw.textlength(name_text, font=name_font)
    draw.text((W / 2 - name_w / 2, 148), name_text, font=name_font, fill=accent)

    date_font = _pass_font("Medium", 32)
    date_w = draw.textlength(date_str, font=date_font)
    draw.text((W / 2 - date_w / 2, 232), date_str, font=date_font, fill=muted)

    # 중앙 노트 카드 (살짝 그림자 + 흰 패널)
    panel = [70, 320, W - 70, 1540]
    shadow = [panel[0] + 10, panel[1] + 16, panel[2] + 10, panel[3] + 16]
    draw.rounded_rectangle(shadow, radius=48, fill=(200, 203, 222))
    draw.rounded_rectangle(panel, radius=48, fill=(255, 255, 255))

    pad_x = 90
    content_w = (panel[2] - panel[0]) - pad_x * 2
    cx = (panel[0] + panel[2]) / 2
    y = panel[1] + 70

    tag_font = _pass_font("Bold", 30)
    tag = "통과한 단원"
    tag_w = draw.textlength(tag, font=tag_font)
    draw.text((cx - tag_w / 2, y), tag, font=tag_font, fill=accent2)
    y += 60

    topic_font, topic_lines = _fit_text(draw, topic, "ExtraBold", content_w, max_lines=2, start_size=78)
    y = _draw_centered_lines(draw, topic_lines, topic_font, cx, y, line_height=int(topic_font.size * 1.25), fill=accent)
    y += 36

    draw.line([(panel[0] + pad_x, y), (panel[2] - pad_x, y)], fill=(224, 226, 240), width=3)
    y += 44

    tag2 = "핵심 개념"
    tag2_w = draw.textlength(tag2, font=tag_font)
    draw.text((cx - tag2_w / 2, y), tag2, font=tag_font, fill=accent2)
    y += 58

    concept_font, concept_lines = _fit_text(
        draw, concept_summary, "Medium", content_w, max_lines=6, start_size=44, min_size=28
    )
    _draw_centered_lines(draw, concept_lines, concept_font, cx, y, line_height=int(concept_font.size * 1.45), fill=ink)

    # 하단부 도장 + 워터마크
    _draw_pass_stamp(draw, img, center_x=W - 230, center_y=panel[3] - 40, diameter=260)

    footer_font = _pass_font("Medium", 30)
    footer = "다혜T의 1:1 수학 PASS"
    footer_w = draw.textlength(footer, font=footer_font)
    draw.line([(W / 2 - 140, 1830), (W / 2 + 140, 1830)], fill=(200, 203, 222), width=2)
    draw.text((W / 2 - footer_w / 2, 1856), footer, font=footer_font, fill=muted)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "student_name" not in st.session_state:
    st.session_state.student_name = ""
if "student_grade" not in st.session_state:
    st.session_state.student_grade = "공통수학1"
if "slack_thread_ts" not in st.session_state:
    st.session_state.slack_thread_ts = None
if "kakao_id" not in st.session_state:
    st.session_state.kakao_id = None
if "kakao_login_error" not in st.session_state:
    st.session_state.kakao_login_error = None
if "kakao_login_error_detail" not in st.session_state:
    st.session_state.kakao_login_error_detail = None
if "clinic_messages" not in st.session_state:
    st.session_state.clinic_messages = []
if "review_messages" not in st.session_state:
    st.session_state.review_messages = []
if "clinic_last_upload_key" not in st.session_state:
    st.session_state.clinic_last_upload_key = None
if "review_last_upload_key" not in st.session_state:
    st.session_state.review_last_upload_key = None
if "clinic_archive_entry_id" not in st.session_state:
    st.session_state.clinic_archive_entry_id = None
if "review_archive_entry_id" not in st.session_state:
    st.session_state.review_archive_entry_id = None
if "upload_mode" not in st.session_state:
    st.session_state.upload_mode = None
if "question_archive" not in st.session_state:
    st.session_state.question_archive = []
if "pass_topic" not in st.session_state:
    st.session_state.pass_topic = None
if "pass_messages" not in st.session_state:
    st.session_state.pass_messages = []
if "pass_completed" not in st.session_state:
    st.session_state.pass_completed = False
if "pass_failed" not in st.session_state:
    st.session_state.pass_failed = False
if "pass_records" not in st.session_state:
    st.session_state.pass_records = []
if "pass_concept_summary" not in st.session_state:
    st.session_state.pass_concept_summary = ""
if "pass_completed_date" not in st.session_state:
    st.session_state.pass_completed_date = ""
if "pass_completed_time" not in st.session_state:
    st.session_state.pass_completed_time = ""
if "pass_grade" not in st.session_state:
    st.session_state.pass_grade = ""
if "teacher_mode" not in st.session_state:
    st.session_state.teacher_mode = False
if "selected_mode" not in st.session_state:
    st.session_state.selected_mode = MODE_OPTIONS[0]

# ==========================================
# 📱 QR코드로 바로 '💮 개념 PASS'로 직행
# 선생님 대시보드가 찍는 QR은 앱 주소 끝에 "?tab=pass"를 붙여서 만든다. 로그인이
# 안 된 학생은 이 상태로 로그인 게이트를 먼저 보고, 로그인 버튼이 get_kakao_login_url
# (state=selected_mode)로 이 선택을 카카오 왕복에 실어 보내므로 로그인 후에도 그대로
# PASS 탭에 떨어진다. 한 번 반영한 뒤 쿼리 파라미터를 지워야 - 안 지우면 다른 탭을
# 눌러도 다음 리런마다 계속 PASS 탭으로 되돌아가 버린다.
# ==========================================
if st.query_params.get("tab") == "pass":
    st.session_state.selected_mode = "💮 개념PASS"
    st.query_params.clear()

st.markdown(
    """
    <div style="
        background: linear-gradient(135deg, #EEF1FB, #E3F2F1);
        border: 1px solid rgba(58, 68, 154, 0.12);
        border-radius: 20px;
        padding: 1.3rem 1.5rem;
        margin-bottom: 1rem;
    ">
        <div style="font-size: 1.45rem; font-weight: 900; color: #3A449A; letter-spacing: -0.01em;">
            👩‍🏫 다혜T의 1:1 수학 클리닉
        </div>
        <div style="font-size: 0.82rem; font-weight: 600; color: #64748B; margin-top: 0.35rem; line-height: 1.5;">
            빈틈없는 오답 분석부터 완벽한 실전 대비까지, 나만의 맞춤 수학 솔루션
        </div>
        <div style="font-size: 0.86rem; color: #475569; margin-top: 0.7rem; line-height: 1.5;">
            안녕! 다혜T의 1:1 수학 클리닉에 온 걸 환영해 😊 선생님이랑 같이 너의 진짜 수학 실력을 만들어보자!
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

if not supabase:
    st.error(
        "⚠️ 데이터베이스(Supabase)가 아직 연결되지 않았습니다. "
        "SUPABASE_URL / SUPABASE_SERVICE_KEY를 Secrets에 설정하기 전까지는 "
        "로그인·질문·PASS 기록이 저장되지 않습니다. (선생님에게 문의)"
    )

# ==========================================
# 🔓 로그아웃 (사이드바, 학생 화면은 그대로 평범한 접힌 사이드바)
# "선생님 모드" 진입은 태블릿/모바일에서 사이드바를 열기 번거로워서
# '👤 마이페이지' 화면 맨 아래의 render_teacher_entry_point()로 옮겼다.
# (메인 화면에는 로그인 상태를 알리는 텍스트를 일절 띄우지 않는다)
# ==========================================
with st.sidebar:
    if st.session_state.authenticated:
        if st.button("🔓 로그아웃", use_container_width=True, key="sidebar_logout_btn"):
            persist_current_student()
            for key in [
                "authenticated", "student_name", "kakao_id",
                "clinic_messages", "review_messages", "clinic_last_upload_key", "review_last_upload_key",
                "clinic_archive_entry_id", "review_archive_entry_id", "question_archive",
                "slack_thread_ts", "pass_topic", "pass_messages", "pass_completed", "pass_failed",
                "pass_records", "pass_concept_summary", "pass_completed_date", "pass_completed_time",
                "pass_card_bytes", "pass_grade", "pass_fail_archive_entry_id", "upload_mode",
            ]:
                st.session_state.pop(key, None)
            st.rerun()

if st.session_state.teacher_mode:
    render_teacher_dashboard()
    st.stop()

# ==========================================
# 🔐 카카오 로그인 콜백 처리
# (인가 코드(code) → 액세스 토큰 → 닉네임/고유 회원번호 조회)
# 탭이 항상 먼저 보이는 구조라, 이 콜백 처리만은 로그인 여부와 무관하게 맨 위에서 돌아야 한다.
# ==========================================
if not st.session_state.authenticated and "code" in st.query_params:
    auth_code = st.query_params.get("code")
    returned_mode = st.query_params.get("state")
    st.query_params.clear()
    # 카카오가 그대로 돌려준 state로 로그인 전에 보고 있던 모드를 복원한다.
    # (st.tabs는 파이썬에서 활성 탭을 지정할 방법이 없어서, 모드 네비게이션을
    # st.radio + selected_mode로 구현해야 리다이렉트 왕복 후에도 복귀가 가능하다)
    if returned_mode in MODE_OPTIONS:
        st.session_state.selected_mode = returned_mode
    if not KAKAO_CLIENT_ID:
        st.session_state.kakao_login_error = "카카오 로그인 설정(KAKAO_CLIENT_ID)이 비어 있습니다. 선생님에게 문의해 주세요."
    else:
        try:
            kakao_id, nickname = exchange_kakao_code(auth_code)
            st.session_state.kakao_id = kakao_id
            st.session_state.student_name = nickname

            # 콴다처럼 과목/학년을 묻지 않고 카카오 인증만 끝나면 바로 들어간다 -
            # '처음 로그인인지'는 더 이상 student_grade 유무로 가리지 않는다(그 컬럼은
            # 실제 users 테이블에 없어 늘 비어 있다). users에 행이 있으면(saved) 그냥
            # 돌아온 학생으로 보고 과거 기록을 복원하고, 없으면 첫 로그인으로 보고
            # 빈 상태로 시작한다.
            saved = load_student_data(kakao_id)
            if saved:
                # 질문 클리닉/오답 첨삭 화면은 매번 백지로 시작한다 (콴다처럼) -
                # clinic_messages/review_messages는 일부러 복원하지 않는다. 지난 질문은
                # question_archive(= Supabase chat_history 테이블)에 그대로 남아 있어서
                # '👤 마이페이지 > 📝 오답노트'에서 언제든 다시 볼 수 있다.
                st.session_state.question_archive = saved.get("question_archive", [])
                st.session_state.pass_topic = saved.get("pass_topic")
                st.session_state.pass_messages = saved.get("pass_messages", [])
                st.session_state.pass_completed = saved.get("pass_completed", False)
                st.session_state.pass_failed = False
                st.session_state.pass_records = saved.get("pass_records", [])
                st.session_state.pass_concept_summary = saved.get("pass_concept_summary", "")
                st.session_state.pass_completed_date = saved.get("pass_completed_date", "")
                st.session_state.pass_completed_time = ""
                st.session_state.pass_grade = saved.get("pass_grade", "")
            else:
                # 첫 로그인 (이전 세션의 흔적이 섞이지 않게 초기화)
                st.session_state.clinic_messages = []
                st.session_state.review_messages = []
                st.session_state.question_archive = []
                st.session_state.pass_topic = None
                st.session_state.pass_messages = []
                st.session_state.pass_completed = False
                st.session_state.pass_failed = False
                st.session_state.pass_records = []
                st.session_state.pass_concept_summary = ""
                st.session_state.pass_completed_date = ""
                st.session_state.pass_completed_time = ""
                st.session_state.pass_grade = ""
            st.session_state.pop("pass_card_bytes", None)
            st.session_state.authenticated = True
        except Exception as e:
            st.session_state.kakao_login_error = "😥 카카오 로그인에 실패했어요. 잠시 후 다시 시도해 주세요."
            st.session_state.kakao_login_error_detail = str(e)
    st.rerun()

if not st.session_state.authenticated and "error" in st.query_params:
    st.query_params.clear()
    st.session_state.kakao_login_error = "카카오 로그인이 취소되었습니다."
    st.rerun()

def render_login_required_gate(feature_label):
    """로그인 전 상태에서 각 탭 내부에 뜨는 안내.

    카카오 로그인만 끝나면 과목/학년을 묻지 않고 바로 기능을 쓸 수 있다(콴다 스타일) -
    로그인 자체를 가로막는 별도의 '학년 선택' 단계는 없다. 탭 3개가 동시에 이 함수를
    호출할 수 있지만, 이 함수 자체는 더 이상 탭별로 분기할 위젯이 없다.
    """
    if st.session_state.kakao_login_error:
        st.error(st.session_state.kakao_login_error)
        if st.session_state.get("kakao_login_error_detail"):
            with st.expander("기술적인 오류 내용 보기"):
                st.code(st.session_state.kakao_login_error_detail)
        st.session_state.kakao_login_error = None
        st.session_state.kakao_login_error_detail = None

    st.markdown(
        f"""
        <div class="guide-box" style="text-align:center;">
            <div class="guide-title">🔒 로그인하고 같이 시작해볼까?</div>
            <div class="guide-item"><b>{feature_label}</b>은 로그인하면 바로 쓸 수 있어. 카카오로 편하게 로그인해줘 😊</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if KAKAO_CLIENT_ID:
        st.markdown(
            f'<a href="{get_kakao_login_url(state=st.session_state.selected_mode)}" class="kakao-login-btn">'
            f'<span class="kakao-login-btn-icon">💬</span>카카오 로그인</a>',
            unsafe_allow_html=True,
        )
    else:
        st.warning("카카오 로그인이 아직 설정되지 않았습니다. 선생님에게 문의해 주세요.")


def preprocess_handwriting_photo(image_bytes, max_width=1600):
    """스마트폰으로 찍은 손글씨 풀이 사진을 AI가 더 또렷하게 읽을 수 있게 전처리한다.

    - ImageOps.exif_transpose로 EXIF Orientation 태그를 읽어 회전을 자동 보정한다
      (세로로 찍은 사진이 가로로 눕혀져 업로드되는 흔한 문제 방지).
    - 흑백 변환 + ImageOps.autocontrast(cutoff=2)로 연필선은 진하게, 종이 바탕(그림자 포함)은
      밝게 끌어올려 대비를 극대화한다.
    - 가로가 max_width(기본 1600px)를 넘으면 비율을 유지한 채 줄여 API 응답 지연을 줄인다.
    """
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    img = img.convert("L")
    img = ImageOps.autocontrast(img, cutoff=2)
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, max(1, round(img.height * ratio))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


HANDWRITING_TRANSCRIBE_SYSTEM_PROMPT = (
    "너는 손글씨 수식 전사 전문가다. 문제를 절대 풀거나 평가하지 마라. 사진 속 손글씨 풀이 "
    "과정을 위에서부터 아래로 순서대로 읽고, 각 줄을 순수한 LaTeX 수식 텍스트로만 변환해라. "
    "각 수식은 Streamlit 마크다운에서 바로 렌더링되도록 '$'로 감싸라. "
    "(형식: [1줄] $수식$\n[2줄] $수식$\n...)"
)


def transcribe_handwriting(image_bytes, media_type):
    """1단계(전사): 전처리된 손글씨 풀이 사진을 줄별 LaTeX 텍스트로만 전사한다 - 풀이/채점은 하지 않는다."""
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL_NAME,
        max_tokens=1500,
        system=HANDWRITING_TRANSCRIBE_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(image_bytes).decode("utf-8"),
                    },
                },
                {"type": "text", "text": "이 사진 속 손글씨 풀이를 위에서부터 줄별로 LaTeX로만 전사해줘."},
            ],
        }],
    )
    return get_response_text(response)


OCR_SANITY_CHECK_SYSTEM_PROMPT = (
    "너는 수학 수식 교정 전문가다. Mathpix OCR로 인식된 수식(LaTeX)과 문제 그림을 대조하여 오독된 "
    "기호나 숫자를 교정하라.\n"
    "- 직사각형/원 접선 그림에서 CE=3, FE=x 로 표시되어 있다면, 빗변 CF는 x+3 이어야 한다.\n"
    "- 만약 OCR 결과에 (x+4)^2 처럼 주변 숫자 '4'의 간섭으로 3이 4로 잘못 인식된 패턴이 보인다면, "
    "기하학적 참값인 (x+3)^2 으로 즉각 정정하라.\n"
    "- 문제 그림에 나온 조건(길이, 각도 등)과 명백히 모순되는 숫자/기호만 교정하고, 확신이 없는 "
    "부분은 원문 그대로 둬라.\n"
    "- 오독 교정된 최종 LaTeX 텍스트만 출력하라. 그 외 설명이나 머리말을 절대 덧붙이지 마라."
)


def sanity_check_ocr_text(transcribed_text, problem_image_bytes, problem_media_type):
    """2단계 검증 전 '기하 맥락 기반 OCR 사후 교정' - Mathpix/Vision이 전사한 텍스트를 문제
    사진(도형 조건 등)과 대조해 숫자/기호 오독을 1회 바로잡는다.

    이 단계는 보정 보너스일 뿐이다 - 실패하면 교정 없이 원문을 그대로 돌려줘서, 이 호출 하나가
    실패했다고 핵심 파이프라인(전사 → Quick-Edit → 검증) 전체가 막히면 안 된다.
    """
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL_NAME,
            max_tokens=1200,
            system=OCR_SANITY_CHECK_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": problem_media_type,
                            "data": base64.b64encode(problem_image_bytes).decode("utf-8"),
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "다음은 OCR로 인식된 손글씨 풀이(줄별 LaTeX)다:\n\n"
                            f"{transcribed_text}\n\n"
                            "위 문제 그림과 대조해서 오독된 숫자/기호가 있다면 교정한 최종 LaTeX 텍스트만 출력해줘."
                        ),
                    },
                ],
            }],
        )
        corrected = get_response_text(response)
        return corrected or transcribed_text
    except Exception:
        return transcribed_text


def _extract_latex_lines(transcribed_text):
    """전사 결과 문자열에서 줄마다 LaTeX 알짹이만 뽑아 st.latex 렌더링용 리스트로 돌려준다.

    Vision LLM 전사는 "[1줄] $수식$" 형식이고 Mathpix 전사는 줄마다 "$수식$"/"$$수식$$"만
    오므로, 두 형식 모두에서 '[n줄]' 라벨과 '$' 구분자를 벗겨낸다.
    """
    lines = []
    for raw_line in (transcribed_text or "").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        raw_line = re.sub(r"^\[\d+\s*줄\]\s*", "", raw_line)
        raw_line = raw_line.strip("$").strip()
        if raw_line:
            lines.append(raw_line)
    return lines


HANDWRITING_VERIFY_SYSTEM_PROMPT = (
    "너는 계산을 수행하는 모델이 아니라, 주어진 '정석 해설'을 기준으로 학생의 풀이를 채점하는 "
    "대조(Diff) 알고리즘이다. 절대 네 스스로 문제를 풀거나 식을 새로 계산하지 마라.\n\n"
    "★ [검증 규칙]:\n"
    "1. 학생의 각 줄(Line)이 정석 해설의 어느 단계에 해당하는지 매칭하라.\n"
    "2. 정석 해설의 단계별 수식과 학생 풀이 간에 차이점(누락된 항, 부호 오류)이 발견된 최초의 줄을 "
    "특정하라.\n"
    "3. 정석 해설을 그대로 노출하지 말고, 학생이 오류를 범한 단계의 '핵심 개념'을 떠올리도록 1줄짜리 "
    "소크라테스 역질문을 작성하라.\n"
    "4. 절대 네가 대신 식을 전개하거나 계산한 결과를 적어서 보여주지 마라 - 오류가 발생한 줄과 개념만 "
    "짚어라.\n\n"
    "★ [🚨 식의 '전제'와 '전개 과정'을 구분해라 - 맞는 식을 틀렸다고 억지 부리지 마라]:\n"
    "- 학생의 어떤 줄이 정석 해설의 한 단계와 수학적으로 동등하다면(표현이 다르더라도) 그 줄 자체를 "
    "의심하지 마라. 차이는 '그 줄에서 다음 줄로 넘어가는 전개/계산 과정'에서만 찾아라.\n\n"
    "★ [🚨 수식 서식 - 한 줄로 뭉개서 쓰지 마라]:\n"
    "- 수식을 인라인($...$)으로 한 줄에 연달아 나열하지 마라. 짧게 하나만 인용할 때만 인라인 $...$를 써라.\n\n"
    "★ [출력 형식 - 반드시 이 2줄 형식 그대로, 다른 설명 없이]:\n"
    "DIAGNOSIS: (정석 해설의 몇 번째 단계에 해당하는 줄에서 어떤 차이가 발견됐는지 1줄로 요약. 차이가 "
    "없으면 어디까지 정석 해설과 일치하는지 1줄로 요약)\n"
    "HINT: (정석 해설/정답을 그대로 노출하지 않는 소크라테스식 역질문 1줄. 예: '2번째 줄에서 $(x+3)^2$을 "
    "전개할 때 앞뒤 제곱 사이에 빠진 항이 없는지 확인해 볼까?'처럼 1개의 핵심 유도 질문만. 수식은 "
    "'$...$'로 표기)"
)

# verify_transcribed_solution()의 reference_solution 기본값 - 아직 문제 은행 DB가 없어 임시로
# 박아둔 하드코딩 참조값이다. 추후에는 문제 사진을 DB의 문제 은행과 매칭해 그 문제의 정식
# 해설로 교체하면 된다(이 상수 대신 매칭된 해설 문자열을 reference_solution으로 넘기면 됨).
DEFAULT_REFERENCE_SOLUTION = (
    "1단계: 기하/접선의 성질 도출 (CE=3, FE=x)\n"
    "2단계: 피타고라스 식 수립 ((x+3)^2 = 4^2 + (5-x)^2)\n"
    "3단계: 완전제곱식 전개 (x^2+6x+9 = 16+25-10x+x^2)\n"
    "4단계: 동류항 정리 및 해답 도출 (16x=32, x=2)"
)


def verify_transcribed_solution(transcribed_text, student_grade, reference_solution=DEFAULT_REFERENCE_SOLUTION):
    """2단계(검증): 전사 결과(줄별 LaTeX)를 '정석 해설(reference_solution)' 기준으로 대조(Diff)해서
    학생이 처음 벗어난 줄을 찾아낸다.

    모델이 스스로 문제를 계산해서 교육과정을 벗어나거나 환각(틀린 전개)을 일으키는 것을 막기 위해,
    역할을 "스스로 풀기"가 아니라 "주어진 해설과 다른 점 찾기"로 제한한다. reference_solution은
    지금은 DEFAULT_REFERENCE_SOLUTION 하드코딩 값이지만, 추후 문제 은행 DB와 매칭한 해설 문자열을
    그대로 넘기도록 확장할 수 있게 파라미터로 분리해 뒀다.
    """
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL_NAME,
        max_tokens=1200,
        system=HANDWRITING_VERIFY_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"[과목: {student_grade}]\n\n"
                f"[정석 해설 (Reference Solution)]\n{reference_solution}\n\n"
                f"[학생 풀이 (Transcribed LaTeX)]\n{transcribed_text}\n\n"
                "학생의 각 줄이 정석 해설의 어느 단계에 해당하는지 매칭하고, 처음 벗어난 줄을 찾아줘."
            ),
        }],
    )
    return get_response_text(response)


def format_reference_solution(problem_row):
    """problem_solutions 테이블 행(reference_steps/pitfalls, jsonb)을 verify_transcribed_solution()의
    reference_solution 문자열 형식(DEFAULT_REFERENCE_SOLUTION과 동일한 'N단계: 제목 (내용)' 줄글)으로
    펼친다."""
    lines = []
    for step in (problem_row.get("reference_steps") or []):
        if isinstance(step, dict):
            step_no = step.get("step", "")
            title = step.get("title", "")
            content = step.get("content", "")
            lines.append(f"{step_no}단계: {title} ({content})" if title else f"{step_no}단계: {content}")
        else:
            lines.append(str(step))

    pitfalls = problem_row.get("pitfalls") or []
    if pitfalls:
        lines.append("")
        lines.append("[자주 틀리는 함정]")
        for pitfall in pitfalls:
            lines.append(f"- {pitfall}")

    return "\n".join(lines)


PROBLEM_CODE_DETECT_SYSTEM_PROMPT = (
    "너는 문제집 사진에서 인쇄된 문제 번호만 정확히 읽어내는 전문가다. 사진 속에서 이 문제의 "
    "번호(보통 3~4자리 숫자, 예: 0353)를 찾아 숫자만 출력하라. 번호를 찾을 수 없으면 'NONE'이라고만 "
    "출력하라. 다른 설명은 절대 덧붙이지 마라."
)


def detect_problem_code(image_bytes, media_type):
    """'문제집에 바로 풀었어요'(1장 모드)에서 사진 속 인쇄된 문제 번호를 Vision으로 감지한다.

    실패하거나 번호를 못 찾으면 빈 문자열을 돌려준다 - 호출하는 쪽이 DB 매칭 없이
    기존 자유형식 첨삭으로 조용히 전환해야 한다. 이 감지 자체가 핵심 첨삭 기능을
    막으면 안 되므로 예외를 전부 삼킨다.
    """
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL_NAME,
            max_tokens=50,
            system=PROBLEM_CODE_DETECT_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(image_bytes).decode("utf-8"),
                        },
                    },
                    {"type": "text", "text": "이 사진 속 인쇄된 문제 번호를 찾아줘."},
                ],
            }],
        )
        raw_code = get_response_text(response).strip()
        return re.sub(r"[^0-9]", "", raw_code)  # 'NONE'/잡음을 지우고 숫자만 남긴다
    except Exception:
        return ""


def verify_image_against_reference(image_bytes, media_type, reference_solution, student_grade):
    """1장 모드 전용 대조(Diff) 검증: 별도 OCR 단계 없이 사진 속 손글씨 풀이를 Vision이
    직접 읽으면서 동시에 '정석 해설(reference_solution)'과 대조한다.

    HANDWRITING_VERIFY_SYSTEM_PROMPT(텍스트 전용)와 같은 채점 규칙/출력 형식을 쓰지만,
    입력이 OCR된 텍스트가 아니라 이미지라는 점만 다르다 - 그래서 시스템 프롬프트 맨 앞에
    "사진 속 손글씨를 먼저 읽어라"는 지시만 추가한 변형판을 쓴다.
    """
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL_NAME,
        max_tokens=1200,
        system=(
            "너는 계산을 수행하는 모델이 아니라, 주어진 '정석 해설'을 기준으로 사진 속 학생의 "
            "손글씨 풀이를 채점하는 대조(Diff) 알고리즘이다. 절대 네 스스로 문제를 풀거나 식을 "
            "새로 계산하지 마라.\n\n"
            "★ [검증 규칙]:\n"
            "1. 사진 속 학생의 풀이를 위에서부터 줄 단위로 읽어라.\n"
            "2. 학생의 각 줄이 정석 해설의 어느 단계에 해당하는지 매칭하라.\n"
            "3. 정석 해설의 단계별 수식과 학생 풀이 간에 차이점(누락된 항, 부호 오류)이 발견된 "
            "최초의 줄을 특정하라.\n"
            "4. 정석 해설을 그대로 노출하지 말고, 학생이 오류를 범한 단계의 '핵심 개념'을 "
            "떠올리도록 1줄짜리 소크라테스 역질문을 작성하라.\n"
            "5. 절대 네가 대신 식을 전개하거나 계산한 결과를 적어서 보여주지 마라 - 오류가 발생한 "
            "줄과 개념만 짚어라.\n\n"
            "★ [🚨 식의 '전제'와 '전개 과정'을 구분해라 - 맞는 식을 틀렸다고 억지 부리지 마라]:\n"
            "- 학생의 어떤 줄이 정석 해설의 한 단계와 수학적으로 동등하다면(표현이 다르더라도) "
            "그 줄 자체를 의심하지 마라. 차이는 '그 줄에서 다음 줄로 넘어가는 전개/계산 과정'에서만 "
            "찾아라.\n\n"
            "★ [🚨 수식 서식 - 한 줄로 뭉개서 쓰지 마라]:\n"
            "- 수식을 인라인($...$)으로 한 줄에 연달아 나열하지 마라. 짧게 하나만 인용할 때만 "
            "인라인 $...$를 써라.\n\n"
            "★ [출력 형식 - 반드시 이 2줄 형식 그대로, 다른 설명 없이]:\n"
            "DIAGNOSIS: (정석 해설의 몇 번째 단계에 해당하는 줄에서 어떤 차이가 발견됐는지 1줄로 "
            "요약. 차이가 없으면 어디까지 정석 해설과 일치하는지 1줄로 요약)\n"
            "HINT: (정석 해설/정답을 그대로 노출하지 않는 소크라테스식 역질문 1줄. 수식은 "
            "'$...$'로 표기)"
        ),
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(image_bytes).decode("utf-8"),
                    },
                },
                {
                    "type": "text",
                    "text": (
                        f"[과목: {student_grade}]\n\n"
                        f"[정석 해설 (Reference Solution)]\n{reference_solution}\n\n"
                        "위 정석 해설을 기준으로 사진 속 학생 풀이를 처음부터 대조 검증해줘."
                    ),
                },
            ],
        }],
    )
    return get_response_text(response)


def parse_handwriting_diagnosis(raw_text):
    """verify_transcribed_solution()의 'DIAGNOSIS: .../HINT: ...' 2줄 출력을 분리한다.

    돌려주는 값: (diagnosis, hint, parsed_ok). 모델이 형식을 못 지켜 두 라벨을 전혀
    못 찾으면 parsed_ok=False를 돌려주고, 호출하는 쪽이 원문 전체를 그대로 화면에
    보여줄 수 있도록 diagnosis에 원문 전체를 담아준다 - 화면이 빈칸으로 끝나지 않게.
    """
    diagnosis, hint = "", ""
    for line in (raw_text or "").splitlines():
        line = line.strip()
        if line.upper().startswith("DIAGNOSIS:"):
            diagnosis = line.split(":", 1)[1].strip()
        elif line.upper().startswith("HINT:"):
            hint = line.split(":", 1)[1].strip()
    parsed_ok = bool(diagnosis or hint)
    if not parsed_ok:
        diagnosis = (raw_text or "").strip()
    return diagnosis, hint, parsed_ok


def run_handwriting_ocr_stage(
    solution_bytes, problem_bytes, problem_media_type, images_submitted, label, api_text, draft_key,
    book_title="", problem_code="",
):
    """1단계(전사) + 기하 맥락 사후 교정을 수행하고, 결과를 Quick-Edit 화면용 draft_key에
    스테이시한다 - 2단계(검증)는 학생이 Quick-Edit에서 확인/수정 후 확정해야 진행된다.

    전사(Mathpix 우선 → Vision LLM 폴백) 자체가 실패하면(예외) 화면에 에러만 띄우고
    draft를 만들지 않는다 - 호출한 쪽(크롭 화면 또는 업로드 직후)이 그대로 유지된다.
    """
    try:
        with st.spinner("전문 수식 엔진이 손글씨를 LaTeX로 판독 중입니다..."):
            preprocessed_bytes = preprocess_handwriting_photo(solution_bytes)
            mathpix_ok, mathpix_result, mathpix_err = recognize_handwriting_latex(preprocessed_bytes)

        # Mathpix 호출 결과는 성공/실패 어느 쪽이든 조용히 넘기지 않고 뱃지로 띄운다
        # (디버깅 목적 - 왜 Vision LLM 폴백으로 넘어갔는지 바로 보여야 한다).
        if mathpix_ok:
            confidence = mathpix_result.get("confidence")
            confidence_label = (
                f" (신뢰도: {round(confidence * 100)}%)" if isinstance(confidence, (int, float)) else ""
            )
            ocr_badge = ("success", f"✅ Mathpix 수식 엔진 작동 완료{confidence_label}")
            raw_transcribed = mathpix_result.get("text") or "\n".join(mathpix_result.get("latex_items", []))
        else:
            ocr_badge = ("error", f"❌ Mathpix 호출 실패: {mathpix_err}")
            raw_transcribed = transcribe_handwriting(preprocessed_bytes, "image/jpeg")

        # 기하 맥락 기반 OCR 사후 교정(Sanity Check) - 문제 그림과 대조해 숫자/기호
        # 오독(예: 3→4)을 2단계 검증으로 넘어가기 전에 한 번 바로잡는다.
        with st.spinner("기하 맥락으로 숫자/기호 오독을 교정하는 중입니다..."):
            sanity_checked = sanity_check_ocr_text(raw_transcribed, problem_bytes, problem_media_type)

        st.session_state[draft_key] = {
            "images_submitted": images_submitted,
            "label": label,
            "api_text": api_text,
            "original_text": raw_transcribed,
            "edited_text": sanity_checked,
            "ocr_badge": ocr_badge,
            "sanity_corrected": sanity_checked.strip() != raw_transcribed.strip(),
            "solution_image_bytes": solution_bytes,
            "book_title": book_title,
            "problem_code": problem_code,
        }
        st.rerun()
    except Exception as e:
        show_friendly_error(e)


def render_clinic_style_tab(feature_label, mode, state_prefix, with_solution_uploader, caption_text):
    """'❓ 질문 클리닉'과 '🔍 오답 첨삭' 탭이 공유하는 렌더러.

    state_prefix로 세션 상태 키(clinic_* / review_*)를 분리해서, 두 탭이 Streamlit
    특성상 매 스크립트 실행마다 동시에 렌더링되어도 서로의 대화 기록·업로드 상태를
    덮어쓰지 않게 한다.
    """
    st.markdown(f"### {feature_label}")

    if not st.session_state.authenticated:
        render_login_required_gate(feature_label)
        return

    st.caption(caption_text)

    # log_student_question()의 DB 저장 결과(디버그 카드) - st.rerun() 이후에도 사라지지
    # 않도록 세션 상태에 저장된 값을 매번 다시 그린다. 로깅은 오답 첨삭에서만 일어나므로
    # 질문 클리닉 탭에는 띄우지 않는다.
    if with_solution_uploader:
        render_last_save_status_debug_card()

    messages_key = f"{state_prefix}_messages"
    upload_key_key = f"{state_prefix}_last_upload_key"
    system_prompt = load_system_prompt(st.session_state.student_grade)

    uploaded_solution = None
    uploaded_problem = None
    if with_solution_uploader:
        if st.session_state.upload_mode is None:
            # View A: 방식 선택 화면 - 모바일 앱처럼 큰 버튼 2개 중 하나를 고르게 한다.
            st.markdown("##### 어떻게 풀었나요? 방식을 선택해 주세요👇")
            mode_c1, mode_c2 = st.columns(2)
            with mode_c1:
                if st.button(
                    "📖 문제집에 바로 풀었어요", use_container_width=True, key=f"{state_prefix}_mode_single_btn"
                ):
                    st.session_state.upload_mode = "single"
                    st.rerun()
            with mode_c2:
                if st.button(
                    "📝 연습장에 따로 풀었어요", use_container_width=True, key=f"{state_prefix}_mode_dual_btn"
                ):
                    st.session_state.upload_mode = "dual"
                    st.rerun()
        else:
            # View B: 선택한 방식에 맞는 업로드 화면.
            if st.button("🔙 다른 방식으로 선택하기", key=f"{state_prefix}_mode_back_btn"):
                st.session_state.upload_mode = None
                st.rerun()

            st.caption("💡 tip: 연필선이 선명할수록, 그림자가 지지 않게 수직으로 찍을수록 AI가 더 정확하게 읽어요!")

            # 등록된 정석 해설 DB와 대조 검증을 하려면 어느 교재인지 알아야 한다 -
            # 일치하는 문제가 없으면 조용히 기존 자유형식/하이브리드 추론 방식으로
            # 전환된다(verify_transcribed_solution/get_problem_solution 참고).
            registered_book_titles = sorted({
                row.get("book_title") for row in list_registered_problems() if row.get("book_title")
            })
            if "쎈 중3-2" not in registered_book_titles:
                registered_book_titles.insert(0, "쎈 중3-2")

            if st.session_state.upload_mode == "dual":
                pick_c1, pick_c2 = st.columns(2)
                with pick_c1:
                    selected_book_title = st.selectbox(
                        "📖 교재", registered_book_titles, key=f"{state_prefix}_book_title_select"
                    )
                with pick_c2:
                    selected_problem_code = st.text_input(
                        "🔢 문제 번호", value="0353", key=f"{state_prefix}_problem_code_input"
                    )

                up_c1, up_c2 = st.columns(2)
                with up_c1:
                    uploaded_problem = st.file_uploader(
                        "1️⃣ 문제 사진", type=["jpg", "jpeg", "png"], key=f"{state_prefix}_prob"
                    )
                with up_c2:
                    uploaded_solution = st.file_uploader(
                        "2️⃣ 연습장 풀이 사진", type=["jpg", "jpeg", "png"], key=f"{state_prefix}_sol"
                    )
            else:
                # 1장 모드는 문제 번호를 직접 입력받지 않는다 - 사진 속 인쇄된 번호를
                # Vision으로 자동 감지해서 DB와 매칭한다(아래 detect_problem_code 참고).
                selected_book_title = st.selectbox(
                    "📖 교재", registered_book_titles, key=f"{state_prefix}_book_title_select_single"
                )
                uploaded_problem = st.file_uploader(
                    "📷 문제집에 바로 풀어놓은 사진", type=["jpg", "jpeg", "png"], key=f"{state_prefix}_prob"
                )
    else:
        uploaded_problem = st.file_uploader(
            "풀다가 막힌 문제 사진을 올려주세요!",
            type=["jpg", "jpeg", "png"],
            key=f"{state_prefix}_prob",
        )

    prob_id = uploaded_problem.file_id if uploaded_problem else "none"
    sol_id = uploaded_solution.file_id if uploaded_solution else "none"
    curr_upload_key = f"{prob_id}_{sol_id}"

    # 손글씨 OCR 파이프라인의 임시 상태 - with_solution_uploader(오답 첨삭) + dual 모드에서만
    # 쓰인다. crop_key: 전사 전 수식 영역을 크롭할 때까지 대기 / draft_key: 전사 결과를
    # Quick-Edit으로 확인/수정한 뒤 확정할 때까지 대기.
    crop_key = f"{state_prefix}_crop_pending"
    draft_key = f"{state_prefix}_ocr_draft"
    ocr_edit_widget_key = f"{state_prefix}_ocr_edit_area"

    if api_key and uploaded_problem and curr_upload_key != st.session_state.get(upload_key_key):
        st.session_state[upload_key_key] = curr_upload_key
        st.session_state[messages_key] = []
        # 새 사진 = 새 질문 → 오답노트에도 새 카드를 만든다 (이전 카드를 계속 수정하지 않도록 끊어준다).
        st.session_state[f"{state_prefix}_archive_entry_id"] = None
        # 이전 사진의 크롭 대기/Quick-Edit draft/위젯 값이 새 사진에 그대로 남아 보이지
        # 않도록 지운다.
        st.session_state.pop(crop_key, None)
        st.session_state.pop(draft_key, None)
        st.session_state.pop(ocr_edit_widget_key, None)

        p_bytes = uploaded_problem.getvalue()

        if with_solution_uploader and uploaded_solution:
            # 손글씨 풀이 사진이 함께 올라온 경우: 1단계(전사) → 2단계(논리 검증)
            # 2단계 파이프라인으로 처리한다. 둘 다 Vision으로 "읽고 바로 채점"하던
            # 예전 방식은 전사 오류와 채점 오류가 뒤섞여 틀린 줄을 못 짚는 경우가 있었다.
            send_telegram_photo(p_bytes, st.session_state.student_name, f"{mode} - 문제")
            s_bytes = uploaded_solution.getvalue()
            send_telegram_photo(s_bytes, st.session_state.student_name, f"{mode} - 풀이")

            images_submitted = [
                {"bytes": p_bytes, "mime": uploaded_problem.type},
                {"bytes": s_bytes, "mime": uploaded_solution.type},
            ]
            label = "📷 문제 + 풀이 사진을 올렸어요!"
            api_text = (
                f"[모드: {mode} / 과목: {st.session_state.student_grade}] 문제 사진과 연습장 손글씨 풀이 사진 2장이 "
                "전달되었어. 첫 줄부터 줄 단위로 연산을 검증해서 잘한 점과 부호/계산 실수가 발생한 정확한 위치를 "
                "다정하게 지적해줘."
            )

            if st_cropper is not None:
                # 수식 포커스 크롭이 가능하면 전사(Mathpix)로 바로 넘기지 않고, 학생이
                # 작은 손글씨 영역만 정확히 잘라낼 기회를 먼저 준다 (아래 crop_key elif 블록).
                st.session_state[crop_key] = {
                    "problem_bytes": p_bytes,
                    "problem_media_type": uploaded_problem.type,
                    "solution_bytes": s_bytes,
                    "images_submitted": images_submitted,
                    "label": label,
                    "api_text": api_text,
                    "book_title": selected_book_title,
                    "problem_code": selected_problem_code,
                }
                st.rerun()
            else:
                # streamlit-cropper가 설치되지 않은 환경 - 크롭 단계 없이 원본 그대로 전사한다.
                run_handwriting_ocr_stage(
                    s_bytes, p_bytes, uploaded_problem.type, images_submitted, label, api_text, draft_key,
                    book_title=selected_book_title, problem_code=selected_problem_code,
                )
        elif with_solution_uploader:
            # "📖 문제집에 바로 풀었어요"(1장 모드) - 사진 속 인쇄된 문제 번호를 먼저
            # Vision으로 감지해서, DB(problem_solutions)에 등록된 정석 해설이 있으면
            # 그걸 '바이블'로 대조 검증하고, 없으면 기존 자유형식 Vision 첨삭으로
            # 조용히 전환한다.
            send_telegram_photo(p_bytes, st.session_state.student_name, f"{mode} - 문제")

            with st.spinner("사진 속 문제 번호를 확인하는 중입니다..."):
                detected_code = detect_problem_code(p_bytes, uploaded_problem.type)
            db_problem = get_problem_solution(selected_book_title, detected_code) if detected_code else None

            images_submitted = [{"bytes": p_bytes, "mime": uploaded_problem.type}]
            label = "📷 문제 사진을 올렸어요!"

            try:
                if db_problem:
                    st.caption(f"📚 문제 {detected_code}번 - 등록된 정석 해설({selected_book_title})과 대조했어요.")
                    reference_solution = format_reference_solution(db_problem)
                    api_text = (
                        f"[모드: {mode} / 과목: {st.session_state.student_grade}] 등록된 정석 해설과 대조해서 "
                        f"문제 {detected_code}번 손글씨 풀이를 검증했어."
                    )
                    with st.spinner("등록된 정석 해설과 대조하며 풀이를 검증하는 중입니다..."):
                        raw_diagnosis = verify_image_against_reference(
                            p_bytes, uploaded_problem.type, reference_solution, st.session_state.student_grade,
                        )
                    diagnosis, hint, parsed_ok = parse_handwriting_diagnosis(raw_diagnosis)
                    if not parsed_ok:
                        bot_reply = diagnosis or "(빈 응답)"
                    else:
                        bot_reply = f"🔍 **진단 결과:** {diagnosis}"
                        if hint:
                            bot_reply += f"\n\n💡 **다혜T의 역질문 힌트:** {hint}"
                    log_label = "풀이 검토 사진 제출(DB 대조)"
                else:
                    # 번호를 못 찾았거나(detected_code == "") DB에 등록된 해설이 없는 경우 -
                    # 기존처럼 단일 Vision 호출로 자유형식 첨삭을 돌려준다.
                    api_text = (
                        f"[모드: {mode} / 과목: {st.session_state.student_grade}] 이 사진 안의 손글씨 풀이를 첫 줄부터 "
                        "줄 단위로 검증하여 잘한 점과 연산 실수가 발생한 정확한 위치를 콕 짚어라."
                    )
                    content_blocks = [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": uploaded_problem.type,
                                "data": base64.b64encode(p_bytes).decode("utf-8"),
                            },
                        },
                        {"type": "text", "text": api_text},
                    ]
                    with st.spinner("손글씨 풀이를 줄 단위로 분석하는 중입니다..."):
                        response = anthropic.Anthropic(api_key=api_key).messages.create(
                            model=MODEL_NAME,
                            max_tokens=2000,
                            system=system_prompt,
                            messages=[{"role": "user", "content": content_blocks}],
                        )
                    bot_reply = get_response_text(response)
                    log_label = "풀이 검토 사진 제출"

                # 감지된 문제 번호(DB 매칭 여부와 무관하게)를 그대로 저장해야 선생님
                # 대시보드에서 "몇 번 문제 첨삭 기록인지"가 바로 보인다.
                log_student_question(detected_code, p_bytes, "", bot_reply)

                # 사진 제출 자체도 대화 기록에 남겨서 새로고침 후에도 사진 내역이 보이게 한다.
                st.session_state[messages_key].append({
                    "role": "user",
                    "content": label,
                    "images": images_submitted,
                    "api_text": api_text,
                })
                st.session_state[messages_key].append({"role": "assistant", "content": bot_reply})
                log_to_slack_and_gsheet(
                    st.session_state.student_name, st.session_state.student_grade, mode, log_label, bot_reply,
                    skip_telegram=True,
                )
                upsert_question_archive_entry(state_prefix, feature_label, messages_key)
                persist_current_student()
                st.rerun()
            except Exception as e:
                show_friendly_error(e)
        else:
            # "❓ 질문 클리닉" - 문제 사진 1장만 받아 정답을 주지 않고 유도 질문을 던진다.
            send_telegram_photo(p_bytes, st.session_state.student_name, mode)
            api_text = (
                f"[모드: {mode} / 과목: {st.session_state.student_grade}] 먼저 1단계로 "
                "[과목 > 대단원 > 중단원 > 세부 대표유형명] 4단계로 세부 출제 유형을 밝히고, 인식한 조건이나 각도를 "
                "먼저 확인한 뒤 2단계 힌트 질문을 던져라."
            )
            content_blocks = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": uploaded_problem.type,
                        "data": base64.b64encode(p_bytes).decode("utf-8"),
                    },
                },
                {"type": "text", "text": api_text},
            ]
            images_submitted = [{"bytes": p_bytes, "mime": uploaded_problem.type}]
            label = "📷 문제 사진을 올렸어요!"

            with st.spinner("문제를 꼼꼼하게 살피는 중입니다..."):
                try:
                    response = anthropic.Anthropic(api_key=api_key).messages.create(
                        model=MODEL_NAME,
                        max_tokens=2000,
                        system=system_prompt,
                        messages=[{"role": "user", "content": content_blocks}],
                    )
                    bot_reply = get_response_text(response)
                    st.session_state[messages_key].append({
                        "role": "user",
                        "content": label,
                        "images": images_submitted,
                        "api_text": api_text,
                    })
                    st.session_state[messages_key].append({"role": "assistant", "content": bot_reply})
                    log_to_slack_and_gsheet(
                        st.session_state.student_name, st.session_state.student_grade, mode, "문제 사진 업로드 제출", bot_reply,
                        skip_telegram=True,
                    )
                    upsert_question_archive_entry(state_prefix, feature_label, messages_key)
                    persist_current_student()
                    st.rerun()
                except Exception as e:
                    show_friendly_error(e)

    elif st.session_state.get(crop_key):
        # ==========================================
        # ✂️ 수식 포커스 크롭: 전체 사진 안의 작은 손글씨 숫자(예: 3↔4)가 해상도 손실로
        # 오독되는 문제를 줄이기 위해, 전사 전에 수식 영역만 네모 박스로 잘라내게 한다.
        # ==========================================
        pending = st.session_state[crop_key]
        st.markdown("✂️ **손글씨 수식 영역만 네모 박스로 잘라주세요** (작게 쓴 숫자/기호일수록 더 정확하게 읽혀요!)")
        source_img = Image.open(io.BytesIO(pending["solution_bytes"]))
        source_img = ImageOps.exif_transpose(source_img)
        cropped_img = st_cropper(
            source_img,
            realtime_update=True,
            box_color="#3A449A",
            return_type="image",
            key=f"{state_prefix}_cropper",
        )
        st.image(cropped_img, caption="🔍 이 영역으로 판독할게요", width=320)

        crop_c1, crop_c2 = st.columns(2)
        with crop_c1:
            if st.button(
                "✅ 이 영역으로 판독 시작", use_container_width=True, type="primary",
                key=f"{state_prefix}_crop_confirm_btn",
            ):
                crop_buf = io.BytesIO()
                cropped_img.convert("RGB").save(crop_buf, format="JPEG", quality=95)
                st.session_state.pop(crop_key, None)
                run_handwriting_ocr_stage(
                    crop_buf.getvalue(), pending["problem_bytes"], pending["problem_media_type"],
                    pending["images_submitted"], pending["label"], pending["api_text"], draft_key,
                    book_title=pending.get("book_title", ""), problem_code=pending.get("problem_code", ""),
                )
        with crop_c2:
            if st.button(
                "⏭️ 자르지 않고 전체 사진 그대로", use_container_width=True,
                key=f"{state_prefix}_crop_skip_btn",
            ):
                st.session_state.pop(crop_key, None)
                run_handwriting_ocr_stage(
                    pending["solution_bytes"], pending["problem_bytes"], pending["problem_media_type"],
                    pending["images_submitted"], pending["label"], pending["api_text"], draft_key,
                    book_title=pending.get("book_title", ""), problem_code=pending.get("problem_code", ""),
                )

    elif st.session_state.get(draft_key):
        # ==========================================
        # 📝 수식 확인 및 원터치 수정(Quick-Edit): OCR이 끝났지만 아직 검증(2단계)
        # 전이다 - 3을 4로 오독하는 등의 OCR 실수가 그대로 검증 단계까지 흘러가면
        # 틀린 진단이 나오므로, 학생이 여기서 한 번 확인/수정하고 넘어가야 한다.
        # ==========================================
        draft = st.session_state[draft_key]
        badge_kind, badge_text = draft["ocr_badge"]
        if badge_kind == "success":
            st.success(badge_text)
        else:
            st.error(badge_text)
            st.caption("🤖 Vision AI로 대체 판독한 결과예요.")
        if draft.get("sanity_corrected"):
            st.info("🔧 문제 그림과 대조해서 오독된 숫자/기호를 자동으로 한 번 교정했어요.")

        st.markdown("**AI가 내 풀이를 이렇게 읽었어요! 잘못 읽힌 글자가 있다면 직접 수정한 뒤 첨삭을 시작하세요.**")
        edited_text = st.text_area(
            "📝 판독된 풀이 (수정 가능)",
            value=draft["edited_text"],
            key=ocr_edit_widget_key,
            height=200,
        )
        draft["edited_text"] = edited_text

        st.markdown("🔎 **실시간 미리보기**")
        preview_lines = _extract_latex_lines(edited_text)
        if preview_lines:
            for preview_line in preview_lines:
                st.latex(preview_line)
        else:
            st.caption("(미리볼 수식이 없어요 - 위 텍스트를 LaTeX 형식으로 적어주세요)")

        if st.button(
            "✨ 이 수식으로 다혜T 첨삭 시작",
            use_container_width=True,
            type="primary",
            key=f"{state_prefix}_ocr_confirm_btn",
        ):
            try:
                # DB에 등록된 정석 해설이 있으면 그걸 '바이블'로 대조 검증하고, 없으면
                # verify_transcribed_solution()의 기본값(DEFAULT_REFERENCE_SOLUTION)으로
                # 조용히 전환된다.
                db_problem = get_problem_solution(draft.get("book_title", ""), draft.get("problem_code", ""))
                verify_kwargs = {}
                if db_problem:
                    verify_kwargs["reference_solution"] = format_reference_solution(db_problem)
                    st.caption(
                        f"📚 등록된 정석 해설({draft.get('book_title')} {draft.get('problem_code')})과 대조했어요."
                    )
                with st.spinner("다혜T의 풀이 첨삭 및 힌트를 분석 중입니다..."):
                    raw_diagnosis = verify_transcribed_solution(
                        edited_text, st.session_state.student_grade, **verify_kwargs
                    )
                diagnosis, hint, parsed_ok = parse_handwriting_diagnosis(raw_diagnosis)

                if not parsed_ok:
                    feedback_text = diagnosis or "(빈 응답)"
                else:
                    feedback_text = f"🔍 **진단 결과:** {diagnosis}"
                    if hint:
                        feedback_text += f"\n\n💡 **다혜T의 역질문 힌트:** {hint}"
                bot_reply = f"📝 **판독된 풀이 과정**\n\n{edited_text}\n\n---\n\n{feedback_text}"

                # 최종 피드백(verify_transcribed_solution 결과)이 나온 직후, 선생님
                # 대시보드가 바로 훑어볼 수 있게 구조화된 로그(student_questions)를
                # 무조건 남긴다. 화면 표시(디버그 카드)는 log_student_question()이
                # st.session_state["last_save_status"]에 적어두고,
                # render_last_save_status_debug_card()가 다음 실행에서 그려준다.
                log_student_question(
                    draft.get("problem_code") or "", draft.get("solution_image_bytes"),
                    edited_text or "", feedback_text or "",
                )

                st.session_state[messages_key].append({
                    "role": "user",
                    "content": draft["label"],
                    "images": draft["images_submitted"],
                    "api_text": draft["api_text"],
                })
                st.session_state[messages_key].append({"role": "assistant", "content": bot_reply})
                log_to_slack_and_gsheet(
                    st.session_state.student_name, st.session_state.student_grade, mode, "풀이 검토 사진 제출", bot_reply,
                    skip_telegram=True,
                )
                upsert_question_archive_entry(state_prefix, feature_label, messages_key)

                # 학생이 OCR 원문을 실제로 고쳤을 때만 학습 데이터로 남긴다.
                if edited_text.strip() != draft["original_text"].strip():
                    log_ocr_correction(
                        draft["original_text"], edited_text, draft["solution_image_bytes"],
                        st.session_state.get("kakao_id"),
                    )

                persist_current_student()
                st.session_state.pop(draft_key, None)
                st.session_state.pop(ocr_edit_widget_key, None)
                st.rerun()
            except Exception as e:
                show_friendly_error(e)

    # ==========================================
    # 대화 기록: 접속할 때마다 백지로 시작한다 (콴다처럼) - 지난 질문은 여기 안 쌓이고
    # question_archive에 저장되어 '📁 나의 오답노트' 팝업에서만 다시 볼 수 있다.
    # 길어진 현재 대화만 오래된 턴은 접어서 최근 1턴만 바로 보이게 한다.
    # ==========================================
    history = st.session_state[messages_key]
    if not history:
        st.info("👋 아직 질문이 없어요. 사진을 올려서 새로운 질문을 시작해봐!")
    older, recent = (history[:-2], history[-2:]) if len(history) > 2 else ([], history)
    if older:
        with st.expander("📝 이전 질문/기록 보기", expanded=False):
            for msg in older:
                _render_clinic_message(msg)
    for msg in recent:
        _render_clinic_message(msg)

    # ==========================================
    # 🆘 학생 구출용 퀵 액션 버튼 (AI가 답변한 직후에만 노출)
    # ==========================================
    if len(history) > 1 and history[-1]["role"] == "assistant":
        st.markdown("👇 **어떻게 풀어야 할지 막막하다면 버튼을 눌러주세요!**")

        btn_col1, btn_col2, btn_col3 = st.columns(3)

        with btn_col1:
            if st.button("💡 다음 힌트만 살짝", use_container_width=True, key=f"{state_prefix}_hint_btn"):
                st.session_state[messages_key].append({"role": "user", "content": "다음 단계로 넘어가는 힌트만 살짝 알려줘."})
                st.rerun()

        with btn_col2:
            if st.button("🔓 80% 풀이 바로 보기", use_container_width=True, key=f"{state_prefix}_80_btn"):
                st.session_state[messages_key].append({"role": "user", "content": "진짜 모르겠어. 날 괴롭히지 말고 풀이의 80%를 시원하게 바로 보여줘!"})
                st.rerun()

        with btn_col3:
            if st.button("📝 유사문제 연습하기", use_container_width=True, key=f"{state_prefix}_similar_btn"):
                st.session_state[messages_key].append({"role": "user", "content": "이 개념을 쓰는 비슷한 다른 문제로 연습해보고 싶어."})
                st.rerun()

    # ==========================================
    # 💬 심플 대화 입력창 (카톡 스타일)
    # ==========================================
    if prompt := st.chat_input("다혜 쌤 질문에 답하거나 궁금한 점을 적어주세요...", key=f"{state_prefix}_chat_input"):
        st.session_state[messages_key].append({"role": "user", "content": prompt})
        st.rerun()

    # ==========================================
    # 🤖 마지막 메시지가 학생 답변이면 다혜 쌤 AI 응답 생성
    # ==========================================
    if st.session_state[messages_key] and st.session_state[messages_key][-1]["role"] == "user":
        if not api_key:
            st.error("API 키가 설정되지 않았습니다.")
            return

        client = anthropic.Anthropic(api_key=api_key)

        # 대화 기록(저장/복원된 기록 포함)을 그대로 API 메시지로 변환한다.
        # 사진이 달린 턴은 최초 업로드 위젯이 아니라 messages에 저장해 둔
        # 이미지/지시문을 사용하므로, 새로고침 후 복원된 세션에서도 동일하게 동작한다.
        api_messages = []
        for msg in st.session_state[messages_key]:
            if msg.get("images"):
                blocks = [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img.get("mime", "image/jpeg"),
                            "data": base64.b64encode(img["bytes"]).decode("utf-8"),
                        },
                    }
                    for img in msg["images"]
                ]
                blocks.append({"type": "text", "text": msg.get("api_text") or msg["content"]})
                api_messages.append({"role": msg["role"], "content": blocks})
            else:
                api_messages.append({"role": msg["role"], "content": msg["content"]})

        with st.chat_message("assistant"):
            with st.spinner("생각 중..."):
                try:
                    response = client.messages.create(
                        model=MODEL_NAME,
                        max_tokens=2000,
                        system=system_prompt,
                        messages=api_messages,
                    )
                    bot_reply = get_response_text(response)
                    render_assistant_content(bot_reply)
                    st.session_state[messages_key].append({"role": "assistant", "content": bot_reply})
                    last_student_text = st.session_state[messages_key][-2]["content"]
                    log_to_slack_and_gsheet(
                        st.session_state.student_name, st.session_state.student_grade, mode,
                        last_student_text, bot_reply,
                    )
                    upsert_question_archive_entry(state_prefix, feature_label, messages_key)

                    # '❓ 질문 클리닉'의 텍스트 답변도 '🔍 오답 첨삭'과 동일한
                    # student_questions 파이프라인에 남긴다 - 이미지가 없는 질문이므로
                    # image_bytes=None(Storage 업로드 생략), problem_code="일반질문"으로
                    # 구분한다. 오답 첨삭의 후속 힌트 대화(이 코드를 같이 쓴다)는 첨삭
                    # 시작 시점에 이미 한 번 기록되므로 여기서는 중복 기록하지 않는다.
                    if not with_solution_uploader:
                        log_student_question("일반질문", None, last_student_text, bot_reply)

                    persist_current_student()
                    st.rerun()
                except Exception as e:
                    show_friendly_error(e)


def render_question_clinic_tab():
    render_clinic_style_tab(
        feature_label="❓ 질문 클리닉",
        mode="❓ 질문 클리닉 (스스로 풀어보기)",
        state_prefix="clinic",
        with_solution_uploader=False,
        caption_text="문제 사진 1장을 올리면, 정답 대신 스스로 답을 찾아갈 수 있게 차근차근 질문을 던져줄게!",
    )


def render_mistake_review_tab():
    render_clinic_style_tab(
        feature_label="🔍 오답 첨삭",
        mode="🔍 오답 첨삭 (문제 + 연습장)",
        state_prefix="review",
        with_solution_uploader=True,
        caption_text="사진 1장 또는 2장(문제+연습장 풀이)을 올리면, 어느 줄에서 연산 실수가 났는지 콕 짚어줄게!",
    )


def _render_pass_message(msg):
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant":
            render_handwritten_note(msg["content"])
        else:
            st.markdown(msg["content"])


def render_pass_grade_picker(key_suffix, button_label="✅ 선택 완료"):
    """'개념 PASS'용 반/학년(pass_grade)을 고르고, 고르는 즉시 세션+DB에 저장한다.

    '💮 개념 PASS' 탭의 최초 1회 선택과 '👤 마이페이지'의 "학년/반 변경하기"가
    똑같은 선택 로직을 쓰므로 하나로 묶었다. 버튼을 눌러 저장이 끝나면 True를
    돌려줘서, 호출부가 st.rerun() 등 다음 동작을 알아서 결정하게 한다.
    """
    chosen = st.selectbox(
        "학년/반 선택", PASS_GRADE_LEVELS, key=f"grade_picker_{key_suffix}"
    )
    if st.button(
        button_label, key=f"grade_picker_btn_{key_suffix}", use_container_width=True, type="primary"
    ):
        st.session_state.pass_grade = chosen
        persist_current_student()
        return True
    return False


def render_pass_tab():
    st.markdown("### 💮 개념 PASS")

    if not st.session_state.authenticated:
        render_login_required_gate("💮 개념 PASS")
        return

    st.caption("다혜T가 오늘 정해준 범위 안에서, 핵심 개념이나 오개념을 콕 집어 물어볼게. 끝까지 같이 해보자!")

    st.markdown(
        f'<div class="pass-date-banner">'
        f'<span class="pass-date-text">📅 {get_today_kst_label()} - 오늘의 개념 미션</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")

    if not st.session_state.pass_topic:
        if not st.session_state.pass_grade:
            # ① 지연 선택(Lazy Selection) - 반/학년을 아직 고른 적이 없는 학생에게만,
            # 이 탭 안에서 딱 한 번 물어본다. 고르는 즉시 세션+DB에 저장되므로 다음부터는
            # (오늘이든 다른 날이든, 심지어 로그아웃 후 다시 로그인해도) 다시 묻지 않는다.
            st.markdown("##### 🙋 어느 반(학년) 미션을 볼까요?")
            st.caption("처음 한 번만 고르면 돼. 다음부터는 바로 그 반의 오늘 미션을 보여줄게.")
            if render_pass_grade_picker("pass_tab"):
                st.rerun()
            return

        # ② 이미 반(학년)이 정해져 있다 - 묻지 않고 바로 오늘의 미션을 보여준다.
        selected_grade = st.session_state.pass_grade
        today_scope_map = load_daily_pass_scope()
        today_scope = today_scope_map.get(selected_grade, "")

        st.caption(f"현재 반: **{selected_grade}** · 반을 바꾸려면 '👤 마이페이지'에서 변경할 수 있어요.")

        if today_scope:
            st.info(f"📌 **오늘의 미션: {selected_grade} - {today_scope}**")
        else:
            st.markdown(
                """
                <div style="
                    background: linear-gradient(135deg, #FFF9EC, #FFF3DC);
                    border: 1px solid rgba(255, 184, 77, 0.35);
                    border-radius: 18px;
                    padding: 1.7rem 1.4rem;
                    text-align: center;
                    margin: 0.8rem 0 1.2rem;
                ">
                    <div style="font-size: 2.2rem; margin-bottom: 0.5rem;">🌤️</div>
                    <div style="font-size: 1rem; color: #92661A; font-weight: 700; line-height: 1.7;">
                        오늘은 아직 등록된 미션이 없어!<br>
                        푹 쉬거나 마이페이지에서 이전 미션을 복습해 보자 👏
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        if st.button(
            "🚀 PASS 도전하기",
            key="pass_start_btn",
            use_container_width=True,
            type="primary",
            disabled=not today_scope,
        ):
            if not api_key:
                st.error("API 키가 설정되지 않았습니다.")
            else:
                client = anthropic.Anthropic(api_key=api_key)
                with st.spinner("질문을 준비하는 중..."):
                    try:
                        response = client.messages.create(
                            model=MODEL_NAME,
                            max_tokens=600,
                            system=load_pass_system_prompt(selected_grade, today_scope),
                            messages=[{
                                "role": "user",
                                "content": f"[선생님이 지정한 오늘의 범위: {today_scope}] 이 범위 안에서 핵심 개념이나 자주 틀리는 오개념을 확인하는 질문을 1개만 던져라.",
                            }],
                        )
                        bot_reply = get_response_text(response)
                        st.session_state.pass_topic = today_scope
                        st.session_state.pass_messages = [{"role": "assistant", "content": bot_reply}]
                        st.session_state.pass_completed = False
                        st.session_state.pass_failed = False
                        st.session_state.pop("pass_fail_archive_entry_id", None)
                        persist_current_student()
                        st.rerun()
                    except Exception as e:
                        show_friendly_error(e)
    else:
        top_c1, top_c2 = st.columns([3, 1])
        with top_c1:
            st.markdown(f"#### 📘 도전 단원: **{st.session_state.pass_topic}**")
        with top_c2:
            if st.button("🔄 다른 단원 도전", key="pass_reset_btn", use_container_width=True):
                st.session_state.pass_topic = None
                st.session_state.pass_messages = []
                st.session_state.pass_completed = False
                st.session_state.pass_failed = False
                st.session_state.pass_concept_summary = ""
                st.session_state.pass_completed_date = ""
                st.session_state.pass_completed_time = ""
                st.session_state.pop("pass_card_bytes", None)
                st.session_state.pop("pass_fail_archive_entry_id", None)
                persist_current_student()
                st.rerun()

        # 대화 기록: 오래된 턴은 접힌 expander, 가장 최근 1턴만 바로 보이게
        pass_history = st.session_state.pass_messages
        pass_older, pass_recent = (
            (pass_history[:-2], pass_history[-2:]) if len(pass_history) > 2 else ([], pass_history)
        )
        if pass_older:
            with st.expander("📝 이전 질문/기록 보기", expanded=False):
                for msg in pass_older:
                    _render_pass_message(msg)
        for msg in pass_recent:
            _render_pass_message(msg)

        if st.session_state.pass_completed:
            # 하원 통제용 '하원 허가증' - 교실 뒤에서 선생님이 한눈에 확인할 수 있게
            # 큰 글자로 통과 시간과 미션 주제를 담는다. 학생은 이 화면만 보여주고 나가면 된다.
            st.markdown(
                f"""
                <div style="
                    background: linear-gradient(135deg, #3A449A, #00A19D);
                    border-radius: 24px;
                    padding: 2.2rem 1.5rem;
                    text-align: center;
                    box-shadow: 0 10px 30px rgba(58, 68, 154, 0.3);
                    margin: 1.2rem 0;
                ">
                    <div style="font-size: 1.05rem; font-weight: 700; color: #BFE8E5; letter-spacing: 0.04em; margin-bottom: 0.4rem;">
                        💮 오늘의 하원 허가증
                    </div>
                    <div style="font-size: 2.3rem; font-weight: 900; color: #FFFFFF; margin-bottom: 1rem;">
                        미션 완료!
                    </div>
                    <div style="background: rgba(255,255,255,0.16); border-radius: 14px; padding: 0.9rem 1.1rem; margin-bottom: 0.7rem;">
                        <div style="font-size: 0.85rem; color: #F0FDFA; opacity: 0.85;">통과 시간</div>
                        <div style="font-size: 1.7rem; font-weight: 800; color: #FFFFFF;">{st.session_state.pass_completed_time or "-"}</div>
                    </div>
                    <div style="background: rgba(255,255,255,0.16); border-radius: 14px; padding: 0.9rem 1.1rem;">
                        <div style="font-size: 0.85rem; color: #F0FDFA; opacity: 0.85;">미션 주제</div>
                        <div style="font-size: 1.3rem; font-weight: 800; color: #FFFFFF;">{st.session_state.pass_topic}</div>
                    </div>
                    <div style="font-size: 1rem; color: #F0FDFA; font-weight: 600; margin-top: 1.1rem;">
                        선생님께 이 화면을 보여주고 하원하세요! 👋
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            if not st.session_state.get("pass_card_bytes"):
                st.session_state.pass_card_bytes = generate_pass_card(
                    nickname=st.session_state.student_name,
                    topic=st.session_state.pass_topic,
                    concept_summary=st.session_state.pass_concept_summary or st.session_state.pass_topic,
                    date_str=st.session_state.pass_completed_date,
                )
            st.image(st.session_state.pass_card_bytes, use_container_width=True)
            st.success("저장해서 인스타에 인증하거나 배경화면으로 써보세요!")
            safe_filename = re.sub(r"[^0-9A-Za-z가-힣_-]", "_", st.session_state.pass_topic)[:30]
            st.download_button(
                "📥 PASS 인증샷 저장하기",
                data=st.session_state.pass_card_bytes,
                file_name=f"PASS_{safe_filename}.png",
                mime="image/png",
                use_container_width=True,
                key="pass_card_download_btn",
            )
        elif st.session_state.pass_failed:
            st.markdown(
                """
                <div style="
                    background: linear-gradient(135deg, #6B7280, #9CA3AF);
                    border-radius: 20px;
                    padding: 1.8rem 1.5rem;
                    text-align: center;
                    box-shadow: 0 10px 24px rgba(107, 114, 128, 0.3);
                    margin: 1.2rem 0;
                ">
                    <div style="font-size: 1.8rem; font-weight: 800; color: #FFFFFF; margin-bottom: 0.3rem;">
                        😢 오늘은 여기까지!
                    </div>
                    <div style="font-size: 1rem; color: #F3F4F6; font-weight: 600;">
                        오답노트에 저장해뒀어. 다음에 다시 도전해보자!
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if st.button(
                "🔄 다시 도전하기", key="pass_fail_retry_btn", use_container_width=True, type="primary"
            ):
                st.session_state.pass_topic = None
                st.session_state.pass_messages = []
                st.session_state.pass_completed = False
                st.session_state.pass_failed = False
                st.session_state.pass_concept_summary = ""
                st.session_state.pass_completed_date = ""
                st.session_state.pass_completed_time = ""
                st.session_state.pop("pass_card_bytes", None)
                st.session_state.pop("pass_fail_archive_entry_id", None)
                persist_current_student()
                st.rerun()
        else:
            st.caption(
                "💡 인스타그램/카카오톡 인앱 브라우저에서 입력이 잘 안 되면, "
                "우측 상단 메뉴를 눌러 [Safari/Chrome으로 열기]를 해줘!"
            )
            with st.form(key="pass_answer_form", clear_on_submit=True):
                pass_answer_draft = st.text_area(
                    "답변 작성",
                    key="pass_answer_text",
                    placeholder="핵심 키워드를 포함해서 1~2줄로 설명해 봐! (예: 조건, 공식 유도 과정 등)",
                    height=140,
                    label_visibility="collapsed",
                )
                pass_submitted = st.form_submit_button(
                    "💮 미션 제출하고 검사받기",
                    use_container_width=True,
                    type="primary",
                )

            if pass_submitted and not pass_answer_draft.strip():
                st.warning("답변을 입력해 주세요.")
            elif pass_submitted and (pass_answer := pass_answer_draft.strip()):
                st.session_state.pass_messages.append({"role": "user", "content": pass_answer})

                if not api_key:
                    st.error("API 키가 설정되지 않았습니다.")
                else:
                    client = anthropic.Anthropic(api_key=api_key)
                    api_pass_messages = [
                        {"role": m["role"], "content": m["content"]}
                        for m in st.session_state.pass_messages
                    ]
                    with st.spinner("답변을 확인하는 중..."):
                        try:
                            response = client.messages.create(
                                model=MODEL_NAME,
                                max_tokens=800,
                                system=load_pass_system_prompt(st.session_state.pass_grade, st.session_state.pass_topic),
                                messages=api_pass_messages,
                            )
                            bot_reply = get_response_text(response)
                            passed = bot_reply.strip().startswith(PASS_SUCCESS_TOKEN)
                            failed = (not passed) and bot_reply.strip().startswith(PASS_FAIL_TOKEN)
                            if passed:
                                body = bot_reply.strip()[len(PASS_SUCCESS_TOKEN):].strip()
                                summary = ""
                                if body.upper().startswith("SUMMARY:"):
                                    first_line, _, remainder = body.partition("\n")
                                    summary = first_line.split(":", 1)[1].strip()
                                    body = remainder.strip()
                                bot_reply = body or "완벽하게 통과했어! 정말 잘했어요! 🎉"

                                st.session_state.pass_completed = True
                                st.session_state.pass_concept_summary = summary or st.session_state.pass_topic
                                kst = timezone(timedelta(hours=9))
                                now_kst = datetime.now(kst)
                                st.session_state.pass_completed_date = now_kst.strftime("%Y.%m.%d")
                                st.session_state.pass_completed_time = now_kst.strftime("%H:%M")
                                st.session_state.pass_records.append({
                                    "topic": st.session_state.pass_topic,
                                    "completed_at": now_kst.strftime("%Y-%m-%d %H:%M"),
                                    "concept_summary": summary or st.session_state.pass_topic,
                                })
                                insert_pass_record(
                                    st.session_state.pass_topic,
                                    summary or st.session_state.pass_topic,
                                )
                                st.session_state.pop("pass_card_bytes", None)
                                st.session_state.pass_messages.append({"role": "assistant", "content": bot_reply})
                                st.balloons()
                            elif failed:
                                body = bot_reply.strip()[len(PASS_FAIL_TOKEN):].strip()
                                bot_reply = body or "오늘은 여기까지 해보고, 다음에 다시 도전해보자! 할 수 있어 💪"
                                st.session_state.pass_messages.append({"role": "assistant", "content": bot_reply})
                                st.session_state.pass_failed = True
                                # 완전히 다른 방향으로 간 시도만 오답노트에 남긴다 (RETRY로 이어지는
                                # 일반 힌트 대화는 아직 진행 중이라 archiving하지 않는다).
                                upsert_question_archive_entry(
                                    "pass_fail",
                                    f"💮 개념PASS (미통과) · {st.session_state.pass_topic}",
                                    "pass_messages",
                                )
                            else:
                                # RETRY: 핵심 키워드 한두 개만 빠진 근접 오답 - 힌트를 주며 같은 시도를 이어간다.
                                st.session_state.pass_messages.append({"role": "assistant", "content": bot_reply})
                            persist_current_student()
                            st.rerun()
                        except Exception as e:
                            show_friendly_error(e)


def render_pass_card_vault():
    """지금까지 통과한 개념들을 목록으로 보여주고, 포토카드를 즉시 다시 받을 수 있게 한다.

    PNG 자체는 저장해 두지 않고(용량 절약), pass_records에 쌓인 topic/completed_at/
    concept_summary로 그때그때 다시 그려서 보여준다 - generate_pass_card()는 순수
    PIL 연산이라 API 호출 없이 바로 끝난다.
    """
    records = st.session_state.get("pass_records", [])
    if not records:
        st.info("아직 PASS한 개념이 없어요. '💮 개념 PASS' 탭에서 오늘 도전해보자!")
        return

    for i, rec in enumerate(reversed(records)):
        topic = rec.get("topic", "-")
        completed_at = rec.get("completed_at", "-")
        concept_summary = rec.get("concept_summary") or topic
        date_str = completed_at[:10].replace("-", ".") if completed_at else ""

        with st.expander(f"💮 {topic} · {completed_at}"):
            card_bytes = generate_pass_card(
                nickname=st.session_state.student_name,
                topic=topic,
                concept_summary=concept_summary,
                date_str=date_str,
            )
            st.image(card_bytes, use_container_width=True)
            safe_filename = re.sub(r"[^0-9A-Za-z가-힣_-]", "_", topic)[:30]
            st.download_button(
                "📥 포토카드 다운로드",
                data=card_bytes,
                file_name=f"PASS_{safe_filename}.png",
                mime="image/png",
                use_container_width=True,
                key=f"pass_vault_dl_{i}",
            )


def render_my_page_tab():
    st.markdown("### 👤 마이페이지")

    if not st.session_state.authenticated:
        render_login_required_gate("👤 마이페이지")
    else:
        # A. 닉네임 + 스트릭 배지, B. 스탬프판 - 탭으로 가리지 않고 화면 맨 위에 항상 보이게
        st.markdown(f"#### 👋 {st.session_state.student_name} 님")
        st.caption("오늘도 다혜T랑 목표한 미션, 완벽하게 끝내보자!")

        grade_c1, grade_c2 = st.columns([3, 1])
        with grade_c1:
            grade_label = st.session_state.pass_grade or "아직 선택 안 함"
            st.caption(f"🎓 개념 PASS 반/학년: **{grade_label}**")
        with grade_c2:
            if st.button("✏️ 변경하기", key="mypage_change_grade_btn", use_container_width=True):
                st.session_state["_show_grade_picker_in_mypage"] = not st.session_state.get(
                    "_show_grade_picker_in_mypage", False
                )

        if st.session_state.get("_show_grade_picker_in_mypage"):
            if render_pass_grade_picker("mypage", button_label="💾 저장"):
                st.session_state["_show_grade_picker_in_mypage"] = False
                st.success(f"✅ {st.session_state.pass_grade}로 변경했습니다!")
                st.rerun()

        render_streak_badge(st.session_state.pass_records)
        render_stamp_board(st.session_state.pass_records)
        st.markdown("---")

        # C. 오답노트 / PASS 포토카드 보관함만 st.tabs로 분리
        tab_notes, tab_vault = st.tabs(["📝 오답노트", "🎴 PASS 포토카드 보관함"])

        with tab_notes:
            render_question_archive_cards()

        with tab_vault:
            render_pass_card_vault()

    # 로그인 여부와 무관하게 화면 맨 아래에 항상 둔다 - 선생님은 학생으로 로그인할
    # 필요 없이 바로 여기서 대시보드로 들어간다.
    render_teacher_entry_point()


def render_teacher_entry_point():
    """'👤 마이페이지' 화면 맨 아래의 은은한 선생님 모드 진입 지점.

    태블릿/모바일 현장 테스트에서 사이드바를 열어 비밀번호를 입력하는 게 너무
    불편하다는 피드백을 받아, 사이드바 대신 터치하기 쉬운 탭 화면 맨 아래로
    옮겼다. 접힌 expander 자체가 "버튼"이고, 펼치면 비밀번호 입력 폼이 나온다.
    """
    st.markdown("---")
    with st.expander("👨‍🏫 선생님 모드 진입"):
        teacher_pw_input = st.text_input(
            "비밀번호", type="password", key="teacher_pw_input"
        )
        if st.button("입장하기", key="teacher_mode_enter_btn", use_container_width=True):
            if teacher_pw_input and teacher_pw_input == TEACHER_DASHBOARD_PASSWORD:
                st.session_state.teacher_mode = True
                st.rerun()
            else:
                st.error("비밀번호가 올바르지 않습니다.")


# st.tabs는 파이썬에서 활성 탭을 지정할 방법이 없어 카카오 로그인 리다이렉트 후
# 항상 첫 탭으로 돌아가 버린다. st.radio는 selected_mode로 복원 가능하므로 이걸 쓴다.
st.radio(
    "모드 선택",
    MODE_OPTIONS,
    key="selected_mode",
    horizontal=True,
    label_visibility="collapsed",
)
st.markdown("---")

if st.session_state.selected_mode == "❓ 질문":
    render_question_clinic_tab()
elif st.session_state.selected_mode == "🔍 첨삭":
    render_mistake_review_tab()
elif st.session_state.selected_mode == "💮 개념PASS":
    render_pass_tab()
elif st.session_state.selected_mode == "👤 마이":
    render_my_page_tab()
