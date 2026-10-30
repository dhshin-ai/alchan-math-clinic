-- ============================================================
-- 알찬학원 신다혜 쌤의 수학 클리닉 - Supabase 스키마
-- Supabase 대시보드 > SQL Editor 에서 이 파일 내용을 그대로 붙여넣고 실행하세요.
-- (앱 코드는 이 8개 테이블 이름/컬럼명을 그대로 참조하므로 이름을 바꾸면 안 됩니다.)
-- ============================================================

-- gen_random_uuid()에 필요 (Supabase 신규 프로젝트는 기본 활성화되어 있지만, 혹시 몰라 명시)
create extension if not exists pgcrypto;

-- 1) users: 카카오로 로그인한 학생(또는 관리자) 1명당 1행.
--    kakao_id가 카카오 OAuth가 돌려주는 고유 회원번호이자 기본키다.
--    주의: '질문/첨삭' 탭 과목 선택(student_grade)은 저장하지 않는다 - 실제 운영 중인
--    테이블에 그 컬럼이 없어서(42703 column does not exist) app.py가 세션에만 두는
--    값으로 바꿔 둔 상태다. 나중에 컬럼을 추가해 영구 저장하려면 여기 `student_grade
--    text`를 추가한 뒤 app.py의 persist_current_student()/fetch_all_chat_history()도
--    같이 되돌려야 한다.
--    주의 2: pass_grade도 이 파일에는 있지만 실제 운영 테이블에는 없는 상태가
--    확인됐다(42703). app.py는 이제 이 컬럼이 없어도 크래시하지 않고 조용히
--    저장/조회를 건너뛰도록 방어 처리돼 있지만, '개념 PASS' 반/학년을 로그인 간에
--    실제로 기억시키려면(지금 설계가 의도한 동작) 아래 SQL을 Supabase SQL Editor에서
--    한 번 실행해야 한다:
--        alter table users add column if not exists pass_grade text;
create table if not exists users (
    kakao_id      text primary key,
    nickname      text not null default '',
    pass_grade    text,                 -- '개념 PASS' 탭용 (중1~고3 등)
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

-- 2) chat_history: '❓ 질문' / '🔍 첨삭' 탭에서 올린 질문 1건 = 1행 (오답노트 카드 1개).
--    messages jsonb 안에 업로드한 사진(base64)과 AI와 주고받은 대화 전체가 들어간다.
create table if not exists chat_history (
    id         uuid primary key default gen_random_uuid(),
    kakao_id   text not null references users(kakao_id) on delete cascade,
    feature    text not null,                         -- '❓ 질문' / '🔍 첨삭'
    messages   jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists chat_history_kakao_id_idx on chat_history (kakao_id);

-- 3) pass_records: '💮 개념PASS' 통과 1건 = 1행 (포토카드 보관함/스트릭/스탬프판의 근거 데이터).
create table if not exists pass_records (
    id               uuid primary key default gen_random_uuid(),
    kakao_id         text not null references users(kakao_id) on delete cascade,
    topic            text not null,
    concept_summary  text,
    completed_at     timestamptz not null default now()
);
create index if not exists pass_records_kakao_id_idx on pass_records (kakao_id);

-- 4) daily_missions: 선생님이 '선생님 대시보드'의 "🚀 오늘의 개념 PASS 출제" 카드에서
--    학년별로 등록하는 미션. 유니크 제약이 없는 단순 로그성 테이블이라 출제할 때마다
--    새 행이 쌓이고, 앱은 created_at이 오늘(KST)인 행 중 학년별로 가장 최근 것만 골라
--    '오늘의 미션'으로 쓴다(app.py의 load_daily_pass_scope() 참고).
create table if not exists daily_missions (
    id           uuid primary key default gen_random_uuid(),
    grade        text not null,                 -- 중1/중2/중3/고1(원종)/고1(수주,덕산)/고2/고3
    concept_text text not null default '',
    created_at   timestamptz not null default now()
);

-- 5) ocr_corrections: '🔍 첨삭' 손글씨 풀이 OCR에서 Mathpix/Vision이 처음 읽은 텍스트와
--    학생이 'Quick-Edit' 화면에서 실제로 고친 최종 텍스트를 나란히 남긴다 - 추후 자체
--    수식 OCR 모델을 학습시킬 데이터셋으로 쓰기 위함(app.py의 log_ocr_correction() 참고).
--    학생이 판독 결과를 실제로 수정했을 때만 한 행이 쌓인다(그대로 확정하면 로그하지 않음).
--    image_url은 Storage 버킷 'ocr-corrections'(public)에 올린 풀이 사진의 공개 URL이다 -
--    이 테이블이 아직 없거나 버킷/업로드 권한이 없어도 첨삭 기능 자체는 조용히 정상
--    동작한다(로깅 실패를 삼키도록 방어돼 있음). 이 로그를 실제로 쌓으려면 이 테이블을
--    만들고, Storage에서 'ocr-corrections' 버킷을 Public으로 한 번 만들어 둬야 한다
--    (대시보드 > Storage > New bucket, 또는 service_role 키가 버킷 생성 권한이 있으면
--    앱이 첫 로깅 시도에서 자동으로 만든다).
create table if not exists ocr_corrections (
    id                 uuid primary key default gen_random_uuid(),
    kakao_id           text references users(kakao_id) on delete cascade,
    original_ocr_text  text not null default '',
    user_edited_text   text not null default '',
    image_url          text,
    created_at         timestamptz not null default now()
);
create index if not exists ocr_corrections_kakao_id_idx on ocr_corrections (kakao_id);

-- 6) problem_solutions: 원장님이 '📚 해설 DB 관리'에서 등록한 교과과정 표준 해설
--    (reference_steps/pitfalls)을 문제(교재명+문제번호) 단위로 저장한다. 손글씨 풀이
--    첨삭 2단계 검증(app.py의 verify_transcribed_solution)이 이 데이터를 '바이블'로
--    받아 학생 풀이와 대조 채점한다 - 등록된 문제가 없으면 기존 하이브리드 추론
--    방식(DEFAULT_REFERENCE_SOLUTION)으로 조용히 전환되므로 이 테이블이 비어 있어도
--    기능 자체는 막히지 않는다. book_title+problem_code 조합이 유니크해서 같은 문제를
--    다시 등록하면 upsert로 덮어쓴다.
create table if not exists problem_solutions (
    id              uuid primary key default gen_random_uuid(),
    book_title      text not null,
    problem_code    text not null,
    chapter_name    text not null default '',
    reference_steps jsonb not null default '[]'::jsonb,
    pitfalls        jsonb not null default '[]'::jsonb,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (book_title, problem_code)
);
create index if not exists problem_solutions_lookup_idx on problem_solutions (book_title, problem_code);

-- 7) student_questions: '🔍 오답 첨삭'의 손글씨 풀이 OCR 파이프라인에서 학생이 Quick-Edit을
--    확정해 최종 진단/힌트를 받을 때마다(수정 여부와 무관하게 매번) 1건씩 쌓이는 구조화된
--    첨삭 기록이다(app.py의 log_student_question() 참고). chat_history는 대화 전체를
--    messages jsonb 블롭으로 담는 범용 로그이고, 이 테이블은 선생님 대시보드가 문제번호/
--    OCR 텍스트/AI 피드백을 바로 훑어볼 수 있게 컬럼을 분리해 둔 전용 로그다.
--    image_url은 Storage 버킷 'student_uploads'(public)에 올린 풀이 사진의 공개 URL이다 -
--    이 테이블/버킷이 아직 없거나 업로드 권한이 없어도 첨삭 기능 자체는 조용히 정상
--    동작한다(로깅 실패를 삼키도록 방어돼 있음).
create table if not exists student_questions (
    id           uuid primary key default gen_random_uuid(),
    kakao_id     text references users(kakao_id) on delete cascade,
    student_name text,
    problem_code text,
    image_url    text,
    ocr_text     text,
    ai_feedback  text,
    created_at   timestamptz not null default now()
);
create index if not exists student_questions_kakao_id_idx on student_questions (kakao_id);
create index if not exists student_questions_created_at_idx on student_questions (created_at desc);

-- ------------------------------------------------------------
-- (권장) Row Level Security 활성화
-- 이 앱은 Streamlit 서버에서 SUPABASE_SERVICE_KEY(= service_role 키)로만 접근하고,
-- 이 키는 RLS를 항상 우회하므로 앱 동작에는 전혀 영향을 주지 않습니다. 대신 혹시
-- anon/공개 키가 어딘가에 노출되더라도 아무 행도 읽고 쓸 수 없게 막아 주는
-- 안전장치입니다. 허용 정책(policy)을 하나도 만들지 않을 것이므로, RLS를 켜는
-- 순간 anon 키로는 완전히 닫힙니다.
--
-- ⚠️ student_questions에도 일부러 "모두 허용(anon도 insert/select 가능)" 정책을
-- 추가하지 않았습니다. 위 설명대로 service_role 키는 RLS를 완전히 우회하므로
-- 정책이 하나도 없어도 앱의 로깅/대시보드 조회는 100% 그대로 동작합니다. 반대로
-- "allow all" 정책을 열면, anon 키가 어딘가에서 노출되는 순간 학생 이름/풀이 사진/
-- AI 첨삭 내용을 누구나 읽고 조작할 수 있게 됩니다 - 그래서 다른 6개 테이블과 똑같이
-- 안전한 기본값(RLS는 켜고 정책은 비움)을 유지했습니다. 정말로 anon 키로 직접
-- 접근해야 하는 사정이 있다면, 꼭 필요한 범위만(예: insert만) 아래처럼 열어 쓰세요:
--   create policy "student_questions anon insert" on student_questions
--     for insert to anon with check (true);
-- ------------------------------------------------------------
alter table users          enable row level security;
alter table chat_history   enable row level security;
alter table pass_records   enable row level security;
alter table daily_missions enable row level security;
alter table ocr_corrections enable row level security;
alter table problem_solutions enable row level security;
alter table student_questions enable row level security;

-- ------------------------------------------------------------
-- 8) concept_taxonomies: 공통수학2 'I. 도형의 방정식' 전체 63개 유형의 채점
--    가이드라인(핵심 공식/표준 풀이 단계/금지된 우회·오답 패턴). 원장님이 Supabase에서
--    직접 실행한 DDL을 그대로 옮겨 둔다 - 손글씨 풀이 첨삭 1장 모드가 problem_solutions에
--    등록된 특정 문제가 없을 때, 이 테이블로 유형을 분류해 한 단계 느슨한 단위로라도
--    표준 풀이 단계와 대조 채점한다(app.py의 classify_concept_type()/
--    get_concept_taxonomy() 참고).
--
-- ⚠️ 아래 정책은 원장님이 명시적으로 요청한 "anon 포함 전체 공개 읽기/쓰기"입니다.
--    이 앱은 service_role 키로만 접근하므로 이 정책이 없어도 앱 동작엔 영향이
--    없습니다 - 열어 두면 anon 키를 아는 누구나 이 표를 읽고/쓰고/지울 수 있다는
--    뜻이니, 나중에 꼭 필요 없다고 판단되면 아래 DROP POLICY로 되돌리면 됩니다.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS concept_taxonomies (
    id SERIAL PRIMARY KEY,
    type_code VARCHAR(50) UNIQUE NOT NULL,
    major_chapter VARCHAR(100) NOT NULL,
    sub_chapter VARCHAR(100) NOT NULL,
    type_name VARCHAR(200) NOT NULL,
    core_formula JSONB NOT NULL,
    standard_steps JSONB NOT NULL,
    forbidden_approaches JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
ALTER TABLE concept_taxonomies ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Allow public read-write for concept_taxonomies" ON concept_taxonomies;
CREATE POLICY "Allow public read-write for concept_taxonomies" ON concept_taxonomies FOR ALL TO public USING (true) WITH CHECK (true);

-- ------------------------------------------------------------
-- 🚨 진단용 "RLS 전면 개방" - student_questions가 계속 비어 있을 때 쓰는 비상 쿼리
-- ------------------------------------------------------------
-- student_questions에 insert가 한 건도 안 들어가는 가장 흔한 실제 원인은 RLS가 아니라
-- st.secrets의 SUPABASE_SERVICE_KEY 값이 service_role 키가 아니라 anon 키로 잘못
-- 들어가 있는 경우입니다. 먼저 Supabase 대시보드 > Settings > API에서
-- "service_role" 비밀 키(anon 키와는 다른 값입니다)를 복사해 SUPABASE_SERVICE_KEY에
-- 다시 넣고, 사이드바의 "🚨 DB 강제 연결 테스트" 버튼으로 먼저 확인해 보세요 - 이
-- 한 줄 수정으로 끝나는 경우가 대부분입니다.
--
-- 그래도 계속 막혀서 정말로 anon 키로도 막히지 않게 완전히 열어야 한다면, 아래 2줄을
-- 실행하세요. 이렇게 하면 anon 키를 아는 누구나(= 이 키가 브라우저에 노출되는 모든
-- 사람) student_questions의 모든 행을 읽고/쓰고/지울 수 있게 됩니다 - 학생 이름,
-- 풀이 사진 URL, AI 첨삭 내용이 전부 공개된다는 뜻이니, 원인 파악이 끝나는 대로
-- 반드시 아래 "원복" 쿼리로 되돌리세요.
drop policy if exists "allow all student_questions (TEMP - 진단용)" on student_questions;
create policy "allow all student_questions (TEMP - 진단용)"
    on student_questions
    for all
    to public
    using (true)
    with check (true);

-- ↑ 원인 파악이 끝나면 아래 쿼리로 반드시 원복하세요(정책만 삭제 - RLS는 계속 켜진
-- 채로 둬도 service_role 키는 영향을 받지 않습니다):
--   drop policy if exists "allow all student_questions (TEMP - 진단용)" on student_questions;
