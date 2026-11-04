"""agents/profile_analyzer.py 근거 찾기 검증 (정책 eligibility.md 13장 3단계, 13-1, 16장; 이슈 #6). 네트워크를 쓰지 않는다."""
import json
from pathlib import Path

import pytest

from agents.profile_analyzer import EvidenceResponse, find_evidence, profile_payload
from core.llm_client import (
    CachedClient,
    FakeClient,
    GeminiClient,
    GeminiSettings,
    LLMQuotaExceeded,
    LLMStageError,
)
from core.llm_mock import evidence_answer, make_evidence_mock
from core.rules import build_result
from core.schemas import EligibilityProfile, JobGateResult, JobRequirement

FIXTURES = Path(__file__).parent / "fixtures"
DATA = json.loads((FIXTURES / "eligibility_sample_expected.json").read_text(encoding="utf-8"))
EVIDENCE = json.loads((FIXTURES / "eligibility_evidence_mock.json").read_text(encoding="utf-8"))["judgements"]
SAMPLES = DATA["samples"]
JUDGED_KEYS = ("status", "reason_code", "compared", "item_statuses")


def profile():
    return EligibilityProfile(**{**DATA["profile"], "as_of_month": DATA["as_of_month"]})


def gate(sample):
    return JobGateResult(job_id=sample["job_id"], desired_family="JF_SERVICE_PLANNING", result="match", summary="테스트",
                         core_family="JF_SERVICE_PLANNING")


def fixture_requirements(sample):
    return [JobRequirement(**{k: v for k, v in r.items() if k not in JUDGED_KEYS}) for r in sample["requirements"]]


def skill(req_id="R1", text="Git 협업", **kw):
    return JobRequirement(id=req_id, level="required", type="skill", text=text, **kw)


def judgement(req_id="R1", **kw):
    return {"requirement_id": req_id, "status": "needs_check", "reason_code": "no_profile_info",
            "question_for_user": "Git으로 협업해 본 적이 있나요?", **kw}


def run(reqs, judgements, **kw):
    fake = FakeClient([{"judgements": judgements}])
    return find_evidence(reqs, profile(), fake, job_id="job1", **kw), fake


def expected_status(req):
    if req["type"] == "preference":
        return None
    if "item_statuses" in req:
        statuses = [st for st, _ in req["item_statuses"]]
        return "met" if "met" in statuses else ("not_met" if set(statuses) == {"not_met"} else "needs_check")
    return req["status"]


# ---------------------------------------------------------------- mock: 샘플 6건이 정책 17장과 같다

@pytest.mark.parametrize("sample", SAMPLES, ids=[f"sample-{s['number']}" for s in SAMPLES])
def test_mock_evidence_reproduces_policy_17_statuses_and_passes_quote_check(sample):
    reqs = fixture_requirements(sample)
    client = make_evidence_mock([sample], EVIDENCE, code_ids=False)
    judgements = find_evidence(reqs, profile(), client, job_id=sample["job_id"])
    result = build_result(gate(sample), profile(), reqs, judgements)  # 인용이 프로필 원문에 없으면 여기서 오류
    assert result.verdict == sample["expected"]["verdict"]
    assert result.decisive_requirement_ids == sample["expected"]["decisive"]
    for req, res in zip(sample["requirements"], result.requirement_results):
        assert res.status == expected_status(req), req["id"]
        if req["type"] in ("skill", "qualitative") and "item_statuses" not in req:
            assert res.reason_code == req.get("reason_code"), req["id"]
            assert (res.evidence_checked_by, res.status_decided_by) == ("ai", "ai")


def test_every_mock_judgement_matches_a_fixture_requirement_and_vice_versa():
    ids = {r["id"] for s in SAMPLES for r in s["requirements"] if r["type"] in ("skill", "qualitative")}
    assert ids == set(EVIDENCE)


def test_mock_met_has_evidence_and_no_profile_info_has_none():
    for key, j in EVIDENCE.items():
        for body in j.get("item_results", [j]):
            if body["status"] == "met":
                assert body["profile_evidence"], key
            if body.get("reason_code") == "no_profile_info":
                assert not body.get("profile_evidence"), key


def test_mock_answer_fits_the_response_schema_and_uses_code_ids():
    for s in SAMPLES:
        EvidenceResponse.model_validate(evidence_answer(s, EVIDENCE))
    ids = [j["requirement_id"] for j in evidence_answer(SAMPLES[0], EVIDENCE)["judgements"]]
    assert ids == ["R3", "R4", "R5", "R6", "R7"]


# ---------------------------------------------------------------- 개인정보 가드 (정책 16장, D4)

def test_external_client_without_permission_is_refused_before_any_call():
    calls = []
    client = GeminiClient(GeminiSettings("k", "m"), generate=lambda r: calls.append(r) or ("{}", 0, 0))
    with pytest.raises(PermissionError):
        find_evidence([skill()], profile(), client)
    assert calls == []


def test_external_client_with_explicit_permission_goes_through():
    client = GeminiClient(GeminiSettings("k", "m", rpm=60), generate=lambda r: (json.dumps({"judgements": [judgement()]}), 1, 1),
                          sleep=lambda s: None)
    assert "R1" in find_evidence([skill()], profile(), client, allow_external_profile=True)


def test_cached_wrapper_inherits_externality(tmp_path):
    external = CachedClient(GeminiClient(GeminiSettings("k", "m")), extract_dir=tmp_path)
    with pytest.raises(PermissionError):
        find_evidence([skill()], profile(), external)
    internal = CachedClient(FakeClient([{"judgements": [judgement()]}]), extract_dir=tmp_path)
    assert "R1" in find_evidence([skill()], profile(), internal)


def test_unknown_client_kind_is_treated_as_external():
    class Plain:
        model = "x"

        def complete_json(self, request):
            raise AssertionError("호출되면 안 된다")

    with pytest.raises(PermissionError):
        find_evidence([skill()], profile(), Plain())


def test_sent_profile_has_only_allowed_fields_and_no_personal_data():
    p = profile()
    payload = profile_payload(p)
    assert set(payload) == {"education", "experiences", "skills", "certificates", "other_qualifications"}
    assert set(payload["experiences"][0]) <= {"id", "title", "type", "period", "role", "actions", "tools", "result"}
    assert set(payload["education"][0]) <= {"degree", "majors", "status", "graduation_month", "raw"}
    # 프로필에 이름·연락처가 섞여 들어와 있어도 보내지 않는다(허용 목록 방식)
    dirty = p.model_copy(update={"name": "김하늘", "email": "a@b.c", "phone": "010-0000-0000"})
    text = json.dumps(profile_payload(dirty), ensure_ascii=False)
    assert "a@b.c" not in text and "010-0000-0000" not in text and "김하늘" not in text


def test_prompt_contains_requirements_and_profile_and_is_marked_as_profile_request():
    fake = FakeClient([{"judgements": [judgement()]}])
    find_evidence([skill()], profile(), fake, job_id="job1")
    r = fake.requests[0]
    assert r.contains_profile and r.stage == "find_evidence" and "{{" not in r.prompt
    assert "Git 협업" in r.prompt and "B2B AI 사업부 기획팀 인턴" in r.prompt


def test_profile_request_is_not_written_to_disk_cache_by_default(tmp_path):
    cached = CachedClient(FakeClient([{"judgements": [judgement()]}]), extract_dir=tmp_path / "ex", profile_dir=tmp_path / "pr")
    find_evidence([skill()], profile(), cached)
    assert not (tmp_path / "pr").exists() and not (tmp_path / "ex").exists()


def test_numeric_and_preference_requirements_are_not_sent():
    reqs = [skill(), JobRequirement(id="R2", level="required", type="numeric", text="대졸 이상", numeric_kind="education",
                                    min_degree="associate"),
            JobRequirement(id="R3", level="preferred", type="preference", text="확인하는 분")]
    judgements, fake = run(reqs, [judgement()])
    assert list(judgements) == ["R1"] and "대졸 이상" not in fake.requests[0].prompt and "확인하는 분" not in fake.requests[0].prompt


def test_no_skill_or_qualitative_requirements_means_no_call():
    fake = FakeClient([])
    assert find_evidence([], profile(), fake) == {} and fake.requests == []


# ---------------------------------------------------------------- 응답 검증과 실패

def test_schema_violation_raises_stage_error_without_reasking():
    fake = FakeClient([{"judgements": "목록이 아님"}, {"judgements": [judgement()]}])
    with pytest.raises(LLMStageError):
        find_evidence([skill()], profile(), fake)
    assert len(fake.requests) == 1


def test_stage_error_and_quota_propagate():
    with pytest.raises(LLMStageError):
        find_evidence([skill()], profile(), FakeClient([LLMStageError("통신 실패")]))
    with pytest.raises(LLMQuotaExceeded):
        find_evidence([skill()], profile(), FakeClient([LLMQuotaExceeded("소진")]))


def test_missing_requirement_judgement_is_an_error_not_a_silent_needs_check():
    with pytest.raises(LLMStageError, match="R2"):
        run([skill(), skill("R2", "다른 요건")], [judgement()])


def test_duplicate_and_unasked_judgements_are_errors():
    with pytest.raises(LLMStageError, match="둘"):
        run([skill()], [judgement(), judgement()])
    with pytest.raises(LLMStageError, match="R9"):
        run([skill()], [judgement(), judgement("R9")])


@pytest.mark.parametrize("bad", [
    judgement(reason_code=None),                                # needs_check인데 사유가 없다
    judgement(profile_evidence=[{"quote": "위치 없는 인용"}]),    # experience_id도 field도 없다
    judgement(profile_evidence=[{"experience_id": "exp-1", "field": "skills", "quote": "둘 다"}]),
])
def test_contract_breaking_judgement_is_a_stage_error(bad):
    with pytest.raises(LLMStageError, match="R1"):
        run([skill()], [bad])


def test_any_of_requires_item_level_judgements_and_plain_requires_status():
    anyof = skill(text="영어가능자, 운전가능자", combine="any_of",
                  items=[{"index": 0, "text": "영어가능자"}, {"index": 1, "text": "운전가능자"}])
    with pytest.raises(LLMStageError, match="항목별"):
        run([anyof], [judgement()])
    with pytest.raises(LLMStageError, match="요건 단위"):
        run([skill()], [{"requirement_id": "R1", "item_results": [{"item_index": 0, "status": "met"}]}])
    with pytest.raises(LLMStageError, match="없는 항목"):
        run([anyof], [{"requirement_id": "R1", "item_results": [{"item_index": 5, "status": "needs_check", "reason_code": "no_profile_info"}]}])


def test_any_of_is_judged_per_item_and_merged_by_build_result():
    anyof = skill(text="영어가능자, 운전가능자", combine="any_of",
                  items=[{"index": 0, "text": "영어가능자"}, {"index": 1, "text": "운전가능자"}])
    item_met = {"item_index": 0, "status": "met", "profile_evidence": [{"field": "certificates", "quote": "TOEIC Speaking IH"}]}
    item_chk = {"item_index": 1, "status": "needs_check", "reason_code": "no_profile_info", "question_for_user": "운전이 가능한가요?"}
    judgements, _ = run([anyof], [{"requirement_id": "R1", "item_results": [item_met, item_chk]}])
    result = build_result(gate(SAMPLES[0]), profile(), [anyof], judgements)
    res = result.requirement_results[0]
    assert res.status == "met" and [i.status for i in res.item_results] == ["met", "needs_check"]


# ---------------------------------------------------------------- build_result와의 맞물림 (4단계, 근거 대조)

def test_model_not_met_is_reverted_to_needs_check_by_build_result():
    req = skill()
    judgements, _ = run([req], [judgement(status="not_met", reason_code="user_denied")])
    result = build_result(gate(SAMPLES[0]), profile(), [req], judgements)
    res = result.requirement_results[0]
    assert (res.status, res.reason_code, res.status_decided_by) == ("needs_check", "scope_unclear", "ai")
    assert "AI가 미충족으로 판단해" in (res.interpretation_note or "")


def test_model_cannot_choose_decider_or_checker():
    # 응답 스키마에는 판정 주체 칸이 없다. 보내도 받지 않고, 결과에는 코드가 ai를 적는다
    j = judgement(status_decided_by="user", evidence_checked_by="rule")
    judgements, _ = run([skill()], [j])
    assert (judgements["R1"]["status_decided_by"], judgements["R1"]["evidence_checked_by"]) == ("ai", "ai")


def test_invented_quote_makes_build_result_refuse_to_build():
    req = skill()
    judgements, _ = run([req], [judgement(status="met", reason_code=None, question_for_user=None,
                                          profile_evidence=[{"experience_id": "exp-1", "quote": "프로필에 없는 지어낸 문장"}])])
    with pytest.raises(ValueError, match="프로필 근거 검증 실패"):
        build_result(gate(SAMPLES[0]), profile(), [req], judgements)


def test_quote_must_come_from_one_piece_of_the_profile():
    req = skill()
    joined = "B2B AI 사업부 기획팀 인턴 서비스 기획 보조"  # title과 role을 이어 붙인 글
    judgements, _ = run([req], [judgement(status="met", reason_code=None, profile_evidence=[{"experience_id": "exp-1", "quote": joined}])])
    with pytest.raises(ValueError, match="프로필 근거 검증 실패"):
        build_result(gate(SAMPLES[0]), profile(), [req], judgements)


def test_met_without_any_quote_is_lowered_to_needs_check():
    req = skill()
    judgements, _ = run([req], [judgement(status="met", reason_code=None, question_for_user=None)])
    res = build_result(gate(SAMPLES[0]), profile(), [req], judgements).requirement_results[0]
    assert (res.status, res.reason_code) == ("needs_check", "scope_unclear") and res.question_for_user
    assert "근거 인용이 없어" in (res.interpretation_note or "")


def test_met_reason_code_is_dropped():
    req = skill()
    q = [{"experience_id": "exp-1", "quote": "Figma"}]
    judgements, _ = run([req], [judgement(status="met", reason_code="scope_unclear", profile_evidence=q)])
    res = build_result(gate(SAMPLES[0]), profile(), [req], judgements).requirement_results[0]
    assert (res.status, res.reason_code) == ("met", None)
