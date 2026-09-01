import base64
import re
import ast
import requests
from datetime import datetime, timezone, timedelta
import anthropic
import streamlit as st
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np

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


def log_to_slack_and_gsheet(student_name, mode, student_text, ai_reply):
    slack_url = st.secrets.get("SLACK_WEBHOOK_URL", "")
    gsheet_url = st.secrets.get("GSHEET_WEBHOOK_URL", "")

    kst = timezone(timedelta(hours=9))
    now_str = datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S")

    if slack_url:
        try:
            slack_payload = {
                "text": f"🔔 *[알찬학원 수학 클리닉] 새로운 질문 접수*\n"
                        f"• *학생 이름*: {student_name}\n"
                        f"• *시각*: {now_str}\n"
                        f"• *모드*: {mode}\n"
                        f"• *학생 입력*: {student_text if student_text else '(사진 업로드)'}\n\n"
                        f"🤖 *다혜 쌤 피드백/질문*:\n{ai_reply[:300]}..."
            }
            requests.post(slack_url, json=slack_payload, timeout=3)
        except Exception:
            pass

    if gsheet_url:
        try:
            gsheet_payload = {
                "timestamp": now_str,
                "student_name": student_name,
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

    html, body, .stApp {
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }

    [data-testid="stIcon"], i, [class*="icon"], [class*="material"] {
        font-family: inherit !important;
    }

    .stApp {
        background-color: #F8FAFC;
    }
    
    [data-testid="stSidebar"] {
        display: none;
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

    /* 💡 메인 2개 모드 선택 대형 카드 버튼 (PC/기본) */
    div[data-testid="stKey-btn_mode1"] button,
    div[data-testid="stKey-btn_mode2"] button {
        width: 100% !important;
        min-height: 240px !important;
        white-space: pre-wrap !important;
        word-break: keep-all !important;
        font-size: 1.1rem !important;
        line-height: 1.75 !important;
        border-radius: 18px !important;
        border: 2.5px solid #3A449A !important;
        background-color: #FFFFFF !important;
        color: #334155 !important;
        box-shadow: 0 6px 16px rgba(58, 68, 154, 0.08) !important;
        text-align: center !important;
        padding: 1.8rem 1.2rem !important;
        transition: all 0.25s ease-in-out !important;
    }

    /* 첫 줄(제목) 강조 */
    div[data-testid="stKey-btn_mode1"] button p::first-line,
    div[data-testid="stKey-btn_mode2"] button p::first-line,
    div[data-testid="stKey-btn_mode1"] button div::first-line,
    div[data-testid="stKey-btn_mode2"] button div::first-line {
        font-size: 1.45rem !important;
        font-weight: 800 !important;
        color: #3A449A !important;
    }

    div[data-testid="stKey-btn_mode1"] button:hover,
    div[data-testid="stKey-btn_mode2"] button:hover {
        border-color: #00A19D !important;
        background-color: #F0FDFA !important;
        box-shadow: 0 10px 24px rgba(0, 161, 157, 0.18) !important;
        transform: translateY(-3px) !important;
    }

    /* 📱 핸드폰(모바일) 화면 전용 스타일 (768px 이하) */
    @media (max-width: 768px) {
        div[data-testid="stKey-btn_mode1"] button,
        div[data-testid="stKey-btn_mode2"] button {
            min-height: 210px !important;
            padding: 1.5rem 1rem !important;
            font-size: 1.05rem !important;
            line-height: 1.65 !important;
            margin-bottom: 0.8rem !important;
        }

        div[data-testid="stKey-btn_mode1"] button p::first-line,
        div[data-testid="stKey-btn_mode2"] button p::first-line,
        div[data-testid="stKey-btn_mode1"] button div::first-line,
        div[data-testid="stKey-btn_mode2"] button div::first-line {
            font-size: 1.35rem !important;
            font-weight: 800 !important;
            color: #3A449A !important;
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

    - import 문 금지
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


def render_assistant_content(content):
    pattern = r"```python\s*(.*?)\s*```"
    parts = re.split(pattern, content, flags=re.DOTALL)

    for i, part in enumerate(parts):
        if i % 2 == 0:
            if part.strip():
                st.markdown(part)
        else:
            code = part.strip()
            if ("plt." in code or "fig" in code) and is_safe_matplotlib_code(code):
                try:
                    plt.close("all")
                    local_scope = {"plt": plt, "np": np, "patches": patches}
                    exec(code, {"__builtins__": {}}, local_scope)
                    fig = local_scope.get("fig", plt.gcf())
                    if fig and len(fig.axes) > 0:
                        st.pyplot(fig)
                        plt.close("all")
                except Exception:
                    pass


def load_system_prompt():
    default_prompt = (
        "너는 친절하고 실력 있는 알찬학원 수학 강사 신다혜 선생님이다.\n\n"
        "[강력 수식 & LaTeX 규칙 - 필수 준수]\n"
        "1. 모든 수학 공식, 수식, 변수(x, y, a, b 등), 숫자 식, 방정식, 기호는 예외 없이 100% LaTeX 표기법(`$ ... $` 또는 `$$ ... $$`)으로 작성해라.\n"
        "2. 분수를 작성할 때 가로 형태(1/2, a/b)는 절대 사용하지 말고, 반드시 문제집처럼 세로 분수 형태인 `\\dfrac{a}{b}`를 사용해라.\n"
        "3. 원의 방정식, 도형/기하 문제 시 반드시 무조건 matplotlib 도형/원 시각화 코드를 생성해라."
    )
    try:
        with open("system_prompt.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return default_prompt


if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "student_name" not in st.session_state:
    st.session_state.student_name = ""

st.title("✏️ 알찬학원 신다혜 쌤의 1:1 수학 클리닉")

if not st.session_state.authenticated:
    st.markdown("---")
    st.subheader("🔒 수강생 입장하기")
    st.caption("학생 본인의 이름과 선생님이 안내한 비밀번호를 입력해 주세요.")

    input_name = st.text_input("학생 이름 (예: 김철수):")
    input_pw = st.text_input("비밀번호:", type="password")

    if st.button("🔓 클리닉 입장하기", use_container_width=True):
        clean_name = input_name.strip()
        if not clean_name:
            st.error("이름을 입력해 주세요!")
        elif input_pw == MASTER_PW or input_pw == "1234":
            st.session_state.authenticated = True
            st.session_state.student_name = clean_name
            st.success(f"{clean_name} 학생, 환영합니다! 공부를 시작해 볼까요?")
            st.rerun()
        else:
            st.error("비밀번호가 올바르지 않습니다. 다혜 쌤에게 문의해 주세요!")
    st.stop()

if "messages" not in st.session_state:
    st.session_state.messages = []
if "selected_mode" not in st.session_state:
    st.session_state.selected_mode = None
if "last_upload_key" not in st.session_state:
    st.session_state.last_upload_key = None

system_prompt = load_system_prompt()

st.caption(f"👤 현재 접속 학생: **{st.session_state.student_name}**")

if st.session_state.selected_mode is None:
    st.markdown(
        """
    <div class="guide-box">
        <div class="guide-title">💡 클리닉 이용 안내</div>
        <div class="guide-item">❓ <b>풀이가 막혔어요:</b> 문제 사진 1장을 올리면, 정답 대신 스스로 답을 찾아갈 수 있게 다혜 쌤이 질문을 던져줍니다.</div>
        <div class="guide-item">✍️ <b>내 풀이 검토:</b> 사진 1장(문제+풀이 함께 기록) 또는 사진 2장을 올리면 잘한 부분과 실수한 오답 지점을 콕 짚어줍니다.</div>
        <div class="guide-item">📐 <b>수식 & 도형/원 그림:</b> 모든 수식은 세로 분수($\dfrac{a}{b}$), 원의 방정식 및 도형 문제 시 직관적인 그림 자동 생성!</div>
    </div>
    """,
        unsafe_allow_html=True,
    )

    st.markdown("### 💡 어떤 도움이 필요하신가요?")
    st.caption("아래 카드 중 원하는 진단 방식을 선택해 주세요.")

    col1, col2 = st.columns(2)

    with col1:
        btn1_text = (
            "❓ 풀이가 막혔어요\n"
            "(스스로 풀어보기)\n\n"
            "📷 문제 사진 1장 필요\n"
            "정답 대신 스스로 답을 찾아갈 수 있게\n"
            "다혜 쌤이 차근차근 질문을 던져줄게요!"
        )
        if st.button(btn1_text, key="btn_mode1", use_container_width=True):
            st.session_state.selected_mode = "❓ 스스로 풀어보기 (차근차근 질문)"
            st.rerun()

    with col2:
        btn2_text = (
            "✍️ 내 풀이 검토\n"
            "(연습장 오답 클리닉)\n\n"
            "📷 사진 1장 또는 2장 가능\n"
            "문제와 풀이가 한 사진에 다 적혀있다면\n"
            "사진 1장만 올려도 오답을 검토해 줄게요!"
        )
        if st.button(btn2_text, key="btn_mode2", use_container_width=True):
            st.session_state.selected_mode = "✍️ 내 풀이 검토 (문제 + 연습장)"
            st.rerun()

    st.stop()

mode = st.session_state.selected_mode

top_col1, top_col2 = st.columns([3, 1])
with top_col1:
    st.markdown(f"#### 📌 선택한 모드: **{mode}**")
with top_col2:
    if st.button("🏠 첫 화면으로 (홈)", use_container_width=True):
        st.session_state.selected_mode = None
        st.session_state.messages = []
        st.session_state.last_upload_key = None
        st.rerun()

st.markdown("---")

uploaded_problem = None
uploaded_solution = None

if mode == "❓ 스스로 풀어보기 (차근차근 질문)":
    st.caption("📷 **문제 사진 1장**을 업로드해 주세요.")
    uploaded_problem = st.file_uploader(
        "문제 사진 첨부", type=["jpg", "jpeg", "png"], key="prob_only"
    )
else:
    st.caption("📷 **사진을 첨부해 주세요.** (한 사진에 문제와 풀이가 함께 있다면 1번 상자에만 1장 올려주세요!)")
    up_col1, up_col2 = st.columns(2)
    with up_col1:
        uploaded_problem = st.file_uploader(
            "1️⃣ 문제 사진 (또는 문제+풀이 사진)", type=["jpg", "jpeg", "png"], key="prob_dual"
        )
    with up_col2:
        uploaded_solution = st.file_uploader(
            "2️⃣ 연습장 풀이 사진 (선택 사항)", type=["jpg", "jpeg", "png"], key="sol_dual"
        )

prob_id = uploaded_problem.file_id if uploaded_problem else "none"
sol_id = uploaded_solution.file_id if uploaded_solution else "none"
curr_upload_key = f"{mode}_{prob_id}_{sol_id}"

MODEL_NAME = "claude-sonnet-4-5"

if api_key and (curr_upload_key != st.session_state.last_upload_key):
    if mode == "❓ 스스로 풀어보기 (차근차근 질문)" and uploaded_problem:
        st.session_state.last_upload_key = curr_upload_key
        st.session_state.messages = []

        client = anthropic.Anthropic(api_key=api_key)
        uploaded_problem.seek(0)
        p_bytes = uploaded_problem.read()

        content_blocks = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": uploaded_problem.type,
                    "data": base64.b64encode(p_bytes).decode("utf-8"),
                },
            },
            {
                "type": "text",
                "text": "[모드: 스스로 풀어보기] 학생이 풀다가 막혀서 문제 사진을 올렸어. 정답이나 전체 풀이를 바로 주지 말고, 스스로 고민해서 답을 찾을 수 있게 첫 번째 개념 질문만 건네줘. (특히 원의 방정식, 도형의 방정식, 기하 문제는 100% 무조건 좌표평면에 원, 중심점, 반지름, 접선/직선 등을 보일 수 있는 matplotlib 그림 코드를 예외 없이 함께 출력해라)",
            },
        ]

        with st.spinner("다혜 쌤이 문제를 꼼꼼하게 살피는 중입니다..."):
            try:
                response = client.messages.create(
                    model=MODEL_NAME,
                    max_tokens=2000,
                    system=system_prompt,
                    messages=[{"role": "user", "content": content_blocks}],
                )
                bot_reply = get_response_text(response)
                st.session_state.messages.append(
                    {"role": "assistant", "content": bot_reply}
                )
                log_to_slack_and_gsheet(st.session_state.student_name, mode, "문제 사진 업로드 제출", bot_reply)
                st.rerun()
            except Exception as e:
                st.error(f"오류가 발생했습니다: {e}")

    elif mode == "✍️ 내 풀이 검토 (문제 + 연습장)" and uploaded_problem:
        st.session_state.last_upload_key = curr_upload_key
        st.session_state.messages = []

        client = anthropic.Anthropic(api_key=api_key)

        uploaded_problem.seek(0)
        p_bytes = uploaded_problem.read()

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
            uploaded_solution.seek(0)
            s_bytes = uploaded_solution.read()
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
            prompt_guide = "[모드: 내 풀이 검토] 문제 사진과 연습장 풀이 사진 2장이 전달되었어. 연습장 풀이를 대조해서 잘 접근한 부분과 실수한 오답 지점을 지적해줘. (특히 원의 방정식, 도형의 방정식, 기하 문제는 100% 무조건 좌표평면에 원, 중심점, 반지름, 접선/직선 등을 보일 수 있는 matplotlib 그림 코드를 예외 없이 함께 출력해라)"
        else:
            prompt_guide = "[모드: 내 풀이 검토] 이 사진 한 장 안에 문제와 학생이 직접 적은 풀이가 함께 들어있어. 인쇄된 문제와 학생의 손글씨 풀이를 함께 확인하여 잘 접근한 부분과 실수한 오답 지점을 콕 짚어줘. (특히 원의 방정식, 도형의 방정식, 기하 문제는 100% 무조건 좌표평면에 원, 중심점, 반지름, 접선/직선 등을 보일 수 있는 matplotlib 그림 코드를 예외 없이 함께 출력해라)"

        content_blocks.append({"type": "text", "text": prompt_guide})

        with st.spinner("다혜 쌤이 문제와 풀이를 대조 분석하는 중입니다..."):
            try:
                response = client.messages.create(
                    model=MODEL_NAME,
                    max_tokens=2000,
                    system=system_prompt,
                    messages=[{"role": "user", "content": content_blocks}],
                )
                bot_reply = get_response_text(response)
                st.session_state.messages.append(
                    {"role": "assistant", "content": bot_reply}
                )
                log_to_slack_and_gsheet(st.session_state.student_name, mode, "풀이 검토 사진 제출", bot_reply)
                st.rerun()
            except Exception as e:
                st.error(f"오류가 발생했습니다: {e}")

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant":
            render_assistant_content(msg["content"])
        else:
            st.markdown(msg["content"])

if prompt := st.chat_input("다혜 쌤에게 답장하기 (예: 정답은 \\dfrac{1}{2} 같아요!)"):
    if not api_key:
        st.error("API 키가 설정되지 않았습니다.")
        st.stop()

    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    client = anthropic.Anthropic(api_key=api_key)
    api_messages = []

    for idx, msg in enumerate(st.session_state.messages):
        if idx == 0:
            if mode == "❓ 스스로 풀어보기 (차근차근 질문)" and uploaded_problem:
                uploaded_problem.seek(0)
                content_blocks = [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": uploaded_problem.type,
                            "data": base64.b64encode(uploaded_problem.read()).decode("utf-8"),
                        },
                    },
                    {"type": "text", "text": msg["content"]},
                ]
                api_messages.append({"role": "user", "content": content_blocks})
            elif mode == "✍️ 내 풀이 검토 (문제 + 연습장)" and uploaded_problem:
                uploaded_problem.seek(0)
                content_blocks = [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": uploaded_problem.type,
                            "data": base64.b64encode(uploaded_problem.read()).decode("utf-8"),
                        },
                    }
                ]
                if uploaded_solution:
                    uploaded_solution.seek(0)
                    content_blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": uploaded_solution.type,
                                "data": base64.b64encode(uploaded_solution.read()).decode("utf-8"),
                            },
                        }
                    )
                content_blocks.append({"type": "text", "text": msg["content"]})
                api_messages.append({"role": "user", "content": content_blocks})
            else:
                api_messages.append({"role": msg["role"], "content": msg["content"]})
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
                st.session_state.messages.append(
                    {"role": "assistant", "content": bot_reply}
                )
                log_to_slack_and_gsheet(st.session_state.student_name, mode, prompt, bot_reply)
            except Exception as e:
                st.error(f"오류가 발생했습니다: {e}")
