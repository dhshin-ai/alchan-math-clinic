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

# 알찬학원 학생 전용 인증 코드 (secrets에서 설정하거나 기본값 사용)
try:
    STUDENT_CODE = st.secrets["STUDENT_CODE"]
except Exception:
    STUDENT_CODE = "alchan1234"  # 기본 인증코드 (필요시 변경 가능)


# 🎨 #3A449A 및 #00A19D 커스텀 CSS
def inject_custom_css():
    st.markdown(
        """
    <style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');

    /* 1. 폰트 충돌 방지: 아이콘이 깨지지 않도록 일반 텍스트 요소에만 폰트 적용 */
    html, body, [class*="st-"] {
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
    }

    /* Streamlit 기본 아이콘(화살표, 링크 등) 폰트 강제 원복 */
    .material-symbols-rounded, .material-icons, [data-testid="stIconMaterial"],
    [data-testid="stSidebarCollapseButton"] span, [data-testid="stSidebarCollapseButton"] svg {
        font-family: 'Material Symbols Rounded', 'Material Icons' !important;
    }

    /* 2. 글씨 색상 뚜렷하게 (회색빛 제거) */
    p, li, span, div, .stMarkdown {
        color: #111827 !important; /* 거의 검은색에 가까운 진한 색 */
    }

    .stApp {
        background-color: #F8FAFC;
    }

    /* 타이틀 및 헤더 메인 컬러 (#3A449A) */
    h1 {
        color: #3A449A !important;
        font-weight: 800 !important;
        letter-spacing: -0.02em;
    }

    /* 사이드바 스타일링 */
    section[data-testid="stSidebar"] {
        background-color: #FFFFFF !important;
        border-right: 1px solid #E2E8F0;
    }

    /* 회색 인용문(>)을 알찬학원 메인 컬러 브랜드 카드로 변경 */
    blockquote {
        background-color: #FFFFFF !important;
        border-left: 5px solid #00A19D !important;
        border-radius: 12px !important;
        padding: 1.2rem 1.5rem !important;
        box-shadow: 0 4px 12px rgba(58, 68, 154, 0.08) !important;
        margin: 1.2rem 0 !important;
    }

    /* 피드백 카드 안의 회색빛 완전 제거 -> 쨍한 검은색 강제 적용 */
    blockquote *, blockquote p, blockquote li, blockquote span, blockquote div {
        color: #000000 !important;
        font-weight: 500 !important;
        opacity: 1 !important;
    }

    /* 채팅 메세지 스타일링 */
    .stChatMessage {
        background-color: #FFFFFF !important;
        border-radius: 16px !important;
        padding: 1.25rem !important;
        border: 1px solid #E2E8F0 !important;
        box-shadow: 0 2px 4px rgba(0,0,0,0.02) !important;
        color: #000000 !important;
    }

    /* 버튼 디자인: 깔끔한 백색/테두리 스타일로 원복 */
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

    /* 수식 블록 배경 */
    .katex-display {
        background-color: #F1F5F9;
        padding: 0.5rem;
        border-radius: 8px;
        color: #000000 !important;
    }

    /* 제목 옆 앵커 링크 및 Material Symbol 텍스트 완벽 차단 */
    a.header-anchor,
    [data-testid="stHeaderActionElements"],
    .stMarkdown h1 a, .stMarkdown h2 a, .stMarkdown h3 a {
        display: none !important;
        visibility: hidden !important;
        pointer-events: none !important;
        width: 0 !important;
        height: 0 !important;
        font-size: 0 !important;
    }
    </style>
    """,
        unsafe_allow_html=True,
    )


inject_custom_css()


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


# ---------------------------------------------------------
# 🔑 알찬학원 수강생 전용 인증 모듈
# ---------------------------------------------------------
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

st.title("✏️ 알찬학원 신다혜 쌤의 1:1 수학 클리닉")
st.caption(
    "막히는 문제나 내 풀이를 올리면 다혜 쌤이 어디가 틀렸는지 콕 짚어 줄게요!"
)

# 미인증 상태일 때 인증 화면 표시
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

# ---------------------------------------------------------
# 🎓 메인 클리닉 앱 영역 (인증 완료 후만 실행됨)
# ---------------------------------------------------------

if "messages" not in st.session_state:
    st.session_state.messages = []
if "last_file_id" not in st.session_state:
    st.session_state.last_file_id = None

with st.sidebar:
    st.header("📸 문제 & 풀이 업로드")
    st.info("💡 알찬학원 수강생은 무료로 자유롭게 이용할 수 있습니다.")

    uploaded_file = st.file_uploader(
        "문제 사진 (손글씨 풀이 포함 가능)", type=["jpg", "jpeg", "png"]
    )

    if uploaded_file:
        st.image(
            uploaded_file, caption="📌 현재 검토 중인 사진", use_container_width=True
        )

    st.markdown("---")
    if st.button("🔄 대화 초기화", use_container_width=True):
        st.session_state.messages = []
        st.session_state.last_file_id = None
        st.rerun()

system_prompt = load_system_prompt()
curr_file_id = uploaded_file.file_id if uploaded_file else None

if uploaded_file and api_key and (curr_file_id != st.session_state.last_file_id):
    st.session_state.last_file_id = curr_file_id
    st.session_state.messages = []

    client = anthropic.Anthropic(api_key=api_key)

    uploaded_file.seek(0)
    p_bytes = uploaded_file.read()

    content_blocks = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": uploaded_file.type,
                "data": base64.b64encode(p_bytes).decode("utf-8"),
            },
        },
        {
            "type": "text",
            "text": "이 사진을 분석해서 신다혜 쌤의 톤으로 양식에 맞춰 피드백해줘.",
        },
    ]

    api_messages = [{"role": "user", "content": content_blocks}]

    with st.spinner("다혜 쌤이 풀이를 꼼꼼하게 살피는 중입니다..."):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                system=system_prompt,
                messages=api_messages,
            )
            raw_reply = response.content[0].text
            bot_reply = clean_thinking_tags(raw_reply)
            st.session_state.messages.append(
                {"role": "assistant", "content": bot_reply}
            )
            st.rerun()
        except Exception as e:
            st.error(f"오류가 발생했습니다: {e}")

# 대화 출력
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# 입력 창
if prompt := st.chat_input("다혜 쌤에게 답장하기 (예: 높이가 18이 나와요!)"):
    if not api_key:
        st.error("API 키가 설정되지 않았습니다.")
        st.stop()

    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    client = anthropic.Anthropic(api_key=api_key)
    api_messages = []

    for idx, msg in enumerate(st.session_state.messages):
        if idx == 0 and uploaded_file:
            uploaded_file.seek(0)
            content_blocks = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": uploaded_file.type,
                        "data": base64.b64encode(
                            uploaded_file.read()
                        ).decode("utf-8"),
                    },
                },
                {"type": "text", "text": msg["content"]},
            ]
            api_messages.append({"role": "user", "content": content_blocks})
        else:
            api_messages.append(
                {"role": msg["role"], "content": msg["content"]}
            )

    with st.chat_message("assistant"):
        with st.spinner("생각 중..."):
            try:
                response = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=2000,
                    system=system_prompt,
                    messages=api_messages,
                )
                raw_reply = response.content[0].text
                bot_reply = clean_thinking_tags(raw_reply)
                st.markdown(bot_reply)
                st.session_state.messages.append(
                    {"role": "assistant", "content": bot_reply}
                )
            except Exception as e:
                st.error(f"오류가 발생했습니다: {e}")
