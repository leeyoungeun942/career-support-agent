"""프로필 분석 Agent: skill·qualitative 요건마다 프로필에서 근거를 찾는다 (정책 eligibility.md 13장 3단계, 13-1, 16장).

LLM은 요건 id별 판정(상태, 사유, 근거 인용, 질문)만 낸다. 판정 주체, not_met 되돌림, any_of 합치기, 근거 대조는 core.rules.build_result가 한다.
"""
import json

from pydantic import BaseModel

from core import rules
from core.llm_client import LLMClient, LLMRequest, LLMStageError
from core.privacy import reject_contact_info
from core.prompts import fill, load_prompt
from core.schemas import EligibilityProfile, JobRequirement, NeedsCheckReason, NotMetReason, RequirementStatus

STAGE = "find_evidence"
NOTE_NO_QUOTE = "근거 인용이 없어 확인 필요로 조정"
QUESTION_NO_QUOTE = "프로필에서 근거를 찾지 못했습니다. 이 요건을 충족하나요?"


class QuoteOut(BaseModel):
    experience_id: str | None = None
    field: str | None = None
    quote: str


class ItemJudgement(BaseModel):
    item_index: int
    status: RequirementStatus
    reason_code: NeedsCheckReason | NotMetReason | None = None
    profile_evidence: list[QuoteOut] = []
    question_for_user: str | None = None


class EvidenceJudgement(BaseModel):
    requirement_id: str
    status: RequirementStatus | None = None  # any_of 요건은 항목별(item_results)로만 낸다
    reason_code: NeedsCheckReason | NotMetReason | None = None
    profile_evidence: list[QuoteOut] = []
    question_for_user: str | None = None
    item_results: list[ItemJudgement] = []


class EvidenceResponse(BaseModel):
    judgements: list[EvidenceJudgement]


def profile_payload(profile: EligibilityProfile) -> dict:
    """외부로 보내는 프로필 입력. 이름·연락처·주소·링크를 빼고 경험, 스킬, 자격증, 학력 원문만 보낸다 (정책 16장)."""
    return {
        "education": [e.model_dump(mode="json", include={"degree", "majors", "status", "graduation_month", "raw"})
                      for e in profile.education],
        "experiences": [x.model_dump(mode="json", include={"id", "title", "type", "period", "role", "actions", "tools", "result"})
                        for x in profile.experiences],
        "skills": list(profile.skills),
        "certificates": list(profile.certificates),
        "other_qualifications": list(profile.other_qualifications),
    }


def _targets(requirements: list[JobRequirement]) -> list[JobRequirement]:
    return [r for r in requirements if r.type in ("skill", "qualitative")]


def _requirement_payload(req: JobRequirement) -> dict:
    out: dict = {"id": req.id, "type": req.type, "text": req.text}
    if req.combine == "any_of":
        out["items"] = [{"index": it.index, "text": it.text} for it in req.items]
    return out


def find_evidence(
    requirements: list[JobRequirement], profile: EligibilityProfile, client: LLMClient,
    allow_external_profile: bool = False, job_id: str = "",
) -> dict[str, dict]:
    """skill·qualitative 요건마다 프로필에서 근거를 찾는다 (정책 13장 3단계).

    요건 id -> 원시 판정(딕셔너리)을 낸다. core.rules.build_result(judgements=...)에 그대로 넘긴다.
    status는 met/needs_check만 제안한다. not_met은 코드와 사용자 몫이고, 나와도 build_result가 되돌린다.
    근거(profile_evidence)는 프로필 원문 인용이어야 하고, 어긋나면 build_result가 결과를 만들지 않는다.
    외부로 나가는 클라이언트에는 allow_external_profile=True를 명시해야 프로필을 보낸다. 기본은 PermissionError (정책 16장).
    통신 실패, 스키마 위반, 요건 누락·중복은 LLMStageError다. 확인하지 못한 요건을 조용히 needs_check로 채우지 않는다.
    """
    external = getattr(client, "external", True)
    if external and not allow_external_profile:
        raise PermissionError("외부 LLM으로 프로필을 보내려면 allow_external_profile=True를 명시해야 한다 (정책 16장)")
    if external:
        reject_contact_info(profile_payload(profile))  # 문장 칸 안의 연락처 형식은 칸 이름 기준 제외로 걸러지지 않는다
    targets = _targets(requirements)
    if not targets:
        return {}
    version, body = load_prompt(STAGE)
    prompt = fill(
        body,
        requirements=json.dumps([_requirement_payload(r) for r in targets], ensure_ascii=False, indent=1),
        profile=json.dumps(profile_payload(profile), ensure_ascii=False, indent=1),
    )
    request = LLMRequest(stage=STAGE, prompt=prompt, schema=EvidenceResponse, job_id=job_id, prompt_version=version,
                         contains_profile=True)
    data = client.complete_json(request).data  # 통신·스키마 실패는 LLMStageError로 올라간다 (다시 묻지 않는다)

    by_id: dict[str, dict] = {}
    for j in data["judgements"]:
        if j["requirement_id"] in by_id:
            raise LLMStageError(f"{STAGE}({job_id}): 같은 요건의 판정이 둘이다: {j['requirement_id']}")
        by_id[j["requirement_id"]] = j
    wanted = {r.id for r in targets}
    if missing := sorted(wanted - by_id.keys()):
        raise LLMStageError(f"{STAGE}({job_id}): 판정이 빠진 요건: {missing}")
    if extra := sorted(by_id.keys() - wanted):
        raise LLMStageError(f"{STAGE}({job_id}): 묻지 않은 요건의 판정: {extra}")

    judgements = {}
    for req in targets:
        raw = _to_raw(req, by_id[req.id])
        try:
            rules.injected_result(req, raw)  # 계약을 어기는 조합(예: needs_check인데 사유 없음)을 여기서 잡는다
        except ValueError:
            raise LLMStageError(f"{STAGE}({job_id}): 요건 {req.id}의 판정이 계약을 어겼다") from None
        judgements[req.id] = raw
    return judgements


def _body(j: dict, job_quote: str) -> tuple[dict, bool]:
    """판정 칸 1개를 원시 딕셔너리로. 근거 인용이 없는 met은 확인 필요로 낮춘다(근거가 불충분하면 needs_check, 정책 13장).

    (판정, 낮췄는가)를 돌려준다.
    """
    status, reason, question = j["status"], j["reason_code"], j["question_for_user"]
    evidence = list(j["profile_evidence"])
    lowered = status == "met" and not evidence
    if status == "met":
        reason = None
    if lowered:
        status, reason, question = "needs_check", "scope_unclear", question or QUESTION_NO_QUOTE
    body = {"status": status, "reason_code": reason, "profile_evidence": evidence, "question_for_user": question,
            "job_quote": job_quote, "evidence_checked_by": "ai", "status_decided_by": "ai"}
    return body, lowered


def _to_raw(req: JobRequirement, j: dict) -> dict:
    if req.combine == "any_of":
        if not j["item_results"]:
            raise LLMStageError(f"{STAGE}: any_of 요건 {req.id}는 항목별(item_results)로 판정해야 한다")
        # 항목별 결과와 함께 온 요건 단위 status·사유·근거·질문은 쓰지 않는다. 요건 전체 값은 항목에서 합친다 (정책 13-1, 5단계)
        item_text = {it.index: it.text for it in req.items}
        items, lowered = [], False
        for item in j["item_results"]:
            if item["item_index"] not in item_text:
                raise LLMStageError(f"{STAGE}: 요건 {req.id}에 없는 항목 {item['item_index']}")
            body, low = _body(item, item_text[item["item_index"]])
            items.append({"item_index": item["item_index"], **body})
            lowered = lowered or low
        raw = {"item_results": items}
    else:
        if j["status"] is None or j["item_results"]:
            raise LLMStageError(f"{STAGE}: 요건 {req.id}는 요건 단위(status)로만 판정해야 한다")
        raw, lowered = _body(j, req.text)
    if lowered:
        raw["interpretation_note"] = NOTE_NO_QUOTE
    return raw
