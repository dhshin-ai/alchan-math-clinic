import base64
import re
import anthropic
import streamlit as st

st.set_page_config(
    page_title="알찬학원 신다혜 쌤의 1:1 수학 클리닉",
    page_icon="✏️",
    layout="wide",
    initial_sidebar_state="collapsed",  # 사이드바 완전 제거
)

# API 키 및 인증 코드 로드
try:
    api_key = st.secrets["ANTHROPIC_API_KEY"]
except Exception:
    api_key = None

try:
    STUDENT_CODE = st.secrets["STUDENT_CODE"]
except Exception:
    STUDENT_CODE = "alchan1234"


# 🎨 커스텀 CSS (선명한 디자인 + 사이드바 숨김 + 반응형 수식)
def inject_custom_css():
    st.markdown(
        '''
    <style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');

    html, body, [class*="st-"] {
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
    }

    .stApp {
        background-color: #F8FAFC;
    }

    /* 사이드바 완전히 숨기기 */
    [data-testid="stSidebar"] {
        display: none;
    }

    h1, h2, h3 {
        color: #3A449A !important;
        font-weight: 800 !important;
        word-break: keep-all !important;
    }

    h1 {
        font-size: clamp(1.25rem, 3.5vw, 1.75rem) !important;
        line-height: 1.35 !important;
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

    .stButton > button {
        background-color: #FFFFFF !important;
        color: #1F2937 !important;
        border: 1px solid #CBD5E1 !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05) !important;
    }

    /* 이용 안내 박스 (복원) */
    .guide-box {
        background-color: #FFFFFF;
        border: 2px solid #3A449A;
        border-radius: 12px;
        padding: 1rem 1.2rem;
        margin: 0.5rem 0 1.5rem;
        box-shadow: 0 4px 10px rgba(58, 68, 154, 0.05);
    }
    .guide-title {
        color: #3A449A;
        font-weight: 800;
        font-size: 1.05rem;
        margin-bottom: 0.5rem;
    }
    .guide-item {
        color: #1E293B;
        font-size: 0.95rem;
        margin-bottom: 0.3rem;
        line-height: 1.5;
    }

    /* 모드 선택: 카드 전체가 하나의 큰 클릭 버튼 (두 장 형태·높이 통일) */
    .st-key-btn_mode1 .stButton > button,
    .st-key-btn_mode2 .stButton > button,
    .st-key-btn_mode1 button,
    .st-key-btn_mode2 button {
        white-space: pre-line !important;
        min-height: 210px !important;
        height: 100% !important;
        border: 2px solid #E2E8F0 !important;
        border-radius: 14px !important;
        padding: 1.5rem 1.25rem !important;
        font-size: 1rem !important;
        line-height: 1.6 !important;
        text-align: center !important;
        box-shadow: 0 2px 8px rgba(0,0,0,0.04) !important;
    }
    .st-key-btn_mode1 .stButton > button:hover,
    .st-key-btn_mode2 .stButton > button:hover,
    .st-key-btn_mode1 button:hover,
    .st-key-btn_mode2 button:hover {
        border-color: #00A19D !important;
        background-color: #F0FDFA !important;
        color: #0F172A !important;
    }

    .katex-display {
        background-color: #F1F5F9;
        padding: 0.5rem;
        border-radius: 8px;
        color: #000000 !important;
    }
    </style>
    ''',
        unsafe_allow_html=True,
    )


inject_custom_css()


# 안전하게 response 텍스트만 추출하는 함수
def get_response_text(response):
    text_parts = []
    for block in response.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
    full_text = "\n".join(text_parts)
    cleaned = re.sub(r"<thinking>.*?</thinking>", "", full_text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<thinking>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


def load_system_prompt():
    default_prompt = (
        "너는 친절하고 실력 있는 알찬학원 수학 강사 신다혜 선생님이다.\n\n"
        "[강력 수식 & LaTeX 규칙 - 필수 준수]\n"
        "1. 모든 수학 공식, 수식, 변수(x, y, a, b 등), 숫자 식, 방정식, 기호는 예외 없이 100% LaTeX 표기법(`$ ... $` 또는 `$$ ... $$`)으로 작성해라.\n"
        "2. 분수를 작성할 때 가로 형태(1/2, a/b)는 절대 사용하지 말고, 반드시 문제집처럼 세로 분수 형태인 `\\dfrac{a}{b}`를 사용해라.\n"
        "   - 예시: 1/2 대신 $\\dfrac{1}{2}$, 3/4 대신 $\\dfrac{3}{4}$\n"
        "3. 학생이 스스로 생각할 수 있도록 친근한 다혜 쌤 톤으로 차근차근 질문을 건네며 이끌어줘라."
    )
    try:
        with open("system_prompt.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return default_prompt


if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

st.title("✏️ 알찬학원 신다혜 쌤의 1:1 수학 클리닉")

# 이용 안내 박스
st.markdown(
    '''
<div class="guide-box">
    <div class="guide-title">💡 클리닉 이용 안내 (필요한 모드를 선택해 주세요!)</div>
    <div class="guide-item">❓ <b>풀이가 막혔어요:</b> 문제 사진 1장만 올리면 다혜 쌤이 스텝별 유도 질문으로 도와줘요.</div>
    <div class="guide-item">✍️ <b>내 풀이 검토:</b> 문제 사진 + 연습장 풀이 사진 2장을 올리면 오답 지점을 콕 짚어줘요.</div>
</div>
''',
    unsafe_allow_html=True,
)

if not st.session_state.authenticated:
    st.markdown("---")
    st.subheader("🔒 알찬학원 수강생 전용 로그인")
    input_code = st.text_input(
        "학원 전용 비밀번호를 입력해 주세요:", type="password"
    )

    if st.button("클리닉 입장하기"):
        if input_code == STUDENT_CODE:
            st.session_state.authenticated = True
            st.success("인증되었습니다! 다혜 쌤과의 공부를 시작해 보세요.")
            st.rerun()
        else:
            st.error("비밀번호가 올바르지 않습니다. 신다혜 쌤에게 문의해 주세요!")
    st.stop()

# 세션 상태 초기화
if "messages" not in st.session_state:
    st.session_state.messages = []
if "selected_mode" not in st.session_state:
    st.session_state.selected_mode = None
if "last_upload_key" not in st.session_state:
    st.session_state.last_upload_key = None

system_prompt = load_system_prompt()

# -------------------------------------------------------------------
# 1단계: 첫 페이지 모드 선택 (메인 화면 카드)
# -------------------------------------------------------------------
if st.session_state.selected_mode is None:
    st.markdown("### 💡 어떤 도움이 필요하신가요?")
    st.caption("아래에서 원하는 방식을 선택해 주세요.")

    col1, col2 = st.columns(2)

    with col1:
        if st.button(
            "❓ 풀이가 막혔어요\n\n"
            "문제 사진 1장만 올려주세요!\n"
            "정답을 바로 주는 대신, 스스로 답을 찾아갈 수 있게\n"
            "다혜 쌤이 차근차근 질문을 던져줄게요.",
            use_container_width=True,
            key="btn_mode1",
        ):
            st.session_state.selected_mode = "❓ 스스로 풀어보기 (차근차근 질문)"
            st.rerun()

    with col2:
        if st.button(
            "✍️ 내 풀이 검토\n\n"
            "문제 + 연습장 풀이 2장을 올려주세요!\n"
            "잘 접근한 부분과 계산·개념이 삐끗한\n"
            "오답 지점을 콕 짚어 줄게요.",
            use_container_width=True,
            key="btn_mode2",
        ):
            st.session_state.selected_mode = "✍️ 내 풀이 검토 (문제 + 연습장)"
            st.rerun()

    st.stop()

# -------------------------------------------------------------------
# 2단계: 모드 선택 후 (사진 업로드 + 대화)
# -------------------------------------------------------------------
mode = st.session_state.selected_mode

top_col1, top_col2 = st.columns([3, 1])
with top_col1:
    st.markdown(f"#### 📌 선택한 모드: **{mode}**")
with top_col2:
    if st.button("🏠 처음으로", use_container_width=True, key="btn_home"):
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
    st.caption("📷 **문제 사진**과 **연습장 풀이 사진** 2장을 각각 첨부해 주세요.")
    up_col1, up_col2 = st.columns(2)
    with up_col1:
        uploaded_problem = st.file_uploader(
            "1️⃣ 문제 사진 업로드", type=["jpg", "jpeg", "png"], key="prob_dual"
        )
    with up_col2:
        uploaded_solution = st.file_uploader(
            "2️⃣ 연습장 풀이 사진 업로드", type=["jpg", "jpeg", "png"], key="sol_dual"
        )

prob_id = uploaded_problem.file_id if uploaded_problem else "none"
sol_id = uploaded_solution.file_id if uploaded_solution else "none"
curr_upload_key = f"{mode}_{prob_id}_{sol_id}"

MODEL_NAME = "claude-sonnet-4-5"

# 사진 업로드 시 자동 분석 시작
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
                "text": "[모드: 스스로 풀어보기] 학생이 풀다가 막혀서 문제 사진을 올렸어. 정답이나 전체 풀이를 바로 주지 말고, 스스로 고민해서 답을 찾을 수 있게 첫 번째 질문만 건네줘. (모든 수식/변수/분수는 예외 없이 $ ... $ LaTeX 세로 분수 \\dfrac{a}{b} 사용 필수)",
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
                st.rerun()
            except Exception as e:
                st.error(f"오류가 발생했습니다: {e}")

    elif mode == "✍️ 내 풀이 검토 (문제 + 연습장)" and uploaded_problem and uploaded_solution:
        st.session_state.last_upload_key = curr_upload_key
        st.session_state.messages = []

        client = anthropic.Anthropic(api_key=api_key)

        uploaded_problem.seek(0)
        p_bytes = uploaded_problem.read()

        uploaded_solution.seek(0)
        s_bytes = uploaded_solution.read()

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
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": uploaded_solution.type,
                    "data": base64.b64encode(s_bytes).decode("utf-8"),
                },
            },
            {
                "type": "text",
                "text": "[모드: 내 풀이 검토] 문제 사진과 연습장 풀이 사진이야. 연습장 풀이를 대조해서 잘 접근한 부분과 실수한 오답 지점을 지적해줘. (모든 수식/변수/분수는 예외 없이 $ ... $ LaTeX 세로 분수 \\dfrac{a}{b} 사용 필수)",
            },
        ]

        with st.spinner("다혜 쌤이 문제와 연습장 풀이를 대조 분석하는 중입니다..."):
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
                st.rerun()
            except Exception as e:
                st.error(f"오류가 발생했습니다: {e}")

# 채팅 메시지 출력
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# 대화 입력
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
            elif mode == "✍️ 내 풀이 검토 (문제 + 연습장)" and uploaded_problem and uploaded_solution:
                uploaded_problem.seek(0)
                uploaded_solution.seek(0)
                content_blocks = [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": uploaded_problem.type,
                            "data": base64.b64encode(uploaded_problem.read()).decode("utf-8"),
                        },
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": uploaded_solution.type,
                            "data": base64.b64encode(uploaded_solution.read()).decode("utf-8"),
                        },
                    },
                    {"type": "text", "text": msg["content"]},
                ]
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
                st.markdown(bot_reply)
                st.session_state.messages.append(
                    {"role": "assistant", "content": bot_reply}
                )
            except Exception as e:
                st.error(f"오류가 발생했습니다: {e}")
