"""자소서·포트폴리오 초안의 입력 구성 (docs/policies/application-writing.md 2장, 이슈 #14). LLM과 네트워크를 쓰지 않는다.

- 자격요건 결과에서 요건을 쓸 수 있는 사실(F) / 확인 필요(C) / 주장 금지(X)로 나누는 일은 코드가 한다.
- 입력(WriterInput)에는 프로필 내용이 들어 있으므로 디스크에 쓰지 않는다. 같은 프로세스 안에서 함수 호출로만 넘긴다.
- 선택한 공고의 경고 문구는 SelectedJob에서 그대로 복사한다. 이후 단계가 고치지 못한다.
"""
from typing import Literal

from pydantic import BaseModel, Field

from agents.profile_analyzer import profile_payload
from core.rules import check_profile_evidence
from core.rules_ai import evidence_problem
from core.schemas import EligibilityProfile, JobRequirement, ProfileEvidence, RequirementResult
from core.selection import SelectedJob

WRITER_INPUT_VERSION = "writer_input.v1"
FACT_PREFIX, CONFIRM_PREFIX, FORBIDDEN_PREFIX = "F", "C", "X"
DEFAULT_QUESTION = "프로필에서 근거를 찾지 못했습니다. 이 요건을 충족하나요?"
NO_EVIDENCE_REASON = "no_evidence"  # 정책 2장: met인데 근거 인용이 없는 skill·qualitative 요건


class WriterInputError(ValueError):
    """입력을 만들 수 없다. 메시지에는 프로필 내용이나 공고 원문을 넣지 않는다."""


class JobInfo(BaseModel):
    """공고 내용. 자격요건 외의 칸(직무명, 업무 항목)은 선택 입력이다."""

    company_name: str | None = None
    job_title: str | None = None
    position: str | None = None
    main_tasks: list[str] = []


class JobRequirementText(BaseModel):
    requirement_id: str
    level: Literal["required", "preferred"]
    text: str


class EvidenceRef(BaseModel):
    experience_id: str | None = None
    field: str | None = None
    quote: str


class ExperienceDetail(BaseModel):
    """사실이 가리킨 경험의 내용. 칸은 role, actions, tools, result만이다(제목, 기간, 분류값은 붙이지 않는다, 정책 2장)."""

    experience_id: str
    role: str | None = None
    actions: list[str] = []
    tools: list[str] = []
    result: str | None = None


class UsableFact(BaseModel):
    fact_id: str
    requirement_id: str
    item_index: int | None = None
    text: str
    evidence: list[EvidenceRef] = []
    computed: str | None = Field(default=None, description="숫자 요건에서 코드가 만든 비교식")
    evidence_detail: list[ExperienceDetail] = Field(default=[], description="근거가 경험일 때 인용된 경험 모두의 지정 칸. 프로필 원문이라 저장하지 않는다")


class TaskLink(BaseModel):
    """주요 업무 연결 후보 (정책 2장, 결정 D14). 사실(usable_facts)이 아니다: "이 경험을 이 업무와 연결해 설명할 수 있다"는 후보일 뿐이다.

    task_id는 업무 항목의 순서(T1…)다. 인용이 프로필과 맞는지만 코드가 보장하고, 업무와 정말 관련 있는지는 보장하지 않는다.
    프로필 원문을 담고 있어 WriterInput처럼 저장하지 않는다(사람이 확정한 가상 프로필 fixture만 예외, 정책 5장).
    """

    task_id: str
    text: str
    label: str | None = None  # 공고가 업무에 붙인 짧은 이름. 있으면 프롬프트에는 text 대신 label을 준다 (정책 2장 D20)
    evidence: list[EvidenceRef]
    evidence_detail: list[ExperienceDetail] = []


class ToConfirm(BaseModel):
    confirm_id: str
    requirement_id: str
    item_index: int | None = None
    text: str
    reason: str
    question: str


class DoNotClaim(BaseModel):
    forbidden_id: str
    requirement_id: str
    item_index: int | None = None
    text: str


class WriterMeta(BaseModel):
    run_id: str
    job_id: str
    profile_id: str
    is_synthetic: bool


class WriterInput(BaseModel):
    """초안 생성의 입력. 프로필 내용이 있으므로 저장하지 않는다."""

    version: Literal["writer_input.v1"] = WRITER_INPUT_VERSION
    job: "WriterJob"
    profile: dict
    usable_facts: list[UsableFact]
    to_confirm: list[ToConfirm]
    do_not_claim: list[DoNotClaim]
    notices: list[str]
    meta: WriterMeta
    task_links: list[TaskLink] = []  # 사실이 아니다. usable_facts·to_confirm·do_not_claim에 들어가지 않는다

    @property
    def fact_ids(self) -> set[str]:
        return {f.fact_id for f in self.usable_facts}

    @property
    def task_ids(self) -> set[str]:
        return {t.task_id for t in self.task_links}

    @property
    def forbidden_ids(self) -> set[str]:
        return {c.confirm_id for c in self.to_confirm} | {x.forbidden_id for x in self.do_not_claim}


class WriterJob(BaseModel):
    job_id: str
    job_label: str | None = None
    company_name: str | None = None
    job_title: str | None = None
    position: str | None = None
    main_tasks: list[str] = []
    requirements: list[JobRequirementText]


WriterInput.model_rebuild()


def _refs(evidence: list[ProfileEvidence]) -> list[EvidenceRef]:
    return [EvidenceRef(experience_id=e.experience_id, field=e.field, quote=e.quote) for e in evidence]


def experience_details(evidence: list[ProfileEvidence], profile: EligibilityProfile) -> list[ExperienceDetail]:
    """인용된 경험마다 지정한 네 칸만 복사한다(경험 전체를 복사하지 않는다). 칸(field) 근거에는 붙이지 않는다."""
    by_id = {x.id: x for x in profile.experiences}
    out: dict[str, ExperienceDetail] = {}
    for e in evidence:
        if e.experience_id and e.experience_id in by_id and e.experience_id not in out:
            x = by_id[e.experience_id]
            out[e.experience_id] = ExperienceDetail(experience_id=x.id, role=x.role, actions=list(x.actions), tools=list(x.tools), result=x.result)
    return list(out.values())


def _bodies(req: JobRequirement, res: RequirementResult):
    """(item_index, 문장, 판정 칸)들. any_of는 항목별로, 아니면 요건 하나다."""
    if req.combine == "any_of":
        text = {it.index: it.text for it in req.items}
        return [(it.item_index, text[it.item_index], it) for it in res.item_results]
    return [(None, req.text, res)]


def build_writer_input(selected: SelectedJob, profile: EligibilityProfile, info: JobInfo | None = None,
                       task_links: list[TaskLink] | None = None) -> WriterInput:
    """선택한 공고와 프로필에서 초안 입력을 만든다. 쓸 수 있는 사실의 분류는 여기서 코드가 끝낸다.

    selected는 프로필 인용을 담고 있어야 한다(profile_evidence_included). 인용이 프로필 원문과 맞는지 다시 대조한다.
    """
    if not selected.profile_evidence_included:
        raise WriterInputError("선택한 공고에 프로필 인용이 없어 쓸 수 있는 사실을 만들 수 없다 (--save-profile-evidence)")
    results = selected.eligibility.requirement_results
    try:
        check_profile_evidence(results, profile)
    except ValueError:
        raise WriterInputError("프로필 근거 인용이 프로필 원문과 맞지 않는다") from None
    for link in task_links or []:  # 연결 후보의 인용도 프로필 원문과 다시 대조한다. 어긋나면 입력을 만들지 않는다
        if any(evidence_problem(profile, ProfileEvidence(experience_id=e.experience_id, field=e.field, quote=e.quote)) for e in link.evidence):
            raise WriterInputError("업무 연결 후보의 인용이 프로필 원문과 맞지 않는다")

    by_id = {r.requirement_id: r for r in results}
    facts: list[UsableFact] = []
    confirms: list[ToConfirm] = []
    forbidden: list[DoNotClaim] = []
    for req in selected.eligibility.requirements:
        if req.type == "preference":
            continue
        res = by_id[req.id]
        for index, text, body in _bodies(req, res):
            if body.status == "met":
                evidence = _refs(body.profile_evidence)
                if not evidence and req.type != "numeric":
                    confirms.append(ToConfirm(confirm_id=f"{CONFIRM_PREFIX}{len(confirms) + 1}", requirement_id=req.id, item_index=index,
                                              text=text, reason=NO_EVIDENCE_REASON, question=body.question_for_user or DEFAULT_QUESTION))
                    continue
                facts.append(UsableFact(fact_id=f"{FACT_PREFIX}{len(facts) + 1}", requirement_id=req.id, item_index=index, text=text,
                                        evidence=evidence, computed=body.compared,
                                        evidence_detail=experience_details(body.profile_evidence, profile)))
            elif body.status == "needs_check":
                confirms.append(ToConfirm(confirm_id=f"{CONFIRM_PREFIX}{len(confirms) + 1}", requirement_id=req.id, item_index=index,
                                          text=text, reason=body.reason_code or "scope_unclear",
                                          question=body.question_for_user or DEFAULT_QUESTION))
            elif body.status == "not_met":
                forbidden.append(DoNotClaim(forbidden_id=f"{FORBIDDEN_PREFIX}{len(forbidden) + 1}", requirement_id=req.id,
                                            item_index=index, text=text))
    info = info or JobInfo()
    return WriterInput(
        job=WriterJob(job_id=selected.job_id, job_label=selected.job_label, company_name=info.company_name, job_title=info.job_title,
                      position=info.position, main_tasks=info.main_tasks,
                      requirements=[JobRequirementText(requirement_id=r.id, level=r.level, text=r.text)
                                    for r in selected.eligibility.requirements if r.type != "preference"]),
        profile=profile_payload(profile),
        usable_facts=facts, to_confirm=confirms, do_not_claim=forbidden,
        notices=list(selected.selection_notices),  # 코드가 복사한다. 이후 단계가 고치지 못한다
        meta=WriterMeta(run_id=selected.run_id, job_id=selected.job_id, profile_id=selected.profile.profile_id,
                        is_synthetic=selected.profile.is_synthetic),
        task_links=list(task_links or []),
    )
