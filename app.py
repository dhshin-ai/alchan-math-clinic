import base64
import html
import io
import json
import os
import re
import ast
import requests
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
import anthropic
import streamlit as st
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np
from PIL import Image, ImageDraw, ImageFont

st.set_page_config(
    page_title="알찬학원 신다혜 쌤의 1:1 수학 클리닉",
    page_icon="✏️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

try:
    api_key = st.secrets["ANTHROPIC_API_KEY"]
except Exception:
    api_key = None

try:
    MASTER_PW = st.secrets["STUDENT_CODE"]
except Exception:
    MASTER_PW = "1234"

KAKAO_CLIENT_ID = st.secrets.get("KAKAO_CLIENT_ID", "")
KAKAO_REDIRECT_URI = st.secrets.get("KAKAO_REDIRECT_URI", "https://math.studyground123.com/")
STUDENT_DATA_DIR = "student_data"
TEACHER_DASHBOARD_PASSWORD = st.secrets.get("TEACHER_DASHBOARD_PASSWORD", "dahye1234")


def get_kakao_login_url():
    """카카오 로그인 동의 화면으로 이동하는 인가 URL을 만든다."""
    return (
        "https://kauth.kakao.com/oauth/authorize"
        f"?client_id={KAKAO_CLIENT_ID}"
        f"&redirect_uri={quote(KAKAO_REDIRECT_URI, safe='')}"
        "&response_type=code"
    )


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


def _student_data_path(kakao_id):
    os.makedirs(STUDENT_DATA_DIR, exist_ok=True)
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(kakao_id))
    return os.path.join(STUDENT_DATA_DIR, f"{safe_id}.json")


def _serialize_messages(messages):
    """st.session_state.messages를 JSON으로 저장 가능한 형태로 변환한다 (이미지는 base64)."""
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
    """저장된 JSON을 다시 st.session_state.messages 형태(이미지는 bytes)로 복원한다."""
    out = []
    for msg in raw_messages:
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


def load_student_data(kakao_id):
    """카카오 고유 회원번호로 저장된 학생의 과목/모드/대화 기록/PASS 기록을 불러온다."""
    try:
        with open(_student_data_path(kakao_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        data["messages"] = _deserialize_messages(data.get("messages", []))
        data["pass_messages"] = _deserialize_messages(data.get("pass_messages", []))
        data.setdefault("pass_topic", None)
        data.setdefault("pass_completed", False)
        data.setdefault("pass_records", [])
        data.setdefault("pass_concept_summary", "")
        data.setdefault("pass_completed_date", "")
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_student_data(
    kakao_id, nickname, student_grade, selected_mode, messages,
    pass_topic=None, pass_messages=None, pass_completed=False, pass_records=None,
    pass_concept_summary="", pass_completed_date="",
):
    """새로고침/재접속 후에도 대화 기록·PASS 진행 상황·PASS 기록이 이어지도록 학생별 파일로 저장한다."""
    data = {
        "kakao_id": kakao_id,
        "nickname": nickname,
        "student_grade": student_grade,
        "selected_mode": selected_mode,
        "messages": _serialize_messages(messages),
        "pass_topic": pass_topic,
        "pass_messages": _serialize_messages(pass_messages or []),
        "pass_completed": pass_completed,
        "pass_records": pass_records or [],
        "pass_concept_summary": pass_concept_summary,
        "pass_completed_date": pass_completed_date,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(_student_data_path(kakao_id), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def persist_current_student():
    """현재 세션(학년/모드/대화 기록/PASS 진행 상황)을 로그인한 카카오 회원번호 파일에 저장한다."""
    if st.session_state.get("kakao_id"):
        save_student_data(
            st.session_state.kakao_id,
            st.session_state.student_name,
            st.session_state.student_grade,
            st.session_state.selected_mode,
            st.session_state.messages,
            pass_topic=st.session_state.get("pass_topic"),
            pass_messages=st.session_state.get("pass_messages", []),
            pass_completed=st.session_state.get("pass_completed", False),
            pass_records=st.session_state.get("pass_records", []),
            pass_concept_summary=st.session_state.get("pass_concept_summary", ""),
            pass_completed_date=st.session_state.get("pass_completed_date", ""),
        )


def list_all_student_records():
    """선생님 대시보드용: student_data/ 폴더의 모든 학생 기록을 최근 업데이트순으로 불러온다."""
    records = []
    if not os.path.isdir(STUDENT_DATA_DIR):
        return records
    for fname in sorted(os.listdir(STUDENT_DATA_DIR)):
        if not fname.endswith(".json"):
            continue
        data = load_student_data(fname[:-5])
        if data:
            records.append(data)
    records.sort(key=lambda d: d.get("updated_at", ""), reverse=True)
    return records


def render_teacher_dashboard():
    """학생들에게는 보이지 않는, 비밀번호로만 들어오는 선생님 전용 대시보드."""
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

    records = list_all_student_records()
    if not records:
        st.info("아직 저장된 학생 데이터가 없습니다.")
        return

    st.caption(f"총 {len(records)}명의 학생 데이터가 저장되어 있습니다.")

    for data in records:
        nickname = data.get("nickname", "익명 학생")
        grade = data.get("student_grade", "-")
        messages = data.get("messages", [])
        pass_records = data.get("pass_records", [])
        updated_at = data.get("updated_at", "")
        q_count = len([m for m in messages if m.get("role") == "user"])

        with st.expander(f"👤 {nickname} ({grade}) · 질문 {q_count}건 · PASS {len(pass_records)}건"):
            st.caption(f"🆔 카카오 회원번호: {data.get('kakao_id', '-')} · 마지막 업데이트: {updated_at}")

            st.markdown("##### 💮 PASS 받은 개념")
            if pass_records:
                for rec in pass_records:
                    st.markdown(f"- **{rec.get('topic', '-')}** · {rec.get('completed_at', '-')}")
            else:
                st.caption("아직 PASS 기록이 없습니다.")

            st.markdown("##### 🙋 질문 클리닉 대화 내역")
            if messages:
                for msg in messages:
                    role_label = "👤 학생" if msg.get("role") == "user" else "🤖 AI"
                    st.markdown(f"**{role_label}**")
                    if msg.get("images"):
                        cols = st.columns(len(msg["images"]))
                        for col, img in zip(cols, msg["images"]):
                            with col:
                                st.image(img["bytes"], width=160)
                    if msg.get("content"):
                        st.write(msg["content"])
                    st.markdown("---")
            else:
                st.caption("아직 질문 클리닉 대화 내역이 없습니다.")


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

    /* 사이드바는 완전히 숨기지 않고 기본 접힘(collapsed) 상태로만 둔다.
       맨 아래 "선생님 모드" 비밀번호 입력칸이 여기 들어있다 (시크릿 선생님 대시보드 진입용). */

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

    /* 오직 메인 모드 선택 버튼 2개(btn_mode1, btn_mode2)만 대형 카드 디자인 적용 */
    div[data-testid="stKey-btn_mode1"] button,
    div[data-testid="stKey-btn_mode2"] button {
        width: 100% !important;
        min-height: 230px !important;
        border-radius: 18px !important;
        border: 2.5px solid #3A449A !important;
        background-color: #FFFFFF !important;
        box-shadow: 0 6px 16px rgba(58, 68, 154, 0.08) !important;
        text-align: center !important;
        padding: 1.6rem 1rem !important;
        transition: all 0.25s ease-in-out !important;
    }

    div[data-testid="stKey-btn_mode1"] button *,
    div[data-testid="stKey-btn_mode2"] button * {
        white-space: pre-wrap !important;
        word-break: keep-all !important;
    }

    div[data-testid="stKey-btn_mode1"] button p,
    div[data-testid="stKey-btn_mode2"] button p {
        margin: 0.3rem 0 !important;
        line-height: 1.55 !important;
        font-size: 0.92rem !important;
        font-weight: 500 !important;
        color: #475569 !important;
    }

    div[data-testid="stKey-btn_mode1"] button strong,
    div[data-testid="stKey-btn_mode2"] button strong {
        font-size: 1.45rem !important;
        font-weight: 800 !important;
        color: #3A449A !important;
        display: block !important;
        margin-bottom: 0.2rem !important;
        line-height: 1.3 !important;
    }

    div[data-testid="stKey-btn_mode1"] button em,
    div[data-testid="stKey-btn_mode2"] button em {
        font-size: 1.05rem !important;
        font-weight: 700 !important;
        font-style: normal !important;
        color: #00A19D !important;
        display: block !important;
        margin-bottom: 0.6rem !important;
        line-height: 1.3 !important;
    }

    div[data-testid="stKey-btn_mode1"] button:hover,
    div[data-testid="stKey-btn_mode2"] button:hover {
        border-color: #00A19D !important;
        background-color: #F0FDFA !important;
        box-shadow: 0 10px 24px rgba(0, 161, 157, 0.18) !important;
        transform: translateY(-3px) !important;
    }

    /* 모바일 화면 대응 */
    @media (max-width: 768px) {
        div[data-testid="stKey-btn_mode1"] button,
        div[data-testid="stKey-btn_mode2"] button {
            min-height: 210px !important;
            padding: 1.2rem 0.8rem !important;
            margin-bottom: 0.8rem !important;
        }

        div[data-testid="stKey-btn_mode1"] button strong,
        div[data-testid="stKey-btn_mode2"] button strong {
            font-size: 1.3rem !important;
        }

        div[data-testid="stKey-btn_mode1"] button em,
        div[data-testid="stKey-btn_mode2"] button em {
            font-size: 1.0rem !important;
        }

        div[data-testid="stKey-btn_mode1"] button p,
        div[data-testid="stKey-btn_mode2"] button p {
            font-size: 0.88rem !important;
        }
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
        f"★ [현재 학생 선택 과목: {student_grade}]\n"
        "★ [🚨 1문 1답 인터랙티브 필수 수칙]:\n"
        "- 전체 단계를 한 번에 출력하지 말고, 출제 유형을 밝힌 후 오직 지금 풀어야 할 '첫 번째 질문(1단계)' 단 하나만 던지고 즉시 말을 마쳐라.\n\n"
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
        "- 안 보이는 보조선을 지어내지 말 것.\n\n"
        "4. 학생이 답을 요구하더라도 최종 정답만은 직접 내주지 마라.\n"
        "5. 수식은 100% LaTeX 표기법(`$ ... $` 또는 `$$ ... $$`)을 사용해라."
        f"{custom_notes}"
    )
    return default_prompt


PASS_SUCCESS_TOKEN = "PASS_SUCCESS"


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
    """지금까지 받은 PASS 개수를 10칸짜리 디지털 스탬프 쿠폰판으로 시각화한다."""
    total = len(pass_records or [])
    board_number, filled = get_stamp_board_state(total)

    st.markdown(f"##### 🎟️ 나의 PASS 스탬프 쿠폰판 ({board_number}번째 판 · {filled}/10)")
    cols = st.columns(10)
    for i, col in enumerate(cols):
        with col:
            if i < filled:
                st.markdown(
                    "<div style='text-align:center; font-size:1.9rem; line-height:1;'>💮</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    "<div style='text-align:center; font-size:1.9rem; line-height:1; color:#CBD5E1;'>⚪</div>",
                    unsafe_allow_html=True,
                )

    if filled >= 10:
        st.success("🎉 축하합니다! 10개 달성! 다혜쌤에게 보여주고 선물을 받으세요 🎁")


def load_pass_system_prompt(student_grade):
    return (
        "너는 알찬학원의 '오늘의 개념 PASS' 테스트를 담당하는, 깐깐하지만 친절한 조교다.\n"
        f"★ [학생 선택 과목: {student_grade}]\n\n"
        "★ [1. 질문 던지기]\n"
        "- 학생이 알려준 단원의 핵심 개념이나 자주 틀리는 오개념을 확인할 수 있는 질문을 오직 1개만 던져라.\n"
        "- 질문 문장 안에 답이나 핵심 키워드를 절대 미리 흘리지 마라.\n\n"
        "★ [2. 채점 원칙 - 가장 중요한 규칙]\n"
        "- 학생의 답변에 핵심 키워드가 빠졌거나 설명이 정확하지 않으면, 정답을 절대로 먼저 알려주지 말고 짧은 힌트만 주면서 다시 답해보라고 유도해라.\n"
        "- 학생의 답변이 개념을 정확히 꿰뚫고 있다고 판단될 때만 통과시켜라. 통과 시 반드시 아래 3줄 형식을 정확히 지켜서 출력해라 (다른 설명 없이 이 형식 그대로):\n"
        f"  1번째 줄: '{PASS_SUCCESS_TOKEN}' 이라는 단어 단독으로만\n"
        "  2번째 줄: 'SUMMARY: ' 다음에 이 개념의 핵심을 15~30자 내외 한 문장으로 요약 (인증샷 카드에 그대로 노출됨)\n"
        "  3번째 줄부터: 이모지를 듬뿍 써서 아주 크고 신나게 칭찬하는 축하 메시지 1~2줄\n"
        f"- 아직 통과시킬 수준이 아니라면 '{PASS_SUCCESS_TOKEN}'이라는 단어를 절대 쓰지 마라.\n\n"
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
    footer = "알찬학원 신다혜 쌤의 수학 클리닉"
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
if "kakao_needs_grade" not in st.session_state:
    st.session_state.kakao_needs_grade = False
if "kakao_login_error" not in st.session_state:
    st.session_state.kakao_login_error = None
if "selected_mode" not in st.session_state:
    st.session_state.selected_mode = None
if "messages" not in st.session_state:
    st.session_state.messages = []
if "pass_topic" not in st.session_state:
    st.session_state.pass_topic = None
if "pass_messages" not in st.session_state:
    st.session_state.pass_messages = []
if "pass_completed" not in st.session_state:
    st.session_state.pass_completed = False
if "pass_records" not in st.session_state:
    st.session_state.pass_records = []
if "pass_concept_summary" not in st.session_state:
    st.session_state.pass_concept_summary = ""
if "pass_completed_date" not in st.session_state:
    st.session_state.pass_completed_date = ""
if "teacher_mode" not in st.session_state:
    st.session_state.teacher_mode = False

st.title("✏️ 알찬학원 신다혜 쌤의 1:1 수학 클리닉")

# ==========================================
# 🕵️ 시크릿 선생님 모드 (사이드바 맨 아래, 학생 화면은 그대로 평범한 접힌 사이드바)
# 비밀번호가 맞으면 학생 로그인 절차 없이 바로 대시보드로 진입한다.
# ==========================================
with st.sidebar:
    st.markdown("<div style='height: 62vh;'></div>", unsafe_allow_html=True)
    teacher_pw_input = st.text_input("선생님 모드", type="password", key="teacher_pw_input")
    if teacher_pw_input and teacher_pw_input == TEACHER_DASHBOARD_PASSWORD:
        st.session_state.teacher_mode = True

if st.session_state.teacher_mode:
    render_teacher_dashboard()
    st.stop()

# ==========================================
# 🔐 카카오 로그인 콜백 처리
# (인가 코드(code) → 액세스 토큰 → 닉네임/고유 회원번호 조회)
# ==========================================
if not st.session_state.authenticated and "code" in st.query_params:
    auth_code = st.query_params.get("code")
    st.query_params.clear()
    if not KAKAO_CLIENT_ID:
        st.session_state.kakao_login_error = "카카오 로그인 설정(KAKAO_CLIENT_ID)이 비어 있습니다. 선생님에게 문의해 주세요."
    else:
        try:
            kakao_id, nickname = exchange_kakao_code(auth_code)
            st.session_state.kakao_id = kakao_id
            st.session_state.student_name = nickname

            saved = load_student_data(kakao_id)
            if saved and saved.get("student_grade"):
                # 이미 과목을 선택해 둔 적이 있는 학생 → 대화 기록까지 그대로 복원하고 바로 입장
                st.session_state.student_grade = saved["student_grade"]
                st.session_state.selected_mode = saved.get("selected_mode")
                st.session_state.messages = saved.get("messages", [])
                st.session_state.pass_topic = saved.get("pass_topic")
                st.session_state.pass_messages = saved.get("pass_messages", [])
                st.session_state.pass_completed = saved.get("pass_completed", False)
                st.session_state.pass_records = saved.get("pass_records", [])
                st.session_state.pass_concept_summary = saved.get("pass_concept_summary", "")
                st.session_state.pass_completed_date = saved.get("pass_completed_date", "")
                st.session_state.pop("pass_card_bytes", None)
                st.session_state.authenticated = True
            else:
                # 첫 로그인 → 과목 선택 한 번만 더 받는다 (이전 세션의 흔적이 섞이지 않게 초기화)
                st.session_state.selected_mode = None
                st.session_state.messages = []
                st.session_state.pass_topic = None
                st.session_state.pass_messages = []
                st.session_state.pass_completed = False
                st.session_state.pass_records = []
                st.session_state.pass_concept_summary = ""
                st.session_state.pass_completed_date = ""
                st.session_state.pop("pass_card_bytes", None)
                st.session_state.kakao_needs_grade = True
        except Exception as e:
            st.session_state.kakao_login_error = f"카카오 로그인에 실패했습니다: {e}"
    st.rerun()

if not st.session_state.authenticated and "error" in st.query_params:
    st.query_params.clear()
    st.session_state.kakao_login_error = "카카오 로그인이 취소되었습니다."
    st.rerun()

if not st.session_state.authenticated:
    st.markdown("---")

    if st.session_state.kakao_login_error:
        st.error(st.session_state.kakao_login_error)
        st.session_state.kakao_login_error = None

    if st.session_state.kakao_needs_grade:
        st.subheader(f"👋 {st.session_state.student_name} 님, 반가워요!")
        st.caption("2022 개정 교육과정에 맞춘 과목을 선택하고 클리닉을 시작해 주세요. (최초 1회만 선택)")
        input_grade = st.selectbox(
            "학년 / 과목 선택 (2022 개정 교육과정):",
            ["중2", "중3", "공통수학1", "공통수학2", "대수", "미적분1", "미적분2", "확률과 통계", "기하와 벡터"]
        )
        if st.button("🚀 클리닉 입장하기", use_container_width=True):
            st.session_state.student_grade = input_grade
            st.session_state.authenticated = True
            st.session_state.kakao_needs_grade = False
            persist_current_student()
            st.rerun()
    else:
        st.subheader("👋 수강생 입장하기")
        st.caption("카카오 계정으로 로그인하면 대화 기록이 안전하게 저장돼서, 다음에 또 이어서 질문할 수 있어요!")

        if KAKAO_CLIENT_ID:
            st.link_button("💬 카카오로 로그인", get_kakao_login_url(), use_container_width=True, type="primary")
        else:
            st.warning("카카오 로그인이 아직 설정되지 않았습니다. 선생님에게 문의해 주세요.")
    st.stop()

if "last_upload_key" not in st.session_state:
    st.session_state.last_upload_key = None

system_prompt = load_system_prompt(st.session_state.student_grade)

cap_col1, cap_col2 = st.columns([4, 1])
with cap_col1:
    st.caption(f"👤 현재 접속 학생: **{st.session_state.student_name}** | 🎓 과목: **{st.session_state.student_grade}**")
with cap_col2:
    if st.button("🔓 로그아웃", use_container_width=True):
        persist_current_student()
        for key in [
            "authenticated", "student_name", "kakao_id", "kakao_needs_grade",
            "selected_mode", "messages", "last_upload_key", "slack_thread_ts",
            "pass_topic", "pass_messages", "pass_completed", "pass_records",
            "pass_concept_summary", "pass_completed_date", "pass_card_bytes",
        ]:
            st.session_state.pop(key, None)
        st.rerun()

_pass_streak = calculate_pass_streak(st.session_state.pass_records)
if _pass_streak > 0:
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
            🔥 현재 {_pass_streak}일 연속 PASS 중!
        </div>
        """,
        unsafe_allow_html=True,
    )
else:
    st.caption("💤 아직 연속 기록이 없어요. 오늘 '💮 개념 PASS'에 도전해서 스트릭을 시작해보자!")

MODEL_NAME = "claude-sonnet-4-5"


def render_clinic_tab():
    if st.session_state.selected_mode is None:
        st.markdown(
            """
        <div class="guide-box">
            <div class="guide-title">💡 클리닉 이용 안내</div>
            <div class="guide-item">📢 <b>공지사항:</b> AI 조교가 다혜 쌤의 특허 풀이를 열심히 배우는 중이라 도형이나 고난도 문제는 가끔 어리바리할 수 있어! 힌트가 이상하거나 막히면 "🔓 80% 풀이 바로 보기" 버튼을 눌러봐!</div>
            <div class="guide-item">❓ <b>풀이가 막혔어요:</b> 문제 사진 1장을 올리면, 정답 대신 스스로 답을 찾아갈 수 있게 질문을 던져줍니다.</div>
            <div class="guide-item">✍️ <b>내 풀이 검토:</b> 사진 1장 또는 2장을 올리면 연습장 손글씨 풀이의 연산 실수를 콕 짚어줍니다.</div>
        </div>
        """,
            unsafe_allow_html=True,
        )

        st.markdown("### 💡 어떤 도움이 필요하신가요?")
        col1, col2 = st.columns(2)

        with col1:
            btn1_text = (
                "**❓ 풀이가 막혔어요**\n"
                "*(스스로 풀어보기)*\n\n"
                "📷 문제 사진 1장 필요\n\n"
                "정답 대신 스스로 답을 찾아갈 수 있게\n\n"
                "차근차근 질문을 던져줄게요!"
            )
            if st.button(btn1_text, key="btn_mode1", use_container_width=True):
                st.session_state.selected_mode = "❓ 스스로 풀어보기 (차근차근 질문)"
                st.session_state.slack_thread_ts = None
                st.rerun()

        with col2:
            btn2_text = (
                "**✍️ 내 풀이 검토**\n"
                "*(연습장 오답 클리닉)*\n\n"
                "📷 사진 1장 또는 2장 가능\n\n"
                "연습장 손글씨 풀이를 함께 올리면\n\n"
                "어느 줄에서 연산 실수가 났는지 찾아줄게요!"
            )
            if st.button(btn2_text, key="btn_mode2", use_container_width=True):
                st.session_state.selected_mode = "✍️ 내 풀이 검토 (문제 + 연습장)"
                st.session_state.slack_thread_ts = None
                st.rerun()

        return

    mode = st.session_state.selected_mode

    top_col1, top_col2 = st.columns([3, 1])
    with top_col1:
        st.markdown(f"#### 📌 선택한 모드: **{mode}**")
    with top_col2:
        if st.button("🏠 첫 화면으로 (홈)", use_container_width=True):
            st.session_state.selected_mode = None
            st.session_state.messages = []
            st.session_state.last_upload_key = None
            st.session_state.slack_thread_ts = None
            persist_current_student()
            st.rerun()

    st.markdown("---")

    # ==========================================
    # 📷 심플 문제 사진 업로드
    # ==========================================
    uploaded_solution = None

    st.markdown("### 📝 문제 올리기")
    if mode == "✍️ 내 풀이 검토 (문제 + 연습장)":
        up_c1, up_c2 = st.columns(2)
        with up_c1:
            uploaded_problem = st.file_uploader(
                "1️⃣ 문제 사진 (또는 문제+풀이 사진)", type=["jpg", "jpeg", "png"], key="prob_clean"
            )
        with up_c2:
            uploaded_solution = st.file_uploader(
                "2️⃣ 연습장 풀이 사진 (선택)", type=["jpg", "jpeg", "png"], key="sol_clean"
            )
    else:
        uploaded_problem = st.file_uploader(
            "풀다가 막힌 문제 사진을 올려주세요!",
            type=["jpg", "jpeg", "png"],
            key="prob_clean",
        )

    prob_id = uploaded_problem.file_id if uploaded_problem else "none"
    sol_id = uploaded_solution.file_id if uploaded_solution else "none"
    curr_upload_key = f"{mode}_{prob_id}_{sol_id}"

    if api_key and (curr_upload_key != st.session_state.last_upload_key):
        if mode == "❓ 스스로 풀어보기 (차근차근 질문)" and uploaded_problem:
            st.session_state.last_upload_key = curr_upload_key
            st.session_state.messages = []

            client = anthropic.Anthropic(api_key=api_key)
            p_bytes = uploaded_problem.getvalue()
            send_telegram_photo(p_bytes, st.session_state.student_name, mode)

            api_text = f"[모드: 스스로 풀어보기 / 과목: {st.session_state.student_grade}] 먼저 1단계로 [과목 > 대단원 > 중단원 > 세부 대표유형명] 4단계로 세부 출제 유형을 밝히고, 인식한 조건이나 각도를 먼저 확인한 뒤 2단계 힌트 질문을 던져라."
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

            with st.spinner("문제를 꼼꼼하게 살피는 중입니다..."):
                try:
                    response = client.messages.create(
                        model=MODEL_NAME,
                        max_tokens=2000,
                        system=system_prompt,
                        messages=[{"role": "user", "content": content_blocks}],
                    )
                    bot_reply = get_response_text(response)
                    # 사진 제출 자체도 대화 기록에 남겨서 새로고침 후에도 사진 내역이 보이게 한다.
                    st.session_state.messages.append({
                        "role": "user",
                        "content": "📷 문제 사진을 올렸어요!",
                        "images": [{"bytes": p_bytes, "mime": uploaded_problem.type}],
                        "api_text": api_text,
                    })
                    st.session_state.messages.append(
                        {"role": "assistant", "content": bot_reply}
                    )
                    log_to_slack_and_gsheet(st.session_state.student_name, st.session_state.student_grade, mode, "문제 사진 업로드 제출", bot_reply, skip_telegram=True)
                    persist_current_student()
                    st.rerun()
                except Exception as e:
                    st.error(f"오류가 발생했습니다: {e}")

        elif mode == "✍️ 내 풀이 검토 (문제 + 연습장)" and uploaded_problem:
            st.session_state.last_upload_key = curr_upload_key
            st.session_state.messages = []

            client = anthropic.Anthropic(api_key=api_key)

            p_bytes = uploaded_problem.getvalue()
            send_telegram_photo(p_bytes, st.session_state.student_name, f"{mode} - 문제")

            content_blocks = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": uploaded_problem.type,
                        "data": base64.b64encode(p_bytes).decode("utf-8"),
                    },
                }
            ]

            if uploaded_solution:
                s_bytes = uploaded_solution.getvalue()
                send_telegram_photo(s_bytes, st.session_state.student_name, f"{mode} - 풀이")
                content_blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": uploaded_solution.type,
                            "data": base64.b64encode(s_bytes).decode("utf-8"),
                        },
                    }
                )
                prompt_guide = f"[모드: 내 풀이 검토 / 과목: {st.session_state.student_grade}] 문제 사진과 연습장 손글씨 풀이 사진 2장이 전달되었어. 첫 줄부터 줄 단위로 연산을 검증해서 잘한 점과 부호/계산 실수가 발생한 정확한 위치를 다정하게 지적해줘."
            else:
                prompt_guide = f"[모드: 내 풀이 검토 / 과목: {st.session_state.student_grade}] 이 사진 안의 손글씨 풀이를 첫 줄부터 줄 단위로 검증하여 잘한 점과 연산 실수가 발생한 정확한 위치를 콕 짚어라."

            content_blocks.append({"type": "text", "text": prompt_guide})

            with st.spinner("손글씨 풀이를 줄 단위로 분석하는 중입니다..."):
                try:
                    response = client.messages.create(
                        model=MODEL_NAME,
                        max_tokens=2000,
                        system=system_prompt,
                        messages=[{"role": "user", "content": content_blocks}],
                    )
                    bot_reply = get_response_text(response)
                    images_submitted = [{"bytes": p_bytes, "mime": uploaded_problem.type}]
                    label = "📷 문제 사진을 올렸어요!"
                    if uploaded_solution:
                        images_submitted.append({"bytes": s_bytes, "mime": uploaded_solution.type})
                        label = "📷 문제 + 풀이 사진을 올렸어요!"
                    # 사진 제출 자체도 대화 기록에 남겨서 새로고침 후에도 사진 내역이 보이게 한다.
                    st.session_state.messages.append({
                        "role": "user",
                        "content": label,
                        "images": images_submitted,
                        "api_text": prompt_guide,
                    })
                    st.session_state.messages.append(
                        {"role": "assistant", "content": bot_reply}
                    )
                    log_to_slack_and_gsheet(st.session_state.student_name, st.session_state.student_grade, mode, "풀이 검토 사진 제출", bot_reply, skip_telegram=True)
                    persist_current_student()
                    st.rerun()
                except Exception as e:
                    st.error(f"오류가 발생했습니다: {e}")

    for msg in st.session_state.messages:
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

    # ==========================================
    # 🆘 학생 구출용 퀵 액션 버튼 (AI가 답변한 직후에만 노출)
    # ==========================================
    if len(st.session_state.messages) > 1 and st.session_state.messages[-1]["role"] == "assistant":
        st.markdown("👇 **어떻게 풀어야 할지 막막하다면 버튼을 눌러주세요!**")

        btn_col1, btn_col2, btn_col3 = st.columns(3)

        with btn_col1:
            if st.button("💡 다음 힌트만 살짝", use_container_width=True):
                st.session_state.messages.append({"role": "user", "content": "다음 단계로 넘어가는 힌트만 살짝 알려줘."})
                st.rerun()

        with btn_col2:
            if st.button("🔓 80% 풀이 바로 보기", use_container_width=True, type="primary"):
                st.session_state.messages.append({"role": "user", "content": "진짜 모르겠어. 날 괴롭히지 말고 풀이의 80%를 시원하게 바로 보여줘!"})
                st.rerun()

        with btn_col3:
            if st.button("📝 유사문제 연습하기", use_container_width=True):
                st.session_state.messages.append({"role": "user", "content": "이 개념을 쓰는 비슷한 다른 문제로 연습해보고 싶어."})
                st.rerun()

    # ==========================================
    # 💬 심플 대화 입력창 (카톡 스타일)
    # ==========================================
    if prompt := st.chat_input("다혜 쌤 질문에 답하거나 궁금한 점을 적어주세요..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.rerun()

    # ==========================================
    # 🤖 마지막 메시지가 학생 답변이면 다혜 쌤 AI 응답 생성
    # ==========================================
    if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
        if not api_key:
            st.error("API 키가 설정되지 않았습니다.")
            return

        client = anthropic.Anthropic(api_key=api_key)

        # 대화 기록(저장/복원된 기록 포함)을 그대로 API 메시지로 변환한다.
        # 사진이 달린 턴은 최초 업로드 위젯이 아니라 messages에 저장해 둔
        # 이미지/지시문을 사용하므로, 새로고침 후 복원된 세션에서도 동일하게 동작한다.
        api_messages = []
        for msg in st.session_state.messages:
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
                    st.session_state.messages.append({"role": "assistant", "content": bot_reply})
                    last_student_text = st.session_state.messages[-2]["content"]
                    log_to_slack_and_gsheet(
                        st.session_state.student_name, st.session_state.student_grade, mode,
                        last_student_text, bot_reply,
                    )
                    persist_current_student()
                    st.rerun()
                except Exception as e:
                    st.error(f"오류가 발생했습니다: {e}")


def render_pass_tab():
    st.markdown("### 💮 오늘의 개념 PASS")
    st.caption("오늘 배운 단원을 적고 도전해봐! 핵심 개념이나 오개념을 확인하는 질문을 던져줄게.")

    render_stamp_board(st.session_state.pass_records)
    st.markdown("---")

    pass_col_main, pass_col_history = st.columns([3, 1])

    with pass_col_main:
        if not st.session_state.pass_topic:
            topic_input = st.text_input(
                "오늘 배운 단원을 입력해줘 (예: 이차함수 최대최소)", key="pass_topic_input"
            )
            if st.button("🚀 PASS 도전하기", key="pass_start_btn", use_container_width=True, type="primary"):
                clean_topic = topic_input.strip()
                if not clean_topic:
                    st.error("단원 이름을 먼저 입력해 줘!")
                elif not api_key:
                    st.error("API 키가 설정되지 않았습니다.")
                else:
                    client = anthropic.Anthropic(api_key=api_key)
                    with st.spinner("질문을 준비하는 중..."):
                        try:
                            response = client.messages.create(
                                model=MODEL_NAME,
                                max_tokens=600,
                                system=load_pass_system_prompt(st.session_state.student_grade),
                                messages=[{
                                    "role": "user",
                                    "content": f"[오늘 배운 단원: {clean_topic}] 이 단원의 핵심 개념이나 자주 틀리는 오개념을 확인하는 질문을 1개만 던져라.",
                                }],
                            )
                            bot_reply = get_response_text(response)
                            st.session_state.pass_topic = clean_topic
                            st.session_state.pass_messages = [{"role": "assistant", "content": bot_reply}]
                            st.session_state.pass_completed = False
                            persist_current_student()
                            st.rerun()
                        except Exception as e:
                            st.error(f"오류가 발생했습니다: {e}")
        else:
            top_c1, top_c2 = st.columns([3, 1])
            with top_c1:
                st.markdown(f"#### 📘 도전 단원: **{st.session_state.pass_topic}**")
            with top_c2:
                if st.button("🔄 다른 단원 도전", key="pass_reset_btn", use_container_width=True):
                    st.session_state.pass_topic = None
                    st.session_state.pass_messages = []
                    st.session_state.pass_completed = False
                    st.session_state.pass_concept_summary = ""
                    st.session_state.pass_completed_date = ""
                    st.session_state.pop("pass_card_bytes", None)
                    persist_current_student()
                    st.rerun()

            for msg in st.session_state.pass_messages:
                with st.chat_message(msg["role"]):
                    if msg["role"] == "assistant":
                        render_handwritten_note(msg["content"])
                    else:
                        st.markdown(msg["content"])

            if st.session_state.pass_completed:
                st.markdown(
                    """
                    <div style="
                        background: linear-gradient(135deg, #3A449A, #00A19D);
                        border-radius: 20px;
                        padding: 2rem 1.5rem;
                        text-align: center;
                        box-shadow: 0 10px 30px rgba(58, 68, 154, 0.3);
                        margin: 1.2rem 0;
                    ">
                        <div style="font-size: 2.1rem; font-weight: 800; color: #FFFFFF; margin-bottom: 0.3rem;">
                            💮 PASS 완료!
                        </div>
                        <div style="font-size: 1.05rem; color: #F0FDFA; font-weight: 600;">
                            오늘의 개념을 완벽하게 통과했어요!
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
            else:
                if pass_answer := st.chat_input("여기에 답변을 적어줘!", key="pass_chat_input"):
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
                                    system=load_pass_system_prompt(st.session_state.student_grade),
                                    messages=api_pass_messages,
                                )
                                bot_reply = get_response_text(response)
                                passed = bot_reply.strip().startswith(PASS_SUCCESS_TOKEN)
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
                                    st.session_state.pass_records.append({
                                        "topic": st.session_state.pass_topic,
                                        "completed_at": now_kst.strftime("%Y-%m-%d %H:%M"),
                                    })
                                    st.session_state.pop("pass_card_bytes", None)
                                    st.balloons()
                                st.session_state.pass_messages.append({"role": "assistant", "content": bot_reply})
                                persist_current_student()
                                st.rerun()
                            except Exception as e:
                                st.error(f"오류가 발생했습니다: {e}")

    with pass_col_history:
        st.markdown("##### 📋 나의 PASS 기록")
        if st.session_state.pass_records:
            for rec in reversed(st.session_state.pass_records):
                safe_topic = html.escape(str(rec.get("topic", "")))
                safe_date = html.escape(str(rec.get("completed_at", "")))
                st.markdown(
                    f"<div style='font-size:0.82rem; line-height:1.5; margin-bottom:0.6rem;'>"
                    f"💮 <b>{safe_topic}</b><br>"
                    f"<span style='color:#64748B;'>🗓️ {safe_date}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
        else:
            st.caption("아직 PASS한 단원이 없어요. 지금 도전해볼까?")


tab1, tab2 = st.tabs(["🙋 질문 클리닉", "💮 오늘의 개념 PASS"])
with tab1:
    render_clinic_tab()
with tab2:
    render_pass_tab()
