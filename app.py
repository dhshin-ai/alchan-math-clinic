import base64
import html
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
STUDENT_DATA_DIR = "student_data"
TEACHER_DASHBOARD_PASSWORD = st.secrets.get("TEACHER_DASHBOARD_PASSWORD", "dahye1234")

PASS_GRADE_LEVELS = ["중1", "중2", "중3", "고1", "고2", "고3"]
DAILY_PASS_SCOPE_FILE = "daily_pass_scope.json"
MODE_OPTIONS = ["❓ 질문 클리닉", "🔍 오답 첨삭", "💮 개념 PASS"]


def load_daily_pass_scope():
    """선생님이 학년별로 지정해 둔 '오늘의 개념 범위'를 불러온다. 없는 학년은 빈 문자열."""
    try:
        with open(DAILY_PASS_SCOPE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return {grade: str(data.get(grade, "")).strip() for grade in PASS_GRADE_LEVELS}


def save_daily_pass_scope(scope_by_grade):
    """학년별 '오늘의 개념 범위'를 파일로 저장해, 모든 학생의 PASS 탭에 그대로 반영되게 한다."""
    data = {grade: str(scope_by_grade.get(grade, "")).strip() for grade in PASS_GRADE_LEVELS}
    try:
        with open(DAILY_PASS_SCOPE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


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


def _student_data_path(kakao_id):
    os.makedirs(STUDENT_DATA_DIR, exist_ok=True)
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(kakao_id))
    return os.path.join(STUDENT_DATA_DIR, f"{safe_id}.json")


def _serialize_messages(messages):
    """메시지 리스트(clinic/review/pass 공용)를 JSON으로 저장 가능한 형태로 변환한다 (이미지는 base64)."""
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
    """저장된 JSON을 다시 메시지 리스트 형태(이미지는 bytes)로 복원한다."""
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


def _serialize_archive(archive):
    """question_archive(카드별 messages 포함)를 JSON 저장 가능한 형태로 변환한다."""
    out = []
    for entry in archive:
        e = {k: v for k, v in entry.items() if k != "messages"}
        e["messages"] = _serialize_messages(entry.get("messages", []))
        out.append(e)
    return out


def _deserialize_archive(raw_archive):
    out = []
    for entry in raw_archive:
        e = {k: v for k, v in entry.items() if k != "messages"}
        e["messages"] = _deserialize_messages(entry.get("messages", []))
        out.append(e)
    return out


def load_student_data(kakao_id):
    """카카오 고유 회원번호로 저장된 학생의 과목/오답노트 아카이브/PASS 기록을 불러온다."""
    try:
        with open(_student_data_path(kakao_id), "r", encoding="utf-8") as f:
            data = json.load(f)

        # 구버전 포맷 호환: messages+selected_mode(가장 옛버전) → clinic/review_messages →
        # question_archive(현재 포맷) 순으로 한 번씩 이관한다.
        if "clinic_messages" not in data and "messages" in data:
            if data.get("selected_mode") == "✍️ 내 풀이 검토 (문제 + 연습장)":
                data["review_messages"] = data.get("messages", [])
            else:
                data["clinic_messages"] = data.get("messages", [])

        if "question_archive" not in data:
            archive = []
            fallback_date = (data.get("updated_at") or "")[:16].replace("T", " ")
            for feature_label, msgs in (
                ("❓ 질문 클리닉", data.get("clinic_messages", [])),
                ("🔍 오답 첨삭", data.get("review_messages", [])),
            ):
                if msgs:
                    archive.append({
                        "id": uuid.uuid4().hex,
                        "feature": feature_label,
                        "created_at": fallback_date,
                        "messages": msgs,
                    })
            data["question_archive"] = archive

        data["question_archive"] = _deserialize_archive(data.get("question_archive", []))
        data["pass_messages"] = _deserialize_messages(data.get("pass_messages", []))
        data.setdefault("pass_topic", None)
        data.setdefault("pass_completed", False)
        data.setdefault("pass_records", [])
        data.setdefault("pass_concept_summary", "")
        data.setdefault("pass_completed_date", "")
        data.setdefault("pass_grade", "")
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_student_data(
    kakao_id, nickname, student_grade, question_archive,
    pass_topic=None, pass_messages=None, pass_completed=False, pass_records=None,
    pass_concept_summary="", pass_completed_date="", pass_grade="",
):
    """학년/오답노트 아카이브/PASS 진행 상황·기록이 이어지도록 학생별 파일로 저장한다.

    질문 클리닉·오답 첨삭의 '현재 화면' 대화(clinic_messages/review_messages)는 더 이상
    저장하지 않는다 - 접속할 때마다 백지로 시작하고, 완료된 질문만 question_archive에
    차곡차곡 쌓여 '나의 오답노트' 팝업에서 다시 볼 수 있다.
    """
    data = {
        "kakao_id": kakao_id,
        "nickname": nickname,
        "student_grade": student_grade,
        "question_archive": _serialize_archive(question_archive),
        "pass_topic": pass_topic,
        "pass_messages": _serialize_messages(pass_messages or []),
        "pass_completed": pass_completed,
        "pass_records": pass_records or [],
        "pass_concept_summary": pass_concept_summary,
        "pass_completed_date": pass_completed_date,
        "pass_grade": pass_grade,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(_student_data_path(kakao_id), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def persist_current_student():
    """현재 세션(학년/오답노트 아카이브/PASS 진행 상황)을 로그인한 카카오 회원번호 파일에 저장한다."""
    if st.session_state.get("kakao_id"):
        save_student_data(
            st.session_state.kakao_id,
            st.session_state.student_name,
            st.session_state.student_grade,
            st.session_state.get("question_archive", []),
            pass_topic=st.session_state.get("pass_topic"),
            pass_messages=st.session_state.get("pass_messages", []),
            pass_completed=st.session_state.get("pass_completed", False),
            pass_records=st.session_state.get("pass_records", []),
            pass_concept_summary=st.session_state.get("pass_concept_summary", ""),
            pass_completed_date=st.session_state.get("pass_completed_date", ""),
            pass_grade=st.session_state.get("pass_grade", ""),
        )


def upsert_question_archive_entry(state_prefix, feature_label, messages_key):
    """현재 살아있는 대화(messages_key)를 question_archive의 해당 카드에 생성/갱신한다.

    사진을 새로 올리면 새 카드를 만들고, 그 뒤 같은 문제로 이어지는 후속 대화는
    (힌트 요청 등) 같은 카드를 계속 갱신한다 - 카드 1개 = 질문 1건.
    """
    entry_id_key = f"{state_prefix}_archive_entry_id"
    entry_id = st.session_state.get(entry_id_key)
    messages_snapshot = list(st.session_state[messages_key])

    if entry_id:
        for entry in st.session_state.question_archive:
            if entry["id"] == entry_id:
                entry["messages"] = messages_snapshot
                return

    new_id = uuid.uuid4().hex
    st.session_state[entry_id_key] = new_id
    st.session_state.question_archive.append({
        "id": new_id,
        "feature": feature_label,
        "created_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "messages": messages_snapshot,
    })


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

    st.markdown("#### 📌 학년별 오늘의 개념 범위 설정")
    st.caption("여기에 입력한 범위가 학생들의 '개념 PASS' 탭에 그대로 노출되고, AI 구술 질문도 이 범위 안에서만 나갑니다.")

    st.markdown("##### 📸 칠판 판서 사진으로 자동 추출 (선택)")
    st.caption("수업 후 칠판 사진을 올리면 AI가 핵심 개념을 요약해서 아래 해당 학년 텍스트 칸에 채워줘요. 저장 전에 한 번 확인해 주세요!")
    board_col1, board_col2 = st.columns([1, 2])
    with board_col1:
        board_grade = st.selectbox("어느 학년 칠판 사진인가요?", PASS_GRADE_LEVELS, key="board_photo_grade")
    with board_col2:
        board_photo = st.file_uploader(
            "📸 오늘 칠판 판서 사진 올리기", type=["jpg", "jpeg", "png"], key="board_photo_upload"
        )

    if st.button("🔍 AI로 핵심 개념 추출하기", key="extract_board_btn", disabled=not board_photo):
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
                    # 아래 scope_input_{grade} text_area가 생성되기 전에 세션 상태를 먼저
                    # 채워 두면, 다음 렌더에서 그 값이 입력창의 현재 값으로 그대로 반영된다.
                    st.session_state[f"scope_input_{board_grade}"] = extracted
                    st.success(f"✅ {board_grade} 칠판 판서에서 핵심 개념을 추출했어요! 아래에서 확인하고 저장해 주세요.")
                    st.rerun()
                except Exception as e:
                    show_friendly_error(e, "😥 칠판 판서 분석 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.")

    st.markdown("---")

    current_scope = load_daily_pass_scope()
    with st.form("daily_pass_scope_form"):
        new_scope = {}
        scope_cols = st.columns(3)
        for i, grade in enumerate(PASS_GRADE_LEVELS):
            with scope_cols[i % 3]:
                new_scope[grade] = st.text_area(
                    grade,
                    value=current_scope.get(grade, ""),
                    height=100,
                    key=f"scope_input_{grade}",
                    placeholder="예: 나머지정리와 인수정리",
                )
        if st.form_submit_button("💾 오늘의 범위 저장하기", use_container_width=True):
            save_daily_pass_scope(new_scope)
            st.success("✅ 오늘의 개념 범위를 저장했습니다! 학생 화면에 바로 반영됩니다.")

    st.markdown("---")

    records = list_all_student_records()
    if not records:
        st.info("아직 저장된 학생 데이터가 없습니다.")
        return

    st.caption(f"총 {len(records)}명의 학생 데이터가 저장되어 있습니다.")

    def _render_msg_list(msg_list):
        for msg in msg_list:
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

    for data in records:
        nickname = data.get("nickname", "익명 학생")
        grade = data.get("student_grade", "-")
        archive = data.get("question_archive", [])
        pass_records = data.get("pass_records", [])
        updated_at = data.get("updated_at", "")
        q_count = len(archive)

        with st.expander(f"👤 {nickname} ({grade}) · 질문 {q_count}건 · PASS {len(pass_records)}건"):
            st.caption(f"🆔 카카오 회원번호: {data.get('kakao_id', '-')} · 마지막 업데이트: {updated_at}")

            st.markdown("##### 💮 PASS 받은 개념")
            if pass_records:
                for rec in pass_records:
                    st.markdown(f"- **{rec.get('topic', '-')}** · {rec.get('completed_at', '-')}")
            else:
                st.caption("아직 PASS 기록이 없습니다.")

            st.markdown("##### 📁 질문 클리닉 / 오답 첨삭 - 오답노트")
            if archive:
                for entry in sorted(archive, key=lambda e: e.get("created_at", ""), reverse=True):
                    st.caption(f"{entry.get('feature', '')} · {entry.get('created_at', '')}")
                    _render_msg_list(entry.get("messages", []))
            else:
                st.caption("아직 대화 내역이 없습니다.")


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


@st.dialog("📁 나의 지난 질문 & 오답노트", width="large")
def render_question_archive_dialog():
    """question_archive를 날짜별(최신순)로 묶어 카드 형태로 보여주는 팝업.

    메인 화면은 매번 백지로 시작하지만, 완료된 질문은 여기서 언제든 다시 볼 수 있다.
    새로 질문하거나 PASS를 완료하면 백그라운드에서 바로 저장되므로, 팝업을 열 때마다
    최신 question_archive가 그대로 반영된다.
    """
    archive = st.session_state.get("question_archive", [])
    if not archive:
        st.info("아직 저장된 질문이 없어요. 질문을 올리면 여기에 차곡차곡 쌓여요!")
        return

    by_date = {}
    for entry in archive:
        date_key = (entry.get("created_at") or "")[:10] or "날짜 미상"
        by_date.setdefault(date_key, []).append(entry)

    for date_key in sorted(by_date.keys(), reverse=True):
        st.markdown(f"#### 🗓️ {date_key}")
        day_entries = sorted(by_date[date_key], key=lambda e: e.get("created_at", ""), reverse=True)
        for entry in day_entries:
            with st.container(border=True):
                st.caption(f"{entry.get('feature', '')} · {entry.get('created_at', '')}")
                for msg in entry.get("messages", []):
                    _render_clinic_message(msg)
        st.markdown("")


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
        "너는 알찬학원의 '오늘의 개념 PASS' 테스트를 담당하는, 깐깐하지만 친절한 조교다.\n"
        f"★ [학생 학년: {pass_grade}]\n"
        f"★ [🚨 오늘 선생님이 지정한 범위 - 반드시 이 범위 안에서만 출제할 것]: {scope_text}\n"
        "- 위 지정 범위를 벗어난 다른 단원이나 개념은 절대 묻지 마라. 학생이 다른 단원 이야기를 꺼내도 지정 범위로 다시 데려와라.\n\n"
        "★ [1. 질문 던지기]\n"
        "- 위 지정 범위의 핵심 개념이나 자주 틀리는 오개념을 확인할 수 있는 질문을 오직 1개만 던져라.\n"
        "- 질문 문장 안에 답이나 핵심 키워드를 절대 미리 흘리지 마라.\n"
        "- 가능하면 '오늘 선생님이 칠판에 적어주신 내용 중 ~에 대해 설명해 보세요' 같은 식으로, 오늘 수업에서 "
        "직접 배운 내용을 짚어주는 듯한 말투로 출제해라.\n\n"
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
if "question_archive" not in st.session_state:
    st.session_state.question_archive = []
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
if "pass_grade" not in st.session_state:
    st.session_state.pass_grade = ""
if "teacher_mode" not in st.session_state:
    st.session_state.teacher_mode = False
if "selected_mode" not in st.session_state:
    st.session_state.selected_mode = MODE_OPTIONS[0]

st.title("✏️ 알찬학원 신다혜 쌤의 1:1 수학 클리닉")

# ==========================================
# 🕵️ 시크릿 선생님 모드 (사이드바 맨 아래, 학생 화면은 그대로 평범한 접힌 사이드바)
# 비밀번호가 맞으면 학생 로그인 절차 없이 바로 대시보드로 진입한다.
# (로그아웃 버튼도 여기 둔다 - 메인 화면에는 로그인 상태를 알리는 텍스트를 일절 띄우지 않는다)
# ==========================================
with st.sidebar:
    if st.session_state.authenticated:
        if st.button("🔓 로그아웃", use_container_width=True, key="sidebar_logout_btn"):
            persist_current_student()
            for key in [
                "authenticated", "student_name", "kakao_id", "kakao_needs_grade",
                "clinic_messages", "review_messages", "clinic_last_upload_key", "review_last_upload_key",
                "clinic_archive_entry_id", "review_archive_entry_id", "question_archive",
                "slack_thread_ts", "pass_topic", "pass_messages", "pass_completed", "pass_records",
                "pass_concept_summary", "pass_completed_date", "pass_card_bytes", "pass_grade",
            ]:
                st.session_state.pop(key, None)
            st.rerun()

    st.markdown("<div style='height: 55vh;'></div>", unsafe_allow_html=True)
    teacher_pw_input = st.text_input("선생님 모드", type="password", key="teacher_pw_input")
    if teacher_pw_input and teacher_pw_input == TEACHER_DASHBOARD_PASSWORD:
        st.session_state.teacher_mode = True

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

            saved = load_student_data(kakao_id)
            if saved and saved.get("student_grade"):
                # 이미 과목을 선택해 둔 적이 있는 학생 → 바로 입장.
                # 단, 질문 클리닉/오답 첨삭 화면은 매번 백지로 시작한다 (콴다처럼) -
                # clinic_messages/review_messages는 일부러 복원하지 않는다. 지난 질문은
                # question_archive에 그대로 남아 있어서 '나의 오답노트' 팝업에서 볼 수 있다.
                st.session_state.student_grade = saved["student_grade"]
                st.session_state.question_archive = saved.get("question_archive", [])
                st.session_state.pass_topic = saved.get("pass_topic")
                st.session_state.pass_messages = saved.get("pass_messages", [])
                st.session_state.pass_completed = saved.get("pass_completed", False)
                st.session_state.pass_records = saved.get("pass_records", [])
                st.session_state.pass_concept_summary = saved.get("pass_concept_summary", "")
                st.session_state.pass_completed_date = saved.get("pass_completed_date", "")
                st.session_state.pass_grade = saved.get("pass_grade", "")
                st.session_state.pop("pass_card_bytes", None)
                st.session_state.authenticated = True
            else:
                # 첫 로그인 → 과목 선택 한 번만 더 받는다 (이전 세션의 흔적이 섞이지 않게 초기화)
                st.session_state.clinic_messages = []
                st.session_state.review_messages = []
                st.session_state.question_archive = []
                st.session_state.pass_topic = None
                st.session_state.pass_messages = []
                st.session_state.pass_completed = False
                st.session_state.pass_records = []
                st.session_state.pass_concept_summary = ""
                st.session_state.pass_completed_date = ""
                st.session_state.pass_grade = ""
                st.session_state.pop("pass_card_bytes", None)
                st.session_state.kakao_needs_grade = True
        except Exception as e:
            st.session_state.kakao_login_error = "😥 카카오 로그인에 실패했어요. 잠시 후 다시 시도해 주세요."
            st.session_state.kakao_login_error_detail = str(e)
    st.rerun()

if not st.session_state.authenticated and "error" in st.query_params:
    st.query_params.clear()
    st.session_state.kakao_login_error = "카카오 로그인이 취소되었습니다."
    st.rerun()

if st.session_state.authenticated:
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

    archive_col1, archive_col2 = st.columns([3, 1])
    with archive_col2:
        if st.button("📁 나의 지난 질문 & 오답노트", use_container_width=True, key="open_archive_btn"):
            render_question_archive_dialog()


def render_login_required_gate(feature_label):
    """로그인 전 상태에서 각 탭 내부에 뜨는 안내.

    카카오 인증까지는 끝났지만 과목을 아직 안 고른 학생에게는(kakao_needs_grade)
    학년 선택 1회 단계를 보여주고, 그 외에는 안내 문구 + 오리지널 카카오 로그인 버튼을 보여준다.
    탭 3개가 동시에 이 함수를 호출할 수 있으므로, 위젯 key는 반드시 feature_label로 구분한다.
    """
    if st.session_state.kakao_login_error:
        st.error(st.session_state.kakao_login_error)
        if st.session_state.get("kakao_login_error_detail"):
            with st.expander("기술적인 오류 내용 보기"):
                st.code(st.session_state.kakao_login_error_detail)
        st.session_state.kakao_login_error = None
        st.session_state.kakao_login_error_detail = None

    if st.session_state.kakao_needs_grade:
        st.markdown(
            f"""
            <div class="guide-box">
                <div class="guide-title">👋 {st.session_state.student_name} 님, 반가워요!</div>
                <div class="guide-item">2022 개정 교육과정에 맞춘 과목을 선택하고 시작해 주세요. (최초 1회만 선택)</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        input_grade = st.selectbox(
            "학년 / 과목 선택 (2022 개정 교육과정):",
            ["중2", "중3", "공통수학1", "공통수학2", "대수", "미적분1", "미적분2", "확률과 통계", "기하와 벡터"],
            key=f"grade_select_{feature_label}",
        )
        if st.button("🚀 시작하기", key=f"grade_start_{feature_label}", use_container_width=True):
            st.session_state.student_grade = input_grade
            st.session_state.authenticated = True
            st.session_state.kakao_needs_grade = False
            persist_current_student()
            st.rerun()
    else:
        st.markdown(
            f"""
            <div class="guide-box" style="text-align:center;">
                <div class="guide-title">🔒 로그인이 필요해요</div>
                <div class="guide-item"><b>{feature_label}</b> 기능을 사용하려면 카카오로 로그인해주세요.</div>
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

    messages_key = f"{state_prefix}_messages"
    upload_key_key = f"{state_prefix}_last_upload_key"
    system_prompt = load_system_prompt(st.session_state.student_grade)

    uploaded_solution = None
    if with_solution_uploader:
        up_c1, up_c2 = st.columns(2)
        with up_c1:
            uploaded_problem = st.file_uploader(
                "1️⃣ 문제 사진 (또는 문제+풀이 사진)", type=["jpg", "jpeg", "png"], key=f"{state_prefix}_prob"
            )
        with up_c2:
            uploaded_solution = st.file_uploader(
                "2️⃣ 연습장 풀이 사진 (선택)", type=["jpg", "jpeg", "png"], key=f"{state_prefix}_sol"
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

    if api_key and uploaded_problem and curr_upload_key != st.session_state.get(upload_key_key):
        st.session_state[upload_key_key] = curr_upload_key
        st.session_state[messages_key] = []
        # 새 사진 = 새 질문 → 오답노트에도 새 카드를 만든다 (이전 카드를 계속 수정하지 않도록 끊어준다).
        st.session_state[f"{state_prefix}_archive_entry_id"] = None

        client = anthropic.Anthropic(api_key=api_key)
        p_bytes = uploaded_problem.getvalue()

        if with_solution_uploader:
            send_telegram_photo(p_bytes, st.session_state.student_name, f"{mode} - 문제")
            content_blocks = [{
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": uploaded_problem.type,
                    "data": base64.b64encode(p_bytes).decode("utf-8"),
                },
            }]
            images_submitted = [{"bytes": p_bytes, "mime": uploaded_problem.type}]
            label = "📷 문제 사진을 올렸어요!"
            if uploaded_solution:
                s_bytes = uploaded_solution.getvalue()
                send_telegram_photo(s_bytes, st.session_state.student_name, f"{mode} - 풀이")
                content_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": uploaded_solution.type,
                        "data": base64.b64encode(s_bytes).decode("utf-8"),
                    },
                })
                images_submitted.append({"bytes": s_bytes, "mime": uploaded_solution.type})
                label = "📷 문제 + 풀이 사진을 올렸어요!"
                api_text = (
                    f"[모드: {mode} / 과목: {st.session_state.student_grade}] 문제 사진과 연습장 손글씨 풀이 사진 2장이 "
                    "전달되었어. 첫 줄부터 줄 단위로 연산을 검증해서 잘한 점과 부호/계산 실수가 발생한 정확한 위치를 "
                    "다정하게 지적해줘."
                )
            else:
                api_text = (
                    f"[모드: {mode} / 과목: {st.session_state.student_grade}] 이 사진 안의 손글씨 풀이를 첫 줄부터 "
                    "줄 단위로 검증하여 잘한 점과 연산 실수가 발생한 정확한 위치를 콕 짚어라."
                )
            content_blocks.append({"type": "text", "text": api_text})
            spinner_text = "손글씨 풀이를 줄 단위로 분석하는 중입니다..."
            log_label = "풀이 검토 사진 제출"
        else:
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
            spinner_text = "문제를 꼼꼼하게 살피는 중입니다..."
            log_label = "문제 사진 업로드 제출"

        with st.spinner(spinner_text):
            try:
                response = client.messages.create(
                    model=MODEL_NAME,
                    max_tokens=2000,
                    system=system_prompt,
                    messages=[{"role": "user", "content": content_blocks}],
                )
                bot_reply = get_response_text(response)
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
            if st.button("🔓 80% 풀이 바로 보기", use_container_width=True, type="primary", key=f"{state_prefix}_80_btn"):
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


def render_pass_tab():
    st.markdown("### 💮 개념 PASS")

    if not st.session_state.authenticated:
        render_login_required_gate("💮 개념 PASS")
        return

    st.caption("선생님이 오늘 정해준 범위 안에서, 핵심 개념이나 오개념을 확인하는 질문을 던져줄게.")

    st.markdown(
        f'<div class="pass-date-banner">'
        f'<span class="pass-date-text">📅 {get_today_kst_label()} - 오늘의 개념 미션</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    render_stamp_board(st.session_state.pass_records)
    st.markdown("---")

    pass_col_main, pass_col_history = st.columns([3, 1])

    with pass_col_main:
        if not st.session_state.pass_topic:
            default_index = (
                PASS_GRADE_LEVELS.index(st.session_state.pass_grade)
                if st.session_state.pass_grade in PASS_GRADE_LEVELS
                else 0
            )
            selected_grade = st.selectbox(
                "자신의 학년을 선택해줘",
                PASS_GRADE_LEVELS,
                index=default_index,
                key="pass_grade_select",
            )
            st.session_state.pass_grade = selected_grade

            today_scope = load_daily_pass_scope().get(selected_grade, "")

            if today_scope:
                st.info(f"📌 **오늘의 미션: {selected_grade} - {today_scope}**")
            else:
                st.warning("오늘의 지정된 개념이 없습니다. 선생님이 범위를 등록하면 도전할 수 있어요!")

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
                    st.session_state.pass_concept_summary = ""
                    st.session_state.pass_completed_date = ""
                    st.session_state.pop("pass_card_bytes", None)
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
                                    system=load_pass_system_prompt(st.session_state.pass_grade, st.session_state.pass_topic),
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
                                show_friendly_error(e)

    with pass_col_history:
        with st.expander("📝 이전 질문/기록 보기", expanded=False):
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

if st.session_state.selected_mode == "❓ 질문 클리닉":
    render_question_clinic_tab()
elif st.session_state.selected_mode == "🔍 오답 첨삭":
    render_mistake_review_tab()
elif st.session_state.selected_mode == "💮 개념 PASS":
    render_pass_tab()
