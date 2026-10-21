"""정석 해설(Reference Solution) DB 연동.

원장님이 '📚 해설 DB 관리' 화면에서 등록한 교과과정 표준 해설(reference_steps/pitfalls)을
problem_solutions 테이블에 저장/조회한다. 손글씨 풀이 첨삭의 2단계 검증
(app.py의 verify_transcribed_solution)이 이 데이터를 '바이블'로 받아 학생 풀이와
대조 채점하는 데 쓴다 - 등록된 문제가 없으면 호출하는 쪽이 조용히 기존 하이브리드
추론 방식(DEFAULT_REFERENCE_SOLUTION)으로 전환한다.
"""
from datetime import datetime, timezone

import streamlit as st

try:
    from supabase import create_client
except ImportError:
    create_client = None

TABLE_NAME = "problem_solutions"


@st.cache_resource
def _get_supabase_client():
    """solution_db 전용 Supabase 클라이언트를 프로세스당 한 번만 만든다.

    app.py도 같은 SUPABASE_URL/SUPABASE_SERVICE_KEY로 자신만의 전역 클라이언트를
    캐싱해 두지만, utils 모듈이 app.py를 거슬러 import하면 순환 참조가 생기므로
    여기서 따로 캐싱한다 - st.cache_resource라 실제로는 프로세스당 한 번만 생성된다.
    """
    if create_client is None:
        return None
    url = st.secrets.get("SUPABASE_URL", "").strip().rstrip("/")
    key = st.secrets.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        return None
    try:
        return create_client(url, key)
    except Exception:
        return None


def save_problem_solution(book_title, problem_code, chapter_name, reference_steps, pitfalls):
    """교재명+문제번호 기준으로 정석 해설 1건을 신규 등록하거나 덮어쓴다(Upsert).

    reference_steps/pitfalls는 JSON으로 직렬화 가능한 구조(보통 dict의 리스트)를 그대로
    받아 jsonb 컬럼에 저장한다. book_title+problem_code 유니크 제약으로 같은 문제를 다시
    등록하면 기존 행을 덮어쓴다.

    돌려주는 값: (성공 여부, 에러 메시지)
    """
    client = _get_supabase_client()
    if not client:
        return False, "Supabase 연결 정보(SUPABASE_URL/SUPABASE_SERVICE_KEY)가 설정되지 않았습니다."
    try:
        client.table(TABLE_NAME).upsert(
            {
                "book_title": book_title.strip(),
                "problem_code": problem_code.strip(),
                "chapter_name": (chapter_name or "").strip(),
                "reference_steps": reference_steps,
                "pitfalls": pitfalls,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="book_title,problem_code",
        ).execute()
        return True, ""
    except Exception as e:
        return False, str(e)


def get_problem_solution(book_title, problem_code):
    """교재명+문제번호로 정석 해설 1건을 조회한다.

    등록된 행이 없거나 Supabase 연결이 안 되어 있으면 조용히 None을 돌려준다 - 호출하는
    쪽(손글씨 첨삭 2단계 검증)이 이 None을 보고 기존 하이브리드 추론 방식으로 안전하게
    전환해야 한다.
    """
    if not (book_title or "").strip() or not (problem_code or "").strip():
        return None
    client = _get_supabase_client()
    if not client:
        return None
    try:
        res = (
            client.table(TABLE_NAME)
            .select("*")
            .eq("book_title", book_title.strip())
            .eq("problem_code", problem_code.strip())
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception:
        return None


def list_registered_problems():
    """등록된 전체 문제 목록을 최근 수정순으로 돌려준다(관리자 페이지 등록 현황 표용)."""
    client = _get_supabase_client()
    if not client:
        return []
    try:
        res = (
            client.table(TABLE_NAME)
            .select("book_title, problem_code, chapter_name, reference_steps, pitfalls, created_at, updated_at")
            .order("updated_at", desc=True)
            .execute()
        )
        return res.data or []
    except Exception:
        return []
