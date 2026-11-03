"""직무 게이트의 mock AI 자리 (docs/policies/job-gate.md 6-3, 8장).

실제 AI 연결은 보류라 AI 자리에는 정답 항목 라벨을 돌려주는 mock을 넣는다. 이 mock의 지표는 "AI가 항상 정답 항목 라벨을 고른다고 가정한
상한"이다. 그래서 틀린 공고는 규칙(단서·문맥 판별·공통 단서·과반·직무명 대조·기준값)이 만든 오류다.
AI 자리의 형태는 core.job_gate.AiJudge를 따른다. 실제 AI를 연결할 때는 같은 형태의 함수를 core/llm_client.py(이슈 #6) 위에 만든다.
"""
from core import job_gate as gate


def make_label_mock(labels, events=None, case_id=None):
    """항목 index별 정답 라벨(코드값)을 돌려주는 AI 자리를 만든다.

    정답 라벨이 request["allowed"] 목록에 없으면 정답을 낼 수 없으므로 혼합을 내고, events에 기록한다(후보 탐색 누락).
    """
    def mock(request):
        want = labels[request["item_index"]]
        if want in request["allowed"]:
            return {"label": want, "reason": "정답 항목 라벨 (mock)"}
        if events is not None:
            events.append({"case": case_id, "item": request["item_index"] + 1, "want": want,
                           "allowed": request["allowed"], "text": request["text"][:60]})
        return {"label": gate.MIXED, "reason": "정답이 라벨 목록 밖 (mock)"}

    return mock
