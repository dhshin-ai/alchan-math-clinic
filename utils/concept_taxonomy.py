"""유형 가이드라인 DB(concept_taxonomies) 연동.

원장님이 구축한 교과과정 유형별 채점 가이드라인(core_formula/standard_steps/
forbidden_approaches)을 조회한다. 손글씨 풀이 첨삭의 1장 모드가, 특정 문제 단위로
등록된 정석 해설(problem_solutions)이 없을 때 한 단계 더 느슨한 '유형' 단위로
이 데이터를 대조 채점 기준으로 쓴다(app.py의 classify_concept_type()/
verify_image_against_concept_taxonomy() 참고).
"""
import streamlit as st

try:
    from supabase import create_client
except ImportError:
    create_client = None

TABLE_NAME = "concept_taxonomies"


@st.cache_resource
def _get_supabase_client():
    """concept_taxonomy 전용 Supabase 클라이언트를 프로세스당 한 번만 만든다.

    app.py도 같은 SUPABASE_URL/SUPABASE_SERVICE_KEY로 자신만의 전역 클라이언트를
    캐싱해 두지만, utils 모듈이 app.py를 거슬러 import하면 순환 참조가 생기므로
    여기서 따로 캐싱한다(utils/solution_db.py와 동일한 패턴).
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


@st.cache_data(ttl=600)
def list_concept_taxonomies():
    """등록된 전체 유형의 (type_code, type_name, core_formula) 목록을 가져온다 -
    Vision 분류 단계(classify_concept_type)에서 '63개 유형 중 어디에 해당하는지'
    고르게 할 때 쓴다.

    학생 여러 명이 동시에 사진을 올려도 매번 테이블을 긁지 않도록 10분 캐시를 건다.
    type_name 컬럼이 없는 환경이면 자동으로 뺀 채 조회를 재시도한다(선생님이 처음
    등록할 때 컬럼 구성이 조금 다를 수 있으므로).
    """
    client = _get_supabase_client()
    if not client:
        return []
    try:
        res = client.table(TABLE_NAME).select("type_code, type_name, core_formula").execute()
        return res.data or []
    except Exception:
        try:
            res = client.table(TABLE_NAME).select("type_code, core_formula").execute()
            return res.data or []
        except Exception:
            return []


def get_concept_taxonomy(type_code):
    """type_code 하나의 전체 채점 가이드라인(core_formula/standard_steps/
    forbidden_approaches)을 조회한다.

    등록된 행이 없거나 Supabase 연결이 안 되어 있으면 조용히 None을 돌려준다 -
    호출하는 쪽(1장 모드 첨삭 파이프라인)이 이 None을 보고 자유형식 첨삭으로
    안전하게 전환해야 한다.
    """
    if not (type_code or "").strip():
        return None
    client = _get_supabase_client()
    if not client:
        return None
    try:
        res = (
            client.table(TABLE_NAME)
            .select("*")
            .eq("type_code", type_code.strip())
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception:
        return None
