"""직무 게이트의 AI 자리(AiJudge)를 실제 LLM으로 채우는 얇은 어댑터 (docs/policies/job-gate.md 5-2 "AI 판별의 계약", 이슈 #12).

- 하는 일: 요청 구성, 공용 LLM 호출 모듈(core/llm_client.py) 호출, 라벨·이유 검증, {"label", "reason"} 반환.
- 하지 않는 일: 규칙 재구현, 항목 라벨·핵심 직무군·최종 판정·사유 코드·경고 코드·순위 그룹 결정, 점수, 자격요건 (AGENTS.md). 그것은 core/job_gate.py와 core/job_gate_result.py가 한다.
- AiJudge가 받는 request에는 직무명·모집부문·대분류·자격요건·우대사항·다른 항목의 라벨이 없다 (정책 5-2). 이 어댑터는 request의 정해진 키만 읽으므로 그 값들을 프롬프트에 넣을 길이 없다.
- 지원자 정보는 어떤 경우에도 넣지 않는다 (정책 eligibility.md 16장). 외부로 나가는 것은 공고 정보뿐이다.
- 응답이 계약을 어기면(허용 라벨 밖, reason 없음·공백뿐, 형식 오류) 다시 묻지 않고 LLMStageError로 처리한다. 실패한 하위 업무를 임의 라벨로 채우지 않는다.
"""
from collections.abc import Callable

from pydantic import BaseModel

from core import job_gate_config as cfg
from core.llm_client import LLMClient, LLMRequest, LLMStageError
from core.prompts import fill, load_prompt
from core.schemas import ITEM_LABEL_COMMON, ITEM_LABEL_MIXED, ITEM_LABEL_OUT_OF_SCOPE

STAGE = "gate_label"
REASON_MAX_CHARS = 200
DELIMITERS = ("<공고>", "</공고>")  # 프롬프트가 공고 문장을 감싸는 표시. 공고 안에 같은 표시가 있으면 무력화한다

SPECIAL_LABEL_NAMES = {
    ITEM_LABEL_COMMON: "공통 업무",
    ITEM_LABEL_OUT_OF_SCOPE: "지원 범위 밖 업무",
    ITEM_LABEL_MIXED: "서로 다른 직무군의 책임이 나눌 수 없게 섞인 업무",
}
LEVEL_NOTES = {
    "subtask": "아래 \"분류할 업무\" 하나(하위 업무)의 라벨을 고른다. 항목 전체와 나머지 항목은 문맥을 이해하는 참고용이다.",
    "item": "이 항목은 하위 업무 라벨이 정확히 절반씩 갈려 항목 전체의 결과물을 기준으로 다시 판단한다. 항목 전체의 라벨을 고른다.",
}


class GateLabelResponse(BaseModel):
    # 클래스 docstring은 모델에 보내는 스키마 설명이 되므로 쓰지 않는다. 허용 라벨 검증은 요청마다 달라 코드가 한다
    label: str
    reason: str


def call_id(posting_id: str, request: dict) -> str:
    """호출 식별자 (정책 5-2). 사용량 기록과 오류 메시지에서 어느 공고의 어느 항목·하위 업무인지 알아보는 용도다."""
    base = f"{posting_id}:item:{request['item_index']}"
    return base if request.get("sub_index") is None else f"{base}:subtask:{request['sub_index']}"


def _data(text: str) -> str:
    """공고 문장을 프롬프트에 넣기 전에 구분자 표시를 무력화한다 (공고 안의 표시가 구분자를 닫아 버리지 못하게)."""
    for mark in DELIMITERS:
        text = text.replace(mark, mark.replace("<", "< "))
    return text


def _family_text(family_id: str) -> str:
    fam = cfg.JOB_FAMILIES.get(family_id, {})
    return f"- `{family_id}` ({fam.get('name', family_id)}): {fam.get('definition', '')} 핵심 결과물: {fam.get('key_results', '')}"


def build_prompt(request: dict, body: str) -> str:
    """request에서 프롬프트를 만든다. 읽는 키는 level, text, item_text, other_items, candidates, allowed, families뿐이다."""
    allowed = request["allowed"]
    shown = request.get("candidates") or request["families"]  # 후보가 없으면 지원하는 직무군 전체의 정의를 준다
    labels = "\n".join(
        f"- `{label}`" + (f" ({cfg.JOB_FAMILIES[label]['name']})" if label in cfg.JOB_FAMILIES else f" ({SPECIAL_LABEL_NAMES.get(label, label)})")
        for label in allowed
    )
    others = "\n".join(f"- {_data(t)}" for t in request["other_items"]) or "(없음)"
    is_item = request["level"] == "item"
    return fill(
        body,
        labels=labels,
        families="\n".join(_family_text(f) for f in shown),
        level_note=LEVEL_NOTES["item" if is_item else "subtask"],
        subtask=_data(request["text"]),
        item="(위와 같다)" if is_item else _data(request["item_text"]),
        others=others,
    )


def clean_reason(reason: str) -> str:
    """이유는 판정에 쓰지 않고 기록에만 남는다. 공백·개행을 정리하고 길이를 제한한다 (정책 5-2)."""
    return " ".join(reason.split())[:REASON_MAX_CHARS]


def make_llm_judge(client: LLMClient, posting_id: str) -> Callable[[dict], dict]:
    """공고 하나의 AiJudge를 만든다 (core.job_gate.AiJudge 형태: request -> {"label", "reason"}).

    client는 LLMClient(실제는 CachedClient로 감싼 GeminiClient, 테스트는 FakeClient)다. 호출마다 새로 묻고 다시 묻지 않는다.
    """
    version, body = load_prompt(STAGE)

    def judge(request: dict) -> dict:
        cid = call_id(posting_id, request)
        llm_request = LLMRequest(
            stage=STAGE, prompt=build_prompt(request, body), schema=GateLabelResponse, job_id=cid,
            prompt_version=version, contains_profile=False,
        )
        data = client.complete_json(llm_request).data  # 통신·스키마 실패는 LLMStageError로 올라온다
        if data["label"] not in request["allowed"]:
            raise LLMStageError(f"{STAGE}({cid}): 허용되지 않은 라벨")
        reason = clean_reason(data["reason"])
        if not reason:
            raise LLMStageError(f"{STAGE}({cid}): reason이 비어 있다")
        return {"label": data["label"], "reason": reason}

    return judge
