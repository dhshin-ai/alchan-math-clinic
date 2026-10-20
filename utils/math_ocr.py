"""Mathpix 전문 수식 OCR 연동.

'손글씨 풀이 첨삭'의 1단계(전사)를 Vision LLM 단독이 아니라, 수식 인식에 특화된
Mathpix API로 우선 처리해서 상용 에듀테크 수준의 인식률을 확보한다. st.secrets에
MATHPIX_APP_ID/MATHPIX_APP_KEY가 없거나 호출이 실패해도 앱이 멈추지 않도록, 예외를
밖으로 던지지 않고 (성공 여부, 결과, 에러메시지) 튜플로만 돌려준다 - 호출하는 쪽
(app.py)이 False를 받으면 기존 Vision LLM 전사로 그대로 폴백하면서, 실패 사유를
화면에 그대로 띄워 '조용한 폴백'으로 원인이 묻히지 않게 한다.
"""
import base64

import requests
import streamlit as st

MATHPIX_ENDPOINT = "https://api.mathpix.com/v3/text"


def _load_mathpix_keys():
    """st.secrets에서 Mathpix 키를 읽어온다. 복붙 과정에서 섞여 들어오는 양쪽 공백을
    제거하지 않으면 키가 멀쩡해도 401 Unauthorized로 조용히 실패하므로 .strip()한다."""
    app_id = str(st.secrets.get("MATHPIX_APP_ID", "") or "").strip()
    app_key = str(st.secrets.get("MATHPIX_APP_KEY", "") or "").strip()
    return app_id, app_key


def is_mathpix_configured():
    """Mathpix 연동 키가 st.secrets에 설정되어 있는지 확인한다."""
    app_id, app_key = _load_mathpix_keys()
    return bool(app_id and app_key)


def recognize_handwriting_latex(image_bytes, timeout=20):
    """손글씨 수식 사진 한 장을 Mathpix v3/text로 인식해 줄별 LaTeX 텍스트를 뽑아낸다.

    성공하면 (True, {"text", "latex_items", "confidence"}, "")를,
    키가 없거나 호출/응답 오류가 나면 (False, None, 에러 상세 메시지)를 돌려준다.
    에러 메시지에는 가능한 한 원인을 특정할 수 있는 정보(HTTP 상태코드, 응답 본문,
    Mathpix가 돌려준 error 필드 등)를 그대로 담아서, 호출하는 쪽이 화면에 그대로
    띄워도 "401 Unauthorized"인지 "키 누락"인지 "Payload 오류"인지 바로 구분되게 한다.
    """
    app_id, app_key = _load_mathpix_keys()
    if not app_id or not app_key:
        return False, None, "MATHPIX_APP_ID/MATHPIX_APP_KEY가 st.secrets에 설정되지 않았습니다."

    try:
        response = requests.post(
            MATHPIX_ENDPOINT,
            json={
                "src": f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode('utf-8')}",
                "formats": ["text", "data"],
                "data_options": {"include_latex": True},
            },
            headers={
                "app_id": app_id,
                "app_key": app_key,
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
    except requests.exceptions.RequestException as e:
        return False, None, f"요청 전송 실패(네트워크/타임아웃): {e}"

    if response.status_code != 200:
        return False, None, f"HTTP {response.status_code} {response.reason} - {response.text[:300]}"

    try:
        data = response.json()
    except ValueError:
        return False, None, f"응답이 JSON이 아닙니다: {response.text[:300]}"

    if data.get("error"):
        detail = (data.get("error_info") or {}).get("message") or data.get("error")
        return False, None, f"Mathpix 오류: {detail}"

    text = (data.get("text") or "").strip()
    latex_items = [
        item.get("value", "")
        for item in (data.get("data") or [])
        if isinstance(item, dict) and item.get("value")
    ]
    if not text and not latex_items:
        return False, None, "Mathpix가 수식을 인식하지 못했습니다(빈 응답)."

    return True, {
        "text": text,
        "latex_items": latex_items,
        "confidence": data.get("confidence"),
    }, ""
