"""core/career_normalizer.py 검증 (정책 eligibility.md 5-4, 13-1; PR #11 리뷰 P1-1, P2-2). 네트워크를 쓰지 않는다."""
import copy
from pathlib import Path

import pytest

from agents.job_analyzer import extract_requirements
from core.career_normalizer import career_basis, normalize_career
from core.llm_client import FakeClient
from core.llm_mock import evidence_answer, extraction_answer, synthetic_posting
from core.pipeline import assess_eligibility
from tests.test_eval_eligibility import EVIDENCE, GATES, PROFILE, SAMPLES

POLICY = (Path(__file__).resolve().parents[1] / "docs" / "policies" / "eligibility.md").read_text(encoding="utf-8")


def career(text, **kw):
    base = {"level": "required", "type": "numeric", "text": text, "numeric_kind": "career_years", "min_value": None, "max_value": None}
    return {**base, **kw}


# ---------------------------------------------------------------- 정해진 표현 -> 값 (정책 5-4 표)

@pytest.mark.parametrize("text, want_min, want_max", [
    ("신입 / 경력 1년 이상 ~ 3년 이하", 0, 3),           # 3.1이 min_value=1로 읽어 거짓 미충족을 만든 표현
    ("신입 / 경력 ~3년 (관련 경력 인정, 전공 무관)", 0, 3),
    ("신입 / 경력 3년 이상", 0, None),
    ("신입 또는 경력 2년 이상", 0, None),
    ("신입/경력 1년~3년", 0, 3),
    ("경력 무관 (신입 지원 가능)", 0, None),
    ("신입", 0, None),
    ("경력 1년 이상 ~ 3년 이하", 1, 3),
    ("경력 2~5년", 2, 5),
    ("경력 1~2년", 1, 2),
    ("서비스 기획 경력 3년 이상 (앱/웹 서비스 출시 경험 필수)", 3, None),
    ("경력 3년 초과", 3, None),
    ("경력 5년 내외", 4, None),
    ("Python 개발 경력 3년 내외", 2, None),
    ("경력 1년 내외", 0, None),
    ("경력 무관", 0, None),
    ("경력무관", 0, None),
    ("경력  3 년  이상", 3, None),                        # 공백이 달라도 같은 표현
])
def test_known_expressions_set_the_values_even_when_the_model_got_them_wrong(text, want_min, want_max):
    wrong = career(text, min_value=7, max_value=9)
    got, changes = normalize_career(wrong)
    assert (got["min_value"], got.get("max_value") if want_max is not None else got["max_value"]) == (want_min, want_max if want_max is not None else 9)
    assert changes and "코드가 원문 표현으로 보정" in got["interpretation_note"]


def test_model_value_that_already_matches_is_left_alone_without_a_note():
    raw = career("서비스 기획 경력 3년 이상 (앱/웹 서비스 출시 경험 필수)", min_value=3)
    got, changes = normalize_career(raw)
    assert got is raw and changes == [] and "interpretation_note" not in got


def test_range_only_changes_the_bounds_the_expression_defines():
    # "N년 내외"는 상한을 말하지 않는다: max_value는 모델 값 그대로
    got, _ = normalize_career(career("경력 3년 내외", min_value=3, max_value=4))
    assert (got["min_value"], got["max_value"]) == (2, 4)


# ---------------------------------------------------------------- 연차 무관 (P2-2)

@pytest.mark.parametrize("wrong_type", ["qualitative", "skill"])
def test_unlimited_years_is_forced_to_numeric_even_if_the_model_chose_another_type(wrong_type):
    raw = {"level": "required", "type": wrong_type, "text": "경력 (연차 무관, 실무 프로젝트 경험)"}
    got, changes = normalize_career(raw)
    assert (got["type"], got["numeric_kind"], got["min_value"]) == ("numeric", "career_years", None)
    assert changes and "유형" in got["interpretation_note"]


def test_unlimited_years_already_numeric_with_no_threshold_is_unchanged():
    raw = career("경력 (연차 무관, 실무 프로젝트 경험)")
    assert normalize_career(raw) == (raw, [])


def test_unlimited_years_with_a_wrong_threshold_is_cleared():
    got, _ = normalize_career(career("경력 (연차 무관)", min_value=0))
    assert got["min_value"] is None


def test_forced_conversion_clears_fields_that_do_not_belong_to_career():
    raw = {"level": "required", "type": "qualitative", "text": "경력 (연차 무관)", "min_degree": "bachelor",
           "career_family": "JF_DEV", "career_family_basis": None}
    got, _ = normalize_career(raw)
    assert got["min_degree"] is None and got["career_family"] is None  # 한쪽만 있는 직무군은 검증을 통과하지 못한다


def test_year_free_sentences_with_career_word_but_no_unlimited_phrase_are_not_forced():
    raw = {"level": "required", "type": "qualitative", "text": "관련 경력이 풍부하신 분"}
    assert normalize_career(raw) == (raw, [])


# ---------------------------------------------------------------- 애매하면 고치지 않는다

@pytest.mark.parametrize("text", [
    "경영기획 분야 1~2년의 경험이 있으신 분",     # '경력'이 없다
    "신입 불가, 경력 3년 이상",                   # 신입 경로가 닫혔다
    "경력 3년 이상 또는 경력 5년 이상",            # 서로 다른 표현이 충돌한다
    "경력 (연차 무관), 신입 환영",                  # 신입과 연차 무관이 함께 있다
    "Python 3년 사용 경험",                       # 정해진 표현이 아니다
])
def test_ambiguous_or_unknown_text_keeps_the_model_value(text):
    raw = career(text, min_value=7, max_value=9)
    assert normalize_career(raw) == (raw, [])


def test_only_career_years_numeric_requirements_are_touched():
    edu = {"level": "required", "type": "numeric", "numeric_kind": "education", "text": "경력 무관, 대졸 이상", "min_degree": "associate"}
    assert normalize_career(edu) == (edu, [])
    qual = {"level": "required", "type": "qualitative", "text": "경력 3년 이상의 PM 경험"}
    assert normalize_career(qual) == (qual, [])
    anyof = career("경력 무관", min_value=5, combine="any_of")
    assert normalize_career(anyof) == (anyof, [])


def test_career_family_fields_are_never_changed():
    raw = career("관련 경력 신입 / 경력 1년 이상", min_value=1, career_family="JF_SERVICE_PLANNING", career_family_basis="related")
    got, _ = normalize_career(raw)
    assert got["min_value"] == 0 and got["career_family"] == "JF_SERVICE_PLANNING" and got["career_family_basis"] == "related"


def test_input_is_not_mutated_and_existing_note_is_kept():
    raw = career("신입 / 경력 1년 이상 ~ 3년 이하", min_value=1, interpretation_note="경력 지원 시 1년 이상")
    before = copy.deepcopy(raw)
    got, _ = normalize_career(raw)
    assert raw == before and got is not raw
    assert got["interpretation_note"].startswith("경력 지원 시 1년 이상 / 코드가 원문 표현으로 보정")


def test_career_basis_returns_none_without_any_known_expression():
    assert career_basis("Python 능숙") is None and career_basis("") is None


# ---------------------------------------------------------------- 추출 단계와 종합 판정에서의 효과

def test_extraction_applies_the_normalizer_and_counts_it():
    posting = "자격요건:\n- 신입 / 경력 1년 이상 ~ 3년 이하"
    fake = FakeClient([{"requirements": [career("신입 / 경력 1년 이상 ~ 3년 이하", min_value=1, max_value=3)], "extraction_issues": []}])
    stats = {}
    reqs, issues = extract_requirements(posting, fake, "job1", stats=stats)
    assert (reqs[0].min_value, reqs[0].max_value) == (0, 3) and issues == []
    assert "코드가 원문 표현으로 보정" in reqs[0].interpretation_note
    assert stats["corrected_by_code"] == 1 and stats["retained"] == 1


def test_forced_numeric_survives_job_requirement_validation():
    posting = "자격요건:\n- 경력 (연차 무관, 실무 프로젝트 경험)"
    raw = {"level": "required", "type": "qualitative", "text": "경력 (연차 무관, 실무 프로젝트 경험)"}
    reqs, issues = extract_requirements(posting, FakeClient([{"requirements": [raw], "extraction_issues": []}]), "job1")
    assert [(r.type, r.numeric_kind, r.min_value) for r in reqs] == [("numeric", "career_years", None)] and issues == []


def test_sample_3_misread_career_range_no_longer_turns_into_a_false_not_met():
    """3.1이 만든 거짓 미충족의 재현: 첫 경력 줄을 min_value=1로 읽어도 코드가 0으로 바로잡아 종합 판정이 정답(조건부 적합)이다."""
    sample = SAMPLES[2]
    answer = extraction_answer(sample)
    answer["requirements"][0].update(text="신입 / 경력 1년 이상 ~ 3년 이하", min_value=1, max_value=3)
    posting = synthetic_posting(sample).replace(sample["requirements"][0]["text"], "신입 / 경력 1년 이상 ~ 3년 이하")
    gate = GATES[sample["job_id"]]
    judge = FakeClient([evidence_answer(sample, EVIDENCE)])
    _, result = assess_eligibility(posting, gate, PROFILE, FakeClient([answer]), judge)
    assert result.verdict == "conditional" == sample["expected"]["verdict"]
    assert result.requirement_results[0].status == "met"


def test_sample_4_no_threshold_career_stays_needs_check_when_the_model_picks_the_wrong_type():
    """3.1이 "경력 (연차 무관)"을 qualitative로 뽑아 근거만 있으면 met이 된 사례의 재현."""
    sample = SAMPLES[3]
    answer = extraction_answer(sample)
    first = answer["requirements"][0]
    first.update(type="qualitative", numeric_kind=None, min_value=None)
    judge = FakeClient([evidence_answer(sample, EVIDENCE)])
    _, result = assess_eligibility(synthetic_posting(sample), GATES[sample["job_id"]], PROFILE, FakeClient([answer]), judge)
    first_result = result.requirement_results[0]
    assert result.requirements[0].type == "numeric"
    assert (first_result.status, first_result.reason_code, first_result.status_decided_by) == ("needs_check", "no_threshold", "rule")


def test_policy_documents_the_normalizer_scope():
    assert "### 5-4." in POLICY and "core/career_normalizer.py" in POLICY
    assert "| **신입 / 경력 N년 이상 ~ M년 이하** | 0" in POLICY
