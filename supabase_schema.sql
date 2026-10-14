-- ============================================================
-- 알찬학원 신다혜 쌤의 수학 클리닉 - Supabase 스키마
-- Supabase 대시보드 > SQL Editor 에서 이 파일 내용을 그대로 붙여넣고 실행하세요.
-- (앱 코드는 이 4개 테이블 이름/컬럼명을 그대로 참조하므로 이름을 바꾸면 안 됩니다.)
-- ============================================================

-- gen_random_uuid()에 필요 (Supabase 신규 프로젝트는 기본 활성화되어 있지만, 혹시 몰라 명시)
create extension if not exists pgcrypto;

-- 1) users: 카카오로 로그인한 학생(또는 관리자) 1명당 1행.
--    kakao_id가 카카오 OAuth가 돌려주는 고유 회원번호이자 기본키다.
create table if not exists users (
    kakao_id      text primary key,
    nickname      text not null default '',
    student_grade text,                 -- '질문/첨삭' 탭용 (2022 개정 교육과정 과목명)
    pass_grade    text,                 -- '개념 PASS' 탭용 (중1~고3)
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

-- 4) daily_missions: 선생님이 '선생님 대시보드'에서 학년별로 지정하는 '오늘의 개념 범위'.
--    (grade, mission_date) 조합이 유니크해서, 저장할 때마다 자동으로 날짜별 기록이 쌓인다.
create table if not exists daily_missions (
    id           uuid primary key default gen_random_uuid(),
    grade        text not null,                 -- 중1/중2/중3/고1/고2/고3
    mission_date date not null default current_date,
    scope_text   text not null default '',
    created_at   timestamptz not null default now(),
    unique (grade, mission_date)
);

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
