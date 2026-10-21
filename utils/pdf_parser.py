"""해설지 PDF 대량 파싱(Bulk Ingestion).

문제를 하나씩 손으로 등록하던 '📚 해설 DB 관리'의 불편함을 줄이기 위해, 해설지 PDF
한 권을 통째로 올리면 텍스트를 추출해 문항 단위로 쪼개고, 각 문항을 '정석 해설 DB'
(utils/solution_db.py의 problem_solutions 테이블) 구조에 바로 넣을 수 있는 JSON으로
LLM이 파싱하게 한다.

PDF 텍스트 추출(PyPDF2)과 LLM 파싱을 분리해 둔 이유: 추출된 텍스트가 너무 길면 한
번의 LLM 호출로 모든 문항을 안정적으로 뽑아내기 어렵다(출력 토큰 한도에 걸려 뒤쪽
문항이 잘림) - 그래서 페이지 경계를 기준으로 여러 청크로 쪼개 각각 파싱한 뒤 합친다.
"""
import base64
import io
import json
import re

import anthropic

try:
    from PyPDF2 import PdfReader
except ImportError:
    PdfReader = None

try:
    import pypdfium2 as pdfium
except ImportError:
    pdfium = None


def is_pdf_parser_available():
    """PyPDF2가 설치되어 있는지 확인한다 - 없으면 호출하는 쪽이 업로드 UI를 숨기거나
    안내 문구로 대체해야 한다."""
    return PdfReader is not None


def is_pdf_image_available():
    """pypdfium2(스캔본 PDF를 Vision이 읽을 이미지로 렌더링하는 용도)가 설치되어
    있는지 확인한다. poppler 같은 외부 바이너리 없이 순수 파이썬으로 동작해서
    Streamlit Cloud에서도 추가 설치 없이 그대로 쓸 수 있다."""
    return pdfium is not None


def extract_pdf_text(pdf_bytes):
    """PDF 바이트에서 페이지별 텍스트를 추출해 페이지 구분자와 함께 이어붙인다.

    페이지 구분자를 남겨두는 이유: _split_into_chunks()가 이 구분자를 기준으로만
    청크를 나눠서, 한 문제의 해설이 청크 중간에서 반 토막 나는 일을 완전히는 못
    막아도 페이지 경계와는 맞출 수 있다.

    스캔본(이미지로만 된) PDF처럼 텍스트가 전혀 추출되지 않는 페이지는 빈 문자열로
    채워지고, 호출하는 쪽이 "추출된 문항 0개"로 자연스럽게 알아차리게 된다.
    """
    if PdfReader is None:
        raise RuntimeError("PyPDF2가 설치되지 않았습니다. requirements.txt를 확인해 주세요.")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages_text = []
    for i, page in enumerate(reader.pages):
        page_text = page.extract_text() or ""
        pages_text.append(f"--- PAGE {i + 1} ---\n{page_text}")
    return "\n\n".join(pages_text)


def is_scanned_pdf_text(pdf_text, min_chars_per_page=50):
    """extract_pdf_text()가 돌려준 '--- PAGE n ---' 마커 섞인 전체 텍스트를 보고,
    페이지당 평균 추출 글자수가 min_chars_per_page 미만이면 스캔본(이미지) PDF로
    판정한다. 텍스트가 통째로 비어 있으면(페이지 추출 자체가 안 됐으면) 당연히
    스캔본으로 본다.
    """
    if not (pdf_text or "").strip():
        return True
    page_count = len(re.findall(r"--- PAGE \d+ ---", pdf_text))
    if page_count == 0:
        return True
    body_only = re.sub(r"--- PAGE \d+ ---", "", pdf_text)
    avg_chars_per_page = len(body_only.strip()) / page_count
    return avg_chars_per_page < min_chars_per_page


def convert_pdf_to_images(pdf_bytes, max_pages=5, scale=2.0):
    """pypdfium2로 PDF 페이지를 JPEG 이미지 바이트 리스트로 렌더링한다.

    poppler 등 외부 바이너리 설치가 필요 없는 순수 파이썬 렌더러라 Streamlit Cloud
    환경에서도 추가 설치 없이 동작한다. max_pages로 앞쪽 페이지만 렌더링하는 이유:
    스캔본 해설지 전체(수십~수백 페이지)를 전부 Vision으로 돌리면 시간/비용이
    과도해지므로, 1차 버전은 처리 범위를 제한한다.
    """
    if pdfium is None:
        raise RuntimeError("pypdfium2가 설치되지 않았습니다. requirements.txt를 확인해 주세요.")
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        images = []
        for i in range(min(len(pdf), max_pages)):
            page = pdf[i]
            try:
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil().convert("RGB")
                buf = io.BytesIO()
                pil_image.save(buf, format="JPEG", quality=90)
                images.append(buf.getvalue())
            finally:
                page.close()
        return images
    finally:
        pdf.close()


SCANNED_SOLUTION_PARSER_SYSTEM_PROMPT = (
    "너는 수학 해설지 이미지 분석 전문가다. 제공된 해설지 페이지 이미지에서 각 문항 번호를 "
    "식별하고, 해설을 4단계(개념착안 -> 관계식 -> 전개/변형 -> 계산) 정석 풀이와 pitfalls "
    "리스트로 구조화하여 JSON으로 추출하라.\n\n"
    "반드시 아래 JSON 배열 형식으로만 출력하라:\n"
    "[\n"
    "  {\n"
    '    "problem_code": "문제번호(예: 0353)",\n'
    '    "reference_steps": [ {"step": 1, "title": "개념착안", "math": "..."}, ... ],\n'
    '    "pitfalls": ["자주 틀리는 개념이나 부호 실수"]\n'
    "  }, ...\n"
    "]\n\n"
    "- 이미지 속 글자가 흐리거나 일부가 잘려서 해설이 불완전한 문제는 통째로 건너뛰고 "
    "결과에 포함하지 마라.\n"
    "- 반드시 위 JSON 배열만 출력하라. 다른 설명, 머리말, 코드블록 표시 없이 JSON 그 자체만 "
    "출력해라. 이미지 속에서 문항 번호를 식별할 수 없으면 빈 배열 []을 출력하라."
)


def _parse_scanned_page(image_bytes, model_name, api_key):
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model_name,
        max_tokens=4000,
        system=SCANNED_SOLUTION_PARSER_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(image_bytes).decode("utf-8"),
                    },
                },
                {"type": "text", "text": "이 해설지 페이지 이미지 속 문제들을 추출해줘."},
            ],
        }],
    )
    raw = "".join(block.text for block in response.content if hasattr(block, "text")).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    parsed = json.loads(cleaned)
    return parsed if isinstance(parsed, list) else []


def parse_scanned_solutions(pdf_bytes, model_name, api_key, max_pages=5, progress_callback=None):
    """스캔본(이미지) PDF 전용 파서: 텍스트 추출 없이 페이지를 이미지로 렌더링해
    Vision LLM이 직접 문항/해설을 읽어 JSON으로 추출한다.

    parse_bulk_solutions()와 동일하게 problem_code 기준으로 결과를 합치고, 한
    페이지가 실패해도 나머지 페이지는 계속 처리한다. max_pages만큼만(기본 앞쪽
    5페이지) 처리한다 - convert_pdf_to_images() 참고.
    """
    images = convert_pdf_to_images(pdf_bytes, max_pages=max_pages)
    by_code = {}
    for i, image_bytes in enumerate(images):
        if progress_callback:
            progress_callback(i, len(images))
        try:
            for problem in _parse_scanned_page(image_bytes, model_name, api_key):
                code = str(problem.get("problem_code", "")).strip()
                if code:
                    by_code[code] = problem
        except Exception:
            continue
    if progress_callback:
        progress_callback(len(images), len(images))
    return list(by_code.values())


def _split_into_chunks(full_text, max_chars=6000):
    """'--- PAGE n ---' 구분자 앞에서만 쪼개서, 페이지 경계와 청크 경계를 맞춘다."""
    pages = re.split(r"(?=--- PAGE \d+ ---)", full_text)
    chunks, current = [], ""
    for page in pages:
        if current and len(current) + len(page) > max_chars:
            chunks.append(current)
            current = page
        else:
            current += page
    if current.strip():
        chunks.append(current)
    return chunks or [full_text]


BULK_SOLUTION_PARSER_SYSTEM_PROMPT = (
    "너는 수학 문제집 해설지 파서(Parser)다. 주어진 텍스트에서 각 문제 번호를 식별하여 "
    "분리하고, 각 문제의 해설을 다음의 JSON 구조로 추출하라:\n"
    "[\n"
    "  {\n"
    '    "problem_code": "문제번호(예: 0353)",\n'
    '    "reference_steps": [ {"step": 1, "title": "개념착안", "math": "..."}, ... ],\n'
    '    "pitfalls": ["자주 틀리는 개념이나 부호 실수"]\n'
    "  }, ...\n"
    "]\n\n"
    "- 각 문제의 해설은 반드시 '교과과정 표준 4단계 사고 흐름'(1. 개념착안/핵심 성질 파악 → "
    "2. 관계식 수립 → 3. 식 전개/변형 → 4. 계산 도출)에 맞게 재구성하라.\n"
    "- 주어진 텍스트 조각의 맨 앞이나 끝에서 해설이 잘려 불완전한 문제는 통째로 건너뛰고 "
    "결과에 포함하지 마라.\n"
    "- 반드시 위 JSON 배열만 출력하라. 다른 설명, 머리말, 코드블록 표시 없이 JSON 그 자체만 "
    "출력해라. 식별되는 문제가 하나도 없으면 빈 배열 []을 출력하라."
)


def _parse_chunk(chunk_text, model_name, api_key):
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model_name,
        max_tokens=4000,
        system=BULK_SOLUTION_PARSER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"다음 해설지 텍스트 조각에서 문제들을 추출해줘:\n\n{chunk_text}"}],
    )
    raw = "".join(block.text for block in response.content if hasattr(block, "text")).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    parsed = json.loads(cleaned)
    return parsed if isinstance(parsed, list) else []


def parse_bulk_solutions(pdf_text, model_name, api_key, max_chars_per_chunk=6000, progress_callback=None):
    """PDF 전체 텍스트를 청크로 쪼개 각각 LLM으로 파싱하고, problem_code 기준으로 합친다.

    같은 번호가 여러 청크에서 겹쳐 나오면(페이지 경계에 걸친 문제 등) 나중 청크의
    결과로 덮어쓴다. 청크 하나가 실패해도(JSON 파싱 오류 등) 나머지 청크는 계속
    진행한다 - 해설지 전체를 처리하다가 중간 한 문제 때문에 전부 실패하면 안 된다.

    progress_callback(처리한 청크 수, 전체 청크 수)가 주어지면 각 청크 처리 전에
    호출한다 - UI에서 진행 상황(progress bar)을 보여줄 때 쓴다.
    """
    chunks = _split_into_chunks(pdf_text, max_chars_per_chunk)
    by_code = {}
    for i, chunk in enumerate(chunks):
        if progress_callback:
            progress_callback(i, len(chunks))
        try:
            for problem in _parse_chunk(chunk, model_name, api_key):
                code = str(problem.get("problem_code", "")).strip()
                if code:
                    by_code[code] = problem
        except Exception:
            continue
    if progress_callback:
        progress_callback(len(chunks), len(chunks))
    return list(by_code.values())
