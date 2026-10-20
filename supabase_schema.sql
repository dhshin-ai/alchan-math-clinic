-- ============================================================
-- 알찬학원 신다혜 쌤의 수학 클리닉 - Supabase 스키마
-- Supabase 대시보드 > SQL Editor 에서 이 파일 내용을 그대로 붙여넣고 실행하세요.
-- (앱 코드는 이 5개 테이블 이름/컬럼명을 그대로 참조하므로 이름을 바꾸면 안 됩니다.)
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

-- ------------------------------------------------------------
-- (권장) Row Level Security 활성화
-- 이 앱은 Streamlit 서버에서 SUPABASE_SERVICE_KEY(= service_role 키)로만 접근하고,
-- 이 키는 RLS를 항상 우회하므로 앱 동작에는 전혀 영향을 주지 않습니다. 대신 혹시
-- anon/공개 키가 어딘가에 노출되더라도 아무 행도 읽고 쓸 수 없게 막아 주는
-- 안전장치입니다. 허용 정책(policy)을 하나도 만들지 않을 것이므로, RLS를 켜는
-- 순간 anon 키로는 완전히 닫힙니다.
-- ------------------------------------------------------------
alter table users          enable row level security;
alter table chat_history   enable row level security;
alter table pass_records   enable row level security;
alter table daily_missions enable row level security;
alter table ocr_corrections enable row level security;
