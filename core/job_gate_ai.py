"""직무 게이트의 AI 자리(AiJudge)를 실제 LLM으로 채우는 얇은 어댑터 (docs/policies/job-gate.md 5-2 "AI 판별의 계약", 이슈 #12).

- 하는 일: 요청 구성, 공용 LLM 호출 모듈(core/llm_client.py) 호출, 라벨·이유 검증, {"label", "reason"} 반환.
- 하지 않는 일: 규칙 재구현, 항목 라벨·핵심 직무군·최종 판정·사유 코드·경고 코드·순위 그룹 결정, 점수, 자격요건 (AGENTS.md). 그것은 core/job_gate.py와 core/job_gate_result.py가 한다.
- AiJudge가 받는 request에는 직무명·모집부문·대분류·자격요건·우대사항·다른 항목의 라벨이 없다 (정책 5-2). 이 어댑터는 request의 정해진 키만 읽으므로 그 값들을 프롬프트에 넣을 길이 없다.
  읽는 키: level, text, item_text, other_items, context_items(회사 소개·포지션 설명 문장), candidates, allowed, families.
- 지원자 정보는 어떤 경우에도 넣지 않는다 (정책 eligibility.md 16장). 외부로 나가는 것은 공고 정보뿐이다.
- 응답이 계약을 어기면(허용 라벨 밖, reason 없음·공백뿐, 형식 오류) 다시 묻지 않고 LLMStageError로 처리한다. 실패한 하위 업무를 임의 라벨로 채우지 않는다.
- 프롬프트 주입: 규칙·허용 라벨·직무군 정의는 시스템 지시로, 공고 문장은 별도 메시지(사용자 메시지)에 구분자로 감싸 보낸다. 이것은 영향을 줄이는 완화이고
  모델이 지시문을 따르지 않는다는 보장이 아니다. 최종 방어선은 코드의 허용 라벨 검증과 판정 규칙이다 (정책 5-2).
"""
import unicodedata
from collections.abc import Callable

from pydantic import BaseModel, field_validator

from core import job_gate_config as cfg
from core.llm_client import LLMClient, LLMRequest, LLMStageError
from core.prompts import fill, load_prompt
from core.schemas import ITEM_LABEL_COMMON, ITEM_LABEL_MIXED, ITEM_LABEL_OUT_OF_SCOPE

STAGE = "gate_label"
REASON_MAX_CHARS = 200
REASON_MAX_BYTES = 600  # UTF-8. 한글은 글자당 3바이트라 200자면 600바이트다. 4바이트 글자가 많을 때를 막는다
COPY_GUARD_CHARS = 30  # reason에 요청의 공고 문장이 이만큼 이어서 그대로 들어 있으면 기록하지 않는다
COPY_PLACEHOLDER = "공고 문장을 그대로 옮겨 기록하지 않음"
DELIMITERS = ("<공고>", "</공고>")  # 프롬프트가 공고 문장을 감싸는 표시. 공고 안에 같은 표시가 있으면 무력화한다
DATA_MARKER = "\n## 분류할 업무\n"  # 프롬프트에서 이 줄부터가 공고에서 온 데이터(사용자 메시지)다. 그 앞은 시스템 지시다

SPECIAL_LABEL_NAMES = {
    ITEM_LABEL_COMMON: "공통 업무",
    ITEM_LABEL_OUT_OF_SCOPE: "지원 범위 밖 업무",
    ITEM_LABEL_MIXED: "서로 다른 직무군의 책임이 나눌 수 없게 섞인 업무",
}
LEVEL_NOTES = {
    "subtask": "아래 \"분류할 업무\" 하나(하위 업무)의 라벨을 고른다. 항목 전체와 나머지 항목은 문맥을 이해하는 참고용이다.",
    "item": "이 항목은 하위 업무 라벨이 정확히 절반씩 갈려 항목 전체의 결과물을 기준으로 다시 판단한다. 항목 전체의 라벨을 고른다.",
}


def clean_reason(reason: str) -> str:
    """이유는 판정에 쓰지 않고 기록에만 남는다. 제어문자·특수 공백을 지우고, 개행·연속 공백을 한 칸으로 합치고, 길이를 제한한다 (정책 5-2).

    글자 수(200)와 UTF-8 바이트 수(600) 중 먼저 닿는 쪽까지만 둔다. 서식·방향 제어 같은 보이지 않는 문자(유니코드 분류 C*)는 지운다.
    """
    kept = "".join(" " if ch.isspace() else ch for ch in reason if ch.isspace() or not unicodedata.category(ch).startswith("C"))
    text = " ".join(kept.split())[:REASON_MAX_CHARS]
    return text.encode("utf-8")[:REASON_MAX_BYTES].decode("utf-8", "ignore")


class GateLabelResponse(BaseModel):
    # 클래스 docstring은 모델에 보내는 스키마 설명이 되므로 쓰지 않는다. 허용 라벨 검증은 요청마다 달라 코드가 한다
    label: str
    reason: str

    @field_validator("reason")
    @classmethod
    def _clean(cls, value: str) -> str:
        return clean_reason(value)  # 캐시에 들어가기 전에 정리한다 (응답 검증이 곧 정리다)


def call_id(posting_id: str, request: dict) -> str:
    """호출 식별자 (정책 5-2). 사용량 기록과 오류 메시지에서 어느 공고의 어느 항목·하위 업무인지 알아보는 용도다."""
    base = f"{posting_id}:item:{request['item_index']}"
    return base if request.get("sub_index") is None else f"{base}:subtask:{request['sub_index']}"


def _data(text: str) -> str:
    """공고 문장을 프롬프트에 넣기 전에 구분자 표시를 무력화한다 (공고 안의 표시가 데이터 블록을 닫아 버리지 못하게). 지시문을 막는 것은 아니다."""
    for mark in DELIMITERS:
        text = text.replace(mark, mark.replace("<", "< "))
    return text


def _family_text(family_id: str) -> str:
    fam = cfg.JOB_FAMILIES.get(family_id, {})
    return f"- `{family_id}` ({fam.get('name', family_id)}): {fam.get('definition', '')} 핵심 결과물: {fam.get('key_results', '')}"


def split_prompt(body: str) -> tuple[str, str]:
    """프롬프트 본문을 (시스템 지시 틀, 데이터 틀)로 나눈다. 값을 채우기 전에 나눠야 공고 문장이 경계를 흉내 낼 수 없다."""
    head, sep, tail = body.partition(DATA_MARKER)
    if not sep:
        raise ValueError(f"프롬프트에 {DATA_MARKER.strip()!r} 구분이 없어 시스템 지시와 데이터를 나눌 수 없다")
    return head, DATA_MARKER.lstrip("\n") + tail


def build_messages(request: dict, body: str) -> tuple[str, str]:
    """request에서 (시스템 지시, 사용자 메시지)를 만든다. 읽는 키는 level, text, item_text, other_items, context_items, candidates, allowed, families뿐이다.

    시스템 지시에는 코드가 채운 믿을 수 있는 글(규칙, 허용 라벨, 직무군 정의)만 들어간다. 공고에서 온 글은 모두 사용자 메시지에만 들어간다.
    """
    allowed = request["allowed"]
    shown = request.get("candidates") or request["families"]  # 후보가 없으면 지원하는 직무군 전체의 정의를 준다
    labels = "\n".join(
        f"- `{label}`" + (f" ({cfg.JOB_FAMILIES[label]['name']})" if label in cfg.JOB_FAMILIES else f" ({SPECIAL_LABEL_NAMES.get(label, label)})")
        for label in allowed
    )
    others = "\n".join(f"- {_data(t)}" for t in request["other_items"]) or "(없음)"
    context = "\n".join(f"- {_data(t)}" for t in request.get("context_items", [])) or "(없음)"
    is_item = request["level"] == "item"
    system_tpl, data_tpl = split_prompt(body)
    system = fill(
        system_tpl,
        labels=labels,
        families="\n".join(_family_text(f) for f in shown),
        supported=", ".join(cfg.JOB_FAMILIES[f]["name"] for f in request["families"]),  # 공통 기준 1을 따져 볼 지원 직무군 전체
    )
    user = fill(
        data_tpl,
        level_note=LEVEL_NOTES["item" if is_item else "subtask"],
        subtask=_data(request["text"]),
        item="(위와 같다)" if is_item else _data(request["item_text"]),
        others=others,
        context=context,
    )
    return system, user


def _squash(text: str) -> str:
    return " ".join(text.split())


def copies_posting(reason: str, request: dict) -> bool:
    """reason에 요청의 공고 문장이 COPY_GUARD_CHARS자 이상 이어서 그대로 들어 있는가(공백 차이는 무시)."""
    r = _squash(reason)
    if len(r) < COPY_GUARD_CHARS:
        return False
    sources = [_squash(t) for t in (request["text"], request["item_text"], *request["other_items"], *request.get("context_items", []))]
    return any(r[i:i + COPY_GUARD_CHARS] in src for i in range(len(r) - COPY_GUARD_CHARS + 1) for src in sources)


def make_llm_judge(client: LLMClient, posting_id: str, prompt: tuple[str, str] | None = None) -> Callable[[dict], dict]:
    """공고 하나의 AiJudge를 만든다 (core.job_gate.AiJudge 형태: request -> {"label", "reason"}).

    client는 LLMClient(실제는 CachedClient로 감싼 GeminiClient, 테스트는 FakeClient)다. 호출마다 새로 묻고 다시 묻지 않는다.
    prompt는 (version, 본문)이다. 비우면 기본 프롬프트(prompts/gate_label.md)를 쓴다. 실험 변형은 core.prompts.load_prompt_file로 읽어 넘긴다 (정책 6-5).
    """
    version, body = prompt or load_prompt(STAGE)
    split_prompt(body)  # 구분이 없는 프롬프트는 호출 전에 알린다

    def judge(request: dict) -> dict:
        cid = call_id(posting_id, request)
        system, prompt = build_messages(request, body)
        llm_request = LLMRequest(
            stage=STAGE, prompt=prompt, schema=GateLabelResponse, job_id=cid,
            prompt_version=version, contains_profile=False, system=system,
        )
        data = client.complete_json(llm_request).data  # 통신·스키마 실패는 LLMStageError로 올라온다. reason은 이미 정리돼 있다
        if data["label"] not in request["allowed"]:
            raise LLMStageError(f"{STAGE}({cid}): 허용되지 않은 라벨")
        reason = data["reason"]
        if not reason:
            raise LLMStageError(f"{STAGE}({cid}): reason이 비어 있다")
        if copies_posting(reason, request):  # 공고 문장이 기록으로 번지지 않게 한다
            reason = COPY_PLACEHOLDER
        return {"label": data["label"], "reason": reason}

    judge.client = client  # 이 judge가 실제로 부르는 클라이언트. run_pipeline이 gate_client와 같은지 확인한다
    return judge
