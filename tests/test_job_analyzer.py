"""agents/job_analyzer.py 요건 추출 검증 (정책 eligibility.md 13-1, 1-4, 17장; 이슈 #6). 네트워크를 쓰지 않는다."""
import json
from pathlib import Path

import pytest

from agents.job_analyzer import ExtractionResponse, extract_requirements
from core.llm_client import FakeClient, LLMQuotaExceeded, LLMStageError
from core.llm_mock import extraction_answer, make_extraction_mock, synthetic_posting
from core.prompts import fill, load_prompt

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "eligibility_sample_expected.json").read_text(encoding="utf-8"))
SAMPLES = FIXTURE["samples"]

POSTING = "자격요건:\n- 대졸 이상\n- 서비스 기획 경력 3년 이상\n- 데이터 기반 의사결정 경험\n우대사항:\n- SQL 활용 경험"


def req_raw(**kw):
    base = {"level": "required", "type": "qualitative", "text": "데이터 기반 의사결정 경험"}
    return {**base, **kw}


def answer(*reqs, issues=()):
    return {"requirements": list(reqs), "extraction_issues": list(issues)}


def extract(reqs, issues=(), posting=POSTING, **kw):
    return extract_requirements(posting, FakeClient([answer(*reqs, issues=issues)]), "job1", **kw)


# ---------------------------------------------------------------- mock: 샘플 6건이 정책 17장과 같다

@pytest.mark.parametrize("sample", SAMPLES, ids=[f"sample-{s['number']}" for s in SAMPLES])
def test_mock_extraction_matches_policy_table(sample):
    client = make_extraction_mock(SAMPLES)
    reqs, issues = extract_requirements(synthetic_posting(sample), client, sample["job_id"])
    assert issues == []
    expected = sample["requirements"]
    assert len(reqs) == len(expected)
    for got, want in zip(reqs, expected):
        assert (got.level, got.type, got.numeric_kind, got.text) == (want["level"], want["type"], want.get("numeric_kind"), want["text"])
        for k in ("min_value", "max_value", "career_family", "career_family_basis", "min_degree", "combine", "origin"):
            if k in want:
                assert getattr(got, k) == want[k], (got.id, k)
        assert [i.model_dump() for i in got.items] == want.get("items", [])
    assert [r.id for r in reqs] == [f"R{i}" for i in range(1, len(reqs) + 1)]


def test_mock_extraction_has_no_judgement_fields():
    assert all("status" not in r and "id" not in r for s in SAMPLES for r in extraction_answer(s)["requirements"])


def test_mock_answer_fits_the_response_schema():
    for s in SAMPLES:
        ExtractionResponse.model_validate(extraction_answer(s))


# ---------------------------------------------------------------- 코드 확인

def test_text_not_in_posting_is_dropped_with_partial_extraction():
    reqs, issues = extract([req_raw(), req_raw(text="공고에 없는 지어낸 요건")])
    assert [r.text for r in reqs] == ["데이터 기반 의사결정 경험"] and issues == ["partial_extraction"]


def test_invalid_requirement_is_dropped_but_others_are_kept_and_ids_are_contiguous():
    bad = req_raw(text="대졸 이상", type="numeric")  # numeric인데 numeric_kind가 없다
    good = req_raw(text="서비스 기획 경력 3년 이상", type="numeric", numeric_kind="career_years", min_value=3,
                   career_family="JF_SERVICE_PLANNING", career_family_basis="stated")
    reqs, issues = extract([bad, good, req_raw()])
    assert [(r.id, r.text) for r in reqs] == [("R1", "서비스 기획 경력 3년 이상"), ("R2", "데이터 기반 의사결정 경험")]
    assert issues == ["partial_extraction"]


def test_any_of_item_not_in_requirement_text_is_dropped():
    r = req_raw(type="skill", combine="any_of", items=[{"index": 0, "text": "데이터"}, {"index": 1, "text": "없는 항목"}])
    reqs, issues = extract([r])
    assert reqs == [] and issues == ["partial_extraction"]


def test_valid_any_of_is_kept():
    r = req_raw(type="skill", combine="any_of", items=[{"index": 0, "text": "데이터 기반"}, {"index": 1, "text": "의사결정 경험"}])
    reqs, issues = extract([r])
    assert reqs[0].combine == "any_of" and len(reqs[0].items) == 2 and issues == []


def test_llm_extraction_status_and_unknown_issues_have_no_effect():
    fake = FakeClient([{**answer(req_raw()), "extraction_status": "complete"}])  # 받을 칸이 없다
    reqs, issues = extract_requirements(POSTING, fake, "job1")
    assert issues == [] and len(reqs) == 1
    reqs, issues = extract([req_raw()], issues=["made_up", "complete", "no_requirement_section", "no_requirement_section"])
    assert issues == ["no_requirement_section"]  # 허용 값만, 중복 없이


def test_preferred_only_posting_with_no_requirement_section():
    reqs, issues = extract([req_raw(level="preferred", text="SQL 활용 경험")], issues=["no_requirement_section"],
                           posting="우대사항:\n- SQL 활용 경험")
    assert [r.level for r in reqs] == ["preferred"] and issues == ["no_requirement_section"]


def test_zero_requirements_with_no_issue_is_complete_not_a_failure():
    reqs, issues = extract([], posting="자격요건: 학력무관, 경력무관")
    assert reqs == [] and issues == []


# ---------------------------------------------------------------- 실패 처리

def test_empty_posting_makes_no_call():
    fake = FakeClient([])
    assert extract_requirements("  \n", fake, "job1") == ([], ["text_unreadable"])
    assert fake.requests == []


def test_schema_violation_becomes_extraction_failed_without_reasking():
    fake = FakeClient([{"requirements": "목록이 아님"}, answer(req_raw())])
    assert extract_requirements(POSTING, fake, "job1") == ([], ["extraction_failed"])
    assert len(fake.requests) == 1


def test_stage_error_becomes_extraction_failed():
    assert extract_requirements(POSTING, FakeClient([LLMStageError("통신 실패")]), "job1") == ([], ["extraction_failed"])


def test_quota_exhaustion_is_not_swallowed():
    with pytest.raises(LLMQuotaExceeded):
        extract_requirements(POSTING, FakeClient([LLMQuotaExceeded("소진")]), "job1")


# ---------------------------------------------------------------- 요청 내용과 프롬프트

def test_request_carries_posting_core_family_version_and_no_profile_flag():
    fake = FakeClient([answer(req_raw())])
    extract_requirements(POSTING, fake, "job1", core_family="JF_SERVICE_PLANNING")
    r = fake.requests[0]
    version, _ = load_prompt("extract_requirements")
    assert POSTING in r.prompt and "핵심 직무군: JF_SERVICE_PLANNING\n" in r.prompt and "{{" not in r.prompt
    assert r.prompt_version == version and r.stage == "extract_requirements" and not r.contains_profile and r.job_id == "job1"


def test_core_family_defaults_to_none_in_prompt():
    fake = FakeClient([answer(req_raw())])
    extract_requirements(POSTING, fake, "job1")
    assert "핵심 직무군: 없음\n" in fake.requests[0].prompt


def test_prompt_version_line_is_required(tmp_path):
    (tmp_path / "x.md").write_text("본문", encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        load_prompt("x", tmp_path)


def test_fill_does_not_expand_placeholders_inside_values_and_rejects_missing():
    assert fill("A {{a}} B {{b}}", a="{{b}}", b="x") == "A {{b}} B x"
    with pytest.raises(KeyError):
        fill("{{없음}}")
