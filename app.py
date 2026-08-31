import ast
import base64
import math
import re
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
    STUDENT_CODE = st.secrets["STUDENT_CODE"]
except Exception:
    STUDENT_CODE = "alchan1234"


def inject_custom_css():
    st.markdown(
        """
    <style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');

    /* 이모지 및 전체 폰트 설정 (이모지 깨짐 완벽 방지) */
    html, body, [class*="st-"], button, button *, input, textarea, select, span, div, p {
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif, "Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol", "Noto Color Emoji" !important;
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
        font-size: 1.1rem;
        margin-bottom: 0.6rem;
    }
    .guide-item {
        color: #1E293B;
        font-size: 0.95rem;
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

    div.stButton > button {
        border-radius: 10px !important;
        font-weight: 700 !important;
    }

    /* 파일 업로더 내 버튼 예외 처리 (대형 카드 스타일 상속 방지) */
    div[data-testid="stFileUploader"] button {
        min-height: unset !important;
        padding: 0.4rem 0.8rem !important;
        font-size: 0.875rem !important;
    }

    /* 💡 메인 2개 모드 선택 대형 카드 버튼 (100% 선명 및 크기 보장) */
    div[data-testid="stKey-btn_mode1"] button,
    div[data-testid="stKey-btn_mode2"] button,
    div.stButton > button[data-testid="stBaseButton-primary"],
    div.stButton > button[kind="primary"] {
        width: 100% !important;
        min-height: 190px !important;
        white-space: pre-wrap !important;
        word-break: keep-all !important;
        font-size: 1.05rem !important;
        line-height: 1.6 !important;
        border-radius: 16px !important;
        border: 2.5px solid #CBD5E1 !important;
        background-color: #FFFFFF !important;
        color: #1F2937 !important;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05) !important;
        text-align: center !important;
        padding: 1.5rem !important;
        transition: all 0.25s ease-in-out !important;
    }

    div[data-testid="stKey-btn_mode1"] button:hover,
    div[data-testid="stKey-btn_mode2"] button:hover,
    div.stButton > button[data-testid="stBaseButton-primary"]:hover,
    div.stButton > button[kind="primary"]:hover {
        border-color: #3A449A !important;
        color: #3A449A !important;
        background-color: #F1F5F9 !important;
        box-shadow: 0 8px 20px rgba(58, 68, 154, 0.15) !important;
        transform: translateY(-2px) !important;
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


# -------------------------------------------------------------------
# 🔒 도형 코드 안전 실행 (AST 화이트리스트)
#    - 모델이 생성한 ```python 블록을 그대로 exec 하지 않고, 먼저 구문 트리를
#      검사해서 그리기(matplotlib/numpy/patches)에 필요한 노드만 통과시킨다.
#    - import, 던더(__) 이름·속성, 임의 함수 호출, 거대한 상수/지수 등은 차단.
# -------------------------------------------------------------------
class UnsafeDiagramCode(Exception):
    pass


_SAFE_BUILTINS = {
    "range": range, "len": len, "enumerate": enumerate, "zip": zip,
    "min": min, "max": max, "abs": abs, "sum": sum, "round": round,
    "list": list, "tuple": tuple, "dict": dict, "set": set,
    "float": float, "int": int, "str": str, "bool": bool,
    "sorted": sorted, "reversed": reversed, "map": map, "filter": filter,
}

_DENY_ATTRS = {
    "savefig", "secrets", "environ", "system", "popen", "getenv", "putenv",
    "read", "write", "open", "remove", "unlink", "rename", "communicate",
    "call", "check_output", "check_call", "run", "Popen", "eval", "exec",
    "load", "loads", "connect", "urlopen", "request",
}

_ALLOWED_NODES = (
    ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign,
    ast.For, ast.If, ast.IfExp, ast.Pass, ast.Break, ast.Continue,
    ast.Call, ast.keyword, ast.Attribute, ast.Name, ast.Load, ast.Store, ast.Del,
    ast.Constant, ast.FormattedValue, ast.JoinedStr,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.List, ast.Tuple, ast.Dict, ast.Set, ast.Subscript, ast.Slice, ast.Starred,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, ast.Invert,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Is, ast.IsNot, ast.In, ast.NotIn,
)
_ALLOWED_NODES += tuple(
    getattr(ast, _n) for _n in ("Index", "ExtSlice", "Num", "Str", "NameConstant", "Bytes")
    if hasattr(ast, _n)
)


def _validate_diagram_ast(tree):
    nodes = list(ast.walk(tree))
    if len(nodes) > 2500:
        raise UnsafeDiagramCode("코드가 너무 깁니다")
    for node in nodes:
        if not isinstance(node, _ALLOWED_NODES):
            raise UnsafeDiagramCode(f"허용되지 않은 구문: {type(node).__name__}")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in _DENY_ATTRS:
                raise UnsafeDiagramCode(f"허용되지 않은 속성: {node.attr}")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise UnsafeDiagramCode("던더 이름 접근 금지")
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and abs(node.value) > 5_000_000:
                raise UnsafeDiagramCode("숫자 상수가 너무 큽니다")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            rhs = node.right
            if isinstance(rhs, ast.Constant) and isinstance(rhs.value, (int, float)) and rhs.value > 8:
                raise UnsafeDiagramCode("지수가 너무 큽니다")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in _SAFE_BUILTINS:
                raise UnsafeDiagramCode(f"허용되지 않은 함수 호출: {node.func.id}")


def run_diagram_code(code):
    """AST 검증을 통과한 코드만 제한된 스코프에서 실행하고 Figure를 반환."""
    tree = ast.parse(code, mode="exec")
    _validate_diagram_ast(tree)
    scope = {
        "__builtins__": _SAFE_BUILTINS,
        "plt": plt, "np": np, "patches": patches, "math": math,
    }
    plt.close("all")
    exec(compile(tree, "<diagram>", "exec"), scope)  # noqa: S102 - AST 화이트리스트 검증 후 실행
    fig = scope.get("fig")
    if fig is None:
        fig = plt.gcf()
    return fig


def render_assistant_content(content):
    pattern = r"```python\s*(.*?)\s*```"
    parts = re.split(pattern, content, flags=re.DOTALL)

    for i, part in enumerate(parts):
        if i % 2 == 0:
            if part.strip():
                st.markdown(part)
            continue

        code = part.strip()
        if not any(tok in code for tok in ("plt.", "fig", "patches.", "ax.")):
            st.code(code, language="python")
            continue

        try:
            fig = run_diagram_code(code)
            if fig is not None and len(fig.axes) > 0:
                st.pyplot(fig)
            plt.close("all")
        except UnsafeDiagramCode as e:
            plt.close("all")
            st.warning(f"⚠️ 안전하지 않은 그림 코드라서 실행하지 않았어요: {e}")
            st.code(code, language="python")
        except Exception:
            plt.close("all")
            st.code(code, language="python")


def load_system_prompt():
    default_prompt = (
        "너는 친절하고 실력 있는 알찬학원 수학 강사 신다혜 선생님이다.\n\n"
        "[강력 수식 & LaTeX 규칙 - 필수 준수]\n"
        "1. 모든 수학 공식, 수식, 변수(x, y, a, b 등), 숫자 식, 방정식, 기호는 예외 없이 100% LaTeX 표기법(`$ ... $` 또는 `$$ ... $$`)으로 작성해라.\n"
        "2. 분수를 작성할 때 가로 형태(1/2, a/b)는 절대 사용하지 말고, 반드시 문제집처럼 세로 분수 형태인 `\\dfrac{a}{b}`를 사용해라.\n"
        "3. 도형/기하 문제 시 반드시 matplotlib 도형 시각화 코드를 생성해라."
    )
    try:
        with open("system_prompt.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return default_prompt


if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

st.title("✏️ 알찬학원 신다혜 쌤의 1:1 수학 클리닉")

if not st.session_state.authenticated:
    st.markdown("---")
    st.subheader("🔒 알찬학원 수강생 전용 로그인")
    input_code = st.text_input(
        "학원 전용 비밀번호를 입력해 주세요:", type="password"
    )

    if st.button("🔓 클리닉 입장하기", use_container_width=True):
        if input_code == STUDENT_CODE:
            st.session_state.authenticated = True
            st.success("인증되었습니다! 다혜 쌤과의 공부를 시작해 보세요.")
            st.rerun()
        else:
            st.error("비밀번호가 올바르지 않습니다. 신다혜 쌤에게 문의해 주세요!")
    st.stop()

if "messages" not in st.session_state:
    st.session_state.messages = []
if "selected_mode" not in st.session_state:
    st.session_state.selected_mode = None
if "last_upload_key" not in st.session_state:
    st.session_state.last_upload_key = None

system_prompt = load_system_prompt()

if st.session_state.selected_mode is None:
    st.markdown(
        """
    <div class="guide-box">
        <div class="guide-title">💡 클리닉 이용 안내</div>
        <div class="guide-item">❓ <b>풀이가 막혔어요:</b> 문제 사진 1장을 올리면, 정답 대신 스스로 답을 찾아갈 수 있게 다혜 쌤이 질문을 던져줍니다.</div>
        <div class="guide-item">✍️ <b>내 풀이 검토:</b> 문제 사진과 연습장 풀이 사진 2장을 올리면 잘한 부분과 실수한 부분을 콕 짚어줍니다.</div>
        <div class="guide-item">📐 <b>수식 & 도형 그림:</b> 모든 수식은 세로 분수($\dfrac{a}{b}$), 도형 문제 시 직관적인 도형 그림 자동 생성!</div>
    </div>
    """,
        unsafe_allow_html=True,
    )

    st.markdown("### 💡 어떤 도움이 필요하신가요?")
    st.caption("아래 카드 중 원하는 진단 방식을 선택해 주세요.")

    col1, col2 = st.columns(2)

    with col1:
        btn1_text = (
            "❓ 풀이가 막혔어요 (스스로 풀어보기)\n\n"
            "📷 문제 사진 1장 필요\n\n"
            "정답 대신 스스로 답을 찾아갈 수 있게\n"
            "다혜 쌤이 차근차근 질문을 던져줄게요!"
        )
        if st.button(btn1_text, key="btn_mode1", use_container_width=True, type="primary"):
            st.session_state.selected_mode = "❓ 스스로 풀어보기 (차근차근 질문)"
            st.rerun()

    with col2:
        btn2_text = (
            "✍️ 내 풀이 검토 (연습장 오답 클리닉)\n\n"
            "📷 문제 + 연습장 풀이 2장 필요\n\n"
            "잘 접근한 부분과 어디서 개념/계산이\n"
            "삐끗했는지 오답 위치를 콕 짚어줄게요!"
        )
        if st.button(btn2_text, key="btn_mode2", use_container_width=True, type="primary"):
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
                "text": "[모드: 스스로 풀어보기] 학생이 풀다가 막혀서 문제 사진을 올렸어. 정답이나 전체 풀이를 바로 주지 말고, 스스로 고민해서 답을 찾을 수 있게 첫 번째 개념 질문만 건네줘. (도형 문제이거나 이해를 돕는 시각적 그림이 필요한 경우 반드시 matplotlib 도형 그림 코드를 생성해라)",
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
                "text": "[모드: 내 풀이 검토] 문제 사진과 연습장 풀이 사진이야. 연습장 풀이를 대조해서 잘 접근한 부분과 실수한 오답 지점을 지적해줘. (도형 문제이거나 시각적 그림이 필요하면 반드시 matplotlib 도형 그림 코드를 생성해라)",
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
                render_assistant_content(bot_reply)
                st.session_state.messages.append(
                    {"role": "assistant", "content": bot_reply}
                )
            except Exception as e:
                st.error(f"오류가 발생했습니다: {e}")
