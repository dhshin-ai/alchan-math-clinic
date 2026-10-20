"""Mathpix 전문 수식 OCR 연동.

'손글씨 풀이 첨삭'의 1단계(전사)를 Vision LLM 단독이 아니라, 수식 인식에 특화된
Mathpix API로 우선 처리해서 상용 에듀테크 수준의 인식률을 확보한다. st.secrets에
MATHPIX_APP_ID/MATHPIX_APP_KEY가 없거나 호출이 실패해도 앱이 멈추지 않도록, 예외를
밖으로 던지지 않고 (성공 여부, 결과, 에러메시지) 튜플로만 돌려준다 - 호출하는 쪽
(app.py)이 False를 받으면 기존 Vision LLM 전사로 그대로 폴백하면 된다.
"""
import base64

import requests
import streamlit as st

MATHPIX_ENDPOINT = "https://api.mathpix.com/v3/text"


def is_mathpix_configured():
    """Mathpix 연동 키가 st.secrets에 설정되어 있는지 확인한다."""
    app_id = st.secrets.get("MATHPIX_APP_ID", "")
    app_key = st.secrets.get("MATHPIX_APP_KEY", "")
    return bool(app_id and app_key)


def recognize_handwriting_latex(image_bytes, timeout=20):
    """손글씨 수식 사진 한 장을 Mathpix v3/text로 인식해 줄별 LaTeX 텍스트를 뽑아낸다.

    성공하면 (True, {"text", "latex_styled", "confidence"}, "")를,
    키가 없거나 호출/응답 오류가 나면 (False, None, 에러 메시지)를 돌려준다.
    """
    app_id = st.secrets.get("MATHPIX_APP_ID", "")
    app_key = st.secrets.get("MATHPIX_APP_KEY", "")
    if not (app_id and app_key):
        return False, None, "Mathpix 연동 정보(MATHPIX_APP_ID/MATHPIX_APP_KEY)가 설정되지 않았습니다."

    try:
        response = requests.post(
            MATHPIX_ENDPOINT,
            json={
                "src": f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode('utf-8')}",
                "formats": ["text", "latex_styled"],
                "data_options": {"include_latex": True},
            },
            headers={
                "app_id": app_id,
                "app_key": app_key,
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()

        if data.get("error"):
            return False, None, data.get("error_info", {}).get("message") or data.get("error")

        text = (data.get("text") or "").strip()
        latex_styled = (data.get("latex_styled") or "").strip()
        if not text and not latex_styled:
            return False, None, "Mathpix가 수식을 인식하지 못했습니다(빈 응답)."

        return True, {
            "text": text,
            "latex_styled": latex_styled,
            "confidence": data.get("confidence"),
        }, ""
    except Exception as e:
        return False, None, str(e)
