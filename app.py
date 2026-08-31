import base64
import re
import anthropic
import streamlit as st

st.set_page_config(
    page_title="알찬학원 신다혜 쌤의 1:1 수학 클리닉",
    page_icon="✏️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# API 키 자동 로드
try:
    api_key = st.secrets["ANTHROPIC_API_KEY"]
except Exception:
    api_key = None

# 알찬학원 학생 전용 인증 코드
try:
    STUDENT_CODE = st.secrets["STUDENT_CODE"]
except Exception:
    STUDENT_CODE = "alchan1234"


# 🎨 커스텀 CSS (피드백 카드 글씨 쨍하게 & 깔끔한 스타일)
def inject_custom_css():
    st.markdown(
        '''
    <style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');

    html, body, [class*="st-"] {
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
    }

    .material-symbols-rounded, .material-icons, [data-testid="stIconMaterial"],
    [data-testid="stSidebarCollapseButton"] span, [data-testid="stSidebarCollapseButton"] svg {
        font-family: 'Material Symbols Rounded', 'Material Icons' !important;
    }

    .stApp {
        background-color: #F8FAFC;
    }

    h1, h2, h3 {
        color: #3A449A !important;
        font-weight: 800 !important;
    }

    section[data-testid="stSidebar"] {
        background-color: #FFFFFF !important;
        border-right: 1px solid #E2E8F0;
    }

    .guide-box {
        background-color: #FFFFFF;
        border: 2px solid #3A449A;
        border-radius: 12px;
        padding: 1rem 1.2rem;
        margin-bottom: 1.5rem;
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
        transition: all 0.2s ease !important;
    }

    .stButton > button:hover {
        border-color: #3A449A !important;
        color: #3A449A !important;
        background-color: #F8FAFC !important;
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


def get_response_text(response):
    """response.content 안에서 TextBlock의 text만 안전하게 추출한다.

    ThinkingBlock 등 text 속성이 없는 블록은 건너뛰므로 인덱싱 에러가 나지 않는다.
    """
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            parts.append(block.text)
    return "\n".join(parts).strip()


def clean_thinking_tags(text):
    cleaned = re.sub(
        r"<thinking>.*?</thinking>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = re.sub(
        r"<thinking>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE
    )
    return cleaned.strip()


def load_system_prompt():
    try:
        with open("system_prompt.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "너는 친절한 수학 강사 신다혜 선생님이다."


if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

st.title("✏️ 알찬학원 신다혜 쌤의 1:1 수학 클리닉")

# 안내 메세지
st.markdown(
    '''
<div class="guide-box">
    <div class="guide-title">💡 클리닉 이용 안내 (필요한 모드를 선택해 주세요!)</div>
    <div class="guide-item">❓ <b>아예 모르겠어요:</b> 문제 사진 1장만 올려주세요! 다혜 쌤이 스텝별 유도 질문으로 풀 수 있게 도와줄게요.</div>
    <div class="guide-item">✍️ <b>내 풀이 검토 (연습장):</b> 문제 사진 + 연습장 풀이 사진 2장을 올려주세요! 정확한 오답 위치를 지적해 줄게요.</div>
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

if "messages" not in st.session_state:
    st.session_state.messages = []
if "last_upload_key" not in st.session_state:
    st.session_state.last_upload_key = None

system_prompt = load_system_prompt()

# 사이드바에서 2개의 모드(탭) 선택
with st.sidebar:
    st.header("📸 문제 & 풀이 업로드")
    mode = st.radio(
        "어떤 진단을 받고 싶나요?",
        ["❓ 아예 모르겠어요 (스텝 튜터링)", "✍️ 내 풀이 검토 (문제 + 연습장)"],
    )

    uploaded_problem = None
    uploaded_solution = None

    if mode == "❓ 아예 모르겠어요 (스텝 튜터링)":
        st.markdown("---")
        st.caption("📌 **문제 사진 1장**을 올려주세요.")
        uploaded_problem = st.file_uploader(
            "문제 사진 업로드", type=["jpg", "jpeg", "png"], key="prob_only"
        )
        if uploaded_problem:
            st.image(uploaded_problem, caption="📷 문제 사진", use_container_width=True)

    else:
        st.markdown("---")
        st.caption("📌 **문제 사진 1장**과 **연습장 풀이 사진 1장**을 각각 올려주세요.")
        uploaded_problem = st.file_uploader(
            "1️⃣ 문제 사진 업로드", type=["jpg", "jpeg", "png"], key="prob_dual"
        )
        if uploaded_problem:
            st.image(uploaded_problem, caption="📷 문제 사진", use_container_width=True)

        uploaded_solution = st.file_uploader(
            "2️⃣ 연습장 풀이 사진 업로드", type=["jpg", "jpeg", "png"], key="sol_dual"
        )
        if uploaded_solution:
            st.image(uploaded_solution, caption="✍️ 연습장 풀이 사진", use_container_width=True)

    st.markdown("---")
    if st.button("🔄 대화 초기화", use_container_width=True):
        st.session_state.messages = []
        st.session_state.last_upload_key = None
        st.rerun()

# 업로드 상태 변경 식별키 생성
prob_id = uploaded_problem.file_id if uploaded_problem else "none"
sol_id = uploaded_solution.file_id if uploaded_solution else "none"
curr_upload_key = f"{mode}_{prob_id}_{sol_id}"

# 새로운 업로드가 발생했을 때 대화 자동 시작
if api_key and (curr_upload_key != st.session_state.last_upload_key):
    if mode == "❓ 아예 모르겠어요 (스텝 튜터링)" and uploaded_problem:
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
                "text": "[모드: 아예 모르겠어요] 학생이 문제를 어떻게 풀어야 할지 몰라 문제 사진 1장만 올렸어. 정답이나 전체 풀이를 바로 알려주지 말고, 신다혜 쌤 톤으로 스텝 1 유도 질문만 전달해줘.",
            },
        ]

        with st.spinner("다혜 쌤이 문제를 꼼꼼하게 살피는 중입니다..."):
            try:
                response = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=2000,
                    system=system_prompt,
                    messages=[{"role": "user", "content": content_blocks}],
                )
                bot_reply = clean_thinking_tags(get_response_text(response))
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
                "text": "[모드: 내 풀이 검토] 첫 번째 사진은 '문제'이고 두 번째 사진은 학생의 '연습장 풀이'야. 연습장 풀이를 대조해서 잘 접근한 부분과 계산/개념이 삐끗한 오답 지점을 정확하게 지적해줘.",
            },
        ]

        with st.spinner("다혜 쌤이 문제와 연습장 풀이를 대조 분석하는 중입니다..."):
            try:
                response = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=2000,
                    system=system_prompt,
                    messages=[{"role": "user", "content": content_blocks}],
                )
                bot_reply = clean_thinking_tags(get_response_text(response))
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

# 답장 입력 처리
if prompt := st.chat_input("다혜 쌤에게 답장하기 (예: 1단계 정답은 18이에요!)"):
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
            if mode == "❓ 아예 모르겠어요 (스텝 튜터링)" and uploaded_problem:
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
                    model="claude-sonnet-4-5",
                    max_tokens=2000,
                    system=system_prompt,
                    messages=api_messages,
                )
                bot_reply = clean_thinking_tags(get_response_text(response))
                st.markdown(bot_reply)
                st.session_state.messages.append(
                    {"role": "assistant", "content": bot_reply}
                )
            except Exception as e:
                st.error(f"오류가 발생했습니다: {e}")
