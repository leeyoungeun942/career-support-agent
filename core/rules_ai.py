"""AI 결과의 상태 검증, any_of 합치기, 프로필 근거 검증 (docs/policies/eligibility.md 13장, 이슈 #4).

- LLM과 네트워크를 쓰지 않는다. LLM이 낸 결과를 코드가 검증하고 고친다 (AGENTS.md).
- 공개 창구는 core/rules.py다. 호출하는 쪽은 core.rules에서 가져온다.
"""
import re
from collections.abc import Iterable

from core.schemas import (
    NEEDS_CHECK_REASONS,
    EligibilityProfile,
    Experience,
    ItemResult,
    JobRequirement,
    ProfileEvidence,
    RequirementResult,
    decision_item,
)

# =====================================================================
# AI 결과 상태 검증과 any_of 합치기 (eligibility.md 13장 4~5단계, 2장)
# =====================================================================

NOTE_AI_NOT_MET_ADJUSTED = "AI가 미충족으로 판단해 확인 필요로 조정"


def _as_ai_body(body: dict) -> tuple[dict, bool]:
    """판정 칸 1개를 AI가 낸 것으로 다룬다: 판정 주체를 ai로 덮고, not_met은 needs_check로 되돌린다. (결과, 되돌렸는가)"""
    if body.get("status") is None:  # preference는 판정 대상이 아니다
        return body, False
    body = {**body, "status_decided_by": "ai"}
    if body["status"] != "not_met":
        return body, False
    reason = body.get("reason_code")
    return {**body, "status": "needs_check", "reason_code": reason if reason in NEEDS_CHECK_REASONS else "scope_unclear"}, True


def normalize_ai_result(raw: dict) -> dict:
    """LLM 원시 결과(딕셔너리)를 AI가 낸 것으로 다룬다. 스키마 검증 전에 쓴다 (13장 4단계).

    안에 적힌 status_decided_by는 믿지 않고 ai로 덮고, not_met은 예외 없이 needs_check로 되돌린다.
    코드가 판정한 결과와 사용자가 정한 판정(user_denied)은 이 함수를 거치지 않고 검증된 결과 객체로 들어온다.
    any_of는 항목마다 같은 처리를 하고, 하나라도 되돌리면 요건의 interpretation_note에 남긴다. 입력은 바꾸지 않는다.
    """
    out, changed = _as_ai_body(raw)
    if "item_results" in raw:
        reverted = [_as_ai_body(item) for item in raw["item_results"]]
        out = {**out, "item_results": [item for item, _ in reverted]}
        changed = changed or any(did for _, did in reverted)
    if changed:
        note = out.get("interpretation_note")
        out = {**out, "interpretation_note": f"{note} / {NOTE_AI_NOT_MET_ADJUSTED}" if note else NOTE_AI_NOT_MET_ADJUSTED}
    return out


def merge_any_of(
    req: JobRequirement, item_results: list[ItemResult], interpretation_note: str | None = None
) -> RequirementResult:
    """any_of 항목 결과를 요건 전체 결과로 합친다. 값은 결정 항목에서 옮기고 근거·질문은 항목에만 둔다 (data-contract 2-4)."""
    if req.combine != "any_of":
        raise ValueError(f"요건 {req.id}: combine={req.combine}는 any_of가 아니다")
    ordered = sorted(item_results, key=lambda r: r.item_index)
    if [r.item_index for r in ordered] != [it.index for it in req.items]:
        raise ValueError(f"요건 {req.id}: item_results의 item_index가 items와 1:1이 아니다")
    decided = decision_item(ordered)
    return RequirementResult(
        requirement_id=req.id,
        status=decided.status,
        reason_code=decided.reason_code,
        evidence_checked_by=decided.evidence_checked_by,
        status_decided_by=decided.status_decided_by,
        interpretation_note=interpretation_note,
        item_results=ordered,
    )


# =====================================================================
# 프로필 근거 검증 (eligibility.md 13장, data-contract 2-6)
# =====================================================================

# 스키마는 프로필을 모르므로 이 검사는 코드가 한다. field로 쓸 수 있는 목록 칸 (skills_denied는 근거가 아니다)
EVIDENCE_LIST_FIELDS = ("skills", "certificates", "other_qualifications")
_EDUCATION_FIELD = re.compile(r"education\[(\d+)\]")


def _experience_pieces(exp: Experience) -> list[str]:
    """경험 원문 조각. quote는 이 중 한 조각 안에 글자 그대로 있어야 한다."""
    return [p for p in (exp.title, exp.role, exp.result, *exp.actions, *exp.tools) if p]


def _field_pieces(profile: EligibilityProfile, field: str) -> list[str] | None:
    """field 근거의 원문 조각. 쓸 수 없는 field이면 None."""
    if field in EVIDENCE_LIST_FIELDS:
        return list(getattr(profile, field))
    if m := _EDUCATION_FIELD.fullmatch(field):
        index = int(m.group(1))
        if index < len(profile.education) and profile.education[index].raw:
            return [profile.education[index].raw]
    return None


def _evidence_problem(profile: EligibilityProfile, ev: ProfileEvidence) -> str | None:
    if ev.experience_id is not None:
        exp = next((e for e in profile.experiences if e.id == ev.experience_id), None)
        if exp is None:
            return f"프로필에 없는 experience_id {ev.experience_id!r}"
        pieces, where = _experience_pieces(exp), f"경험 {ev.experience_id}"
    else:
        pieces, where = _field_pieces(profile, ev.field), f"field {ev.field!r}"
        if pieces is None:
            return f"근거로 쓸 수 없는 {where} (없는 칸, 범위 밖 education[i], raw 없음)"
    if not any(ev.quote in piece for piece in pieces):
        return f"quote가 {where}의 원문에 없다: {ev.quote!r}"
    return None


class ProfileEvidenceError(ValueError):
    """프로필 근거 인용이 프로필 원문과 맞지 않는다. 호출하는 쪽이 실패 범주를 메시지가 아니라 이 타입으로 가른다.

    ValueError의 하위 타입이라 기존에 ValueError를 잡던 곳은 그대로 동작한다.
    """


def check_profile_evidence(results: Iterable[RequirementResult], profile: EligibilityProfile) -> None:
    """모든 profile_evidence(any_of 항목 포함)가 프로필 원문에 있는지 검사한다. 어긋난 곳을 모아 ProfileEvidenceError(ValueError)로 낸다."""
    problems = []
    for res in results:
        located = [(res.requirement_id, ev) for ev in res.profile_evidence]
        located += [(f"{res.requirement_id}[항목 {it.item_index}]", ev) for it in res.item_results for ev in it.profile_evidence]
        for where, ev in located:
            if problem := _evidence_problem(profile, ev):
                problems.append(f"{where}: {problem}")
    if problems:
        raise ProfileEvidenceError("\n".join(["프로필 근거 검증 실패", *problems]))
