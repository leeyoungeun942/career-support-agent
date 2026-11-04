"""프롬프트 파일 <-> 정책 문서 <-> 응답 스키마 대조 (플랜 이슈 #6 Step 7, D7).

프롬프트에는 정책 전체가 아니라 필요한 규칙만 옮겨 적는다. 정책이나 스키마가 바뀌면 프롬프트도 바꿔야 하므로, 필수 규칙이 빠지거나
어긋나면 여기서 잡는다. 문구가 정확히 같은지까지는 못 잡고, 필수 값·칸 이름·허용 값의 누락만 잡는다 (플랜 리스크 '프롬프트-정책 어긋남').
정책 문서(Markdown)를 읽는 테스트라 문서 형식이 바뀌면 깨진다. 제외하려면 -m "not policy_contract".
"""
import re
from pathlib import Path

import pytest

from agents.job_analyzer import ALLOWED_ISSUES, ExtractedRequirement
from agents.profile_analyzer import EvidenceJudgement, ItemJudgement, QuoteOut
from core.job_gate_config import JOB_FAMILIES
from core.prompts import load_prompt
from core.rules_ai import EVIDENCE_LIST_FIELDS
from core.schemas import NEEDS_CHECK_REASONS, Degree

pytestmark = pytest.mark.policy_contract

POLICY = (Path(__file__).resolve().parents[1] / "docs" / "policies" / "eligibility.md").read_text(encoding="utf-8")
_, EXTRACT = load_prompt("extract_requirements")
_, EVIDENCE = load_prompt("find_evidence")


def ticked(text: str) -> set[str]:
    """백틱 안에 적힌 낱말들. `combine=any_of`처럼 값이 붙은 표기는 칸 이름(combine)도 낱말로 센다."""
    words = set(re.findall(r"`([^`\s]+)`", text))
    return words | {w.split("=")[0] for w in words if "=" in w}


def section(text: str, heading: str) -> str:
    """'## 제목'부터 다음 '## '까지."""
    m = re.search(rf"^## {re.escape(heading)}.*?(?=^## |\Z)", text, flags=re.S | re.M)
    assert m, f"{heading} 절이 없다"
    return m.group(0)


# ---------------------------------------------------------------- 요건 추출 프롬프트

def test_extract_prompt_names_every_response_field():
    missing = [f for f in ExtractedRequirement.model_fields if f not in ticked(EXTRACT)]
    assert missing == [], f"프롬프트가 설명하지 않는 응답 칸: {missing}"


def test_extract_prompt_family_list_matches_supported_families():
    supported = {k for k, v in JOB_FAMILIES.items() if v["status"] == "지원"}
    listed = set(re.findall(r"JF_[A-Z_]+", section(EXTRACT, "경력 (min_value)")))
    assert listed == supported


def test_extract_prompt_issue_list_matches_policy_minus_code_owned_value():
    issues = ticked(section(EXTRACT, "extraction_issues")) & set(ALLOWED_ISSUES)
    # extraction_failed는 호출 실패를 코드가 확인했을 때만 적는다(정책 13-1). 모델에게 고르게 하지 않는다
    assert issues == set(ALLOWED_ISSUES) - {"extraction_failed"}
    for name in ALLOWED_ISSUES:
        assert f"`{name}`" in POLICY, f"정책 1-4에 {name}이 없다"


def test_extract_prompt_education_order_matches_schema():
    order = " < ".join(Degree.__args__)
    assert order in EXTRACT
    assert "고졸 < 전문학사 < 학사 < 석사 < 박사" in POLICY  # 6장. 코드값 순서와 같은 순서다


@pytest.mark.parametrize("rule, in_prompt, in_policy", [
    ("신입 / 경력 N년 이상은 0 (5-1)", "신입 / 경력 N년 이상 | 0", "신입 / 경력 N년 이상"),
    ("경력 N년 내외는 N-1 (5장)", "경력 N년 내외 | N-1", "| 경력 N년 내외 | N−1 (0보다 작으면 0)"),
    ("경력 연차 무관은 null (5장)", "경력 (연차 무관) | `null`", "경력 (연차 무관)"),
    ("대졸 이상은 전문학사 이상 (6장)", "\"대졸 이상\"(연수 표시 없음) → associate", "전문학사 이상"),
    ("학력무관은 요건으로 만들지 않는다 (6장)", "\"학력무관\"은 요건으로 만들지 않는다", "학력무관"),
    ("전공 무관은 요건으로 만들지 않는다 (7장)", "\"전공 무관\"도 만들지 않는다", "전공 무관"),
    ("전공명이 없으면 qualitative (7장)", "전공명이 없으면 `qualitative`", "전공명이 없으면 `qualitative`"),
    ("한 문장의 값 부분과 경험 부분은 둘로 나눈다, origin=split (10장)", "`origin=split`", "`origin=split`"),
    ("나열된 보유역량은 any_of (9장)", "`combine=any_of`", "`combine=any_of`"),
    ("태도·성향은 preference (9장)", "`preference`", "`type=preference`"),
    ("관련 경력은 핵심 직무군 + related (4-1)", "`related`", "career_family_basis=related"),
    ("동종 업계 경력은 qualitative (4-1)", "\"동종 업계 경력\"도 `qualitative`", "동종 업계"),
    ("어학 점수는 skill (12장)", "어학 시험 점수도 `skill`", "어학 점수 요건"),
    ("고용형태·근무지는 요건이 아니다 (12장)", "고용형태와 근무지도 요건으로 만들지 않는다", "고용형태·근무지"),
    ("text는 원문 그대로 (13-1)", "**글자 그대로**", "공고 원문의 일부"),
])
def test_extract_prompt_carries_required_policy_rule(rule, in_prompt, in_policy):
    assert in_prompt in EXTRACT, f"프롬프트에 빠짐: {rule}"
    assert in_policy in POLICY, f"정책 문서에서 사라짐(프롬프트도 확인 필요): {rule}"


def test_extract_prompt_does_not_ask_for_judgement_or_ids():
    for banned in ("status", "status_decided_by", "compared", "extraction_status"):
        assert banned not in ticked(EXTRACT), banned
    assert "요건 id, 충족 여부" in EXTRACT  # 내지 않는다고 명시


# ---------------------------------------------------------------- 근거 찾기 프롬프트

def test_evidence_prompt_names_every_response_field():
    fields = set(EvidenceJudgement.model_fields) | set(ItemJudgement.model_fields) | set(QuoteOut.model_fields)
    fields -= {"requirement_id"}  # 요건 `id`로 설명한다
    missing = [f for f in fields if f not in ticked(EVIDENCE)]
    assert missing == [], f"프롬프트가 설명하지 않는 응답 칸: {missing}"


def test_evidence_prompt_offers_only_the_reasons_the_model_may_choose():
    offered = ticked(section(EVIDENCE, "`needs_check` 사유 고르기")) & (NEEDS_CHECK_REASONS | {"below_threshold", "user_denied"})
    # no_threshold, graduation_pending, category_unclear는 코드의 계산에서만 생긴다 (정책 3-1)
    assert offered == {"no_profile_info", "scope_unclear", "level_unclear"}


def test_evidence_prompt_order_of_reasons_follows_policy_3_1():
    body = section(EVIDENCE, "`needs_check` 사유 고르기")
    positions = [body.index(f"`{c}`") for c in ("no_profile_info", "scope_unclear", "level_unclear")]
    assert positions == sorted(positions)
    policy_order = [POLICY.index("| 2 | 프로필에 이 요건과 관련된 정보가 아예 없는가? | `no_profile_info`"),
                    POLICY.index("| 3 | 정보는 있으나 요건과 같은 것인지 판단할 수 없는가? | `scope_unclear`"),
                    POLICY.index("| 4 | 같은 것은 맞지만 요구하는 수준에 이르는지 판단할 수 없는가? | `level_unclear`")]
    assert policy_order == sorted(policy_order)


def test_evidence_prompt_forbids_not_met_and_requires_verbatim_single_piece_quotes():
    assert "미충족(`not_met`)은 내지 않는다" in EVIDENCE
    assert "글자 그대로" in EVIDENCE and "이어 붙이지 않는다" in EVIDENCE
    assert "한 조각" in EVIDENCE and "한 조각" in POLICY


def test_evidence_prompt_lists_the_field_names_the_code_accepts():
    for field in EVIDENCE_LIST_FIELDS:
        assert f"`{field}`" in EVIDENCE, field
    assert "education[0]" in EVIDENCE
    assert "skills_denied" not in EVIDENCE  # 근거로 쓸 수 없는 칸 (정책 13장)


def test_evidence_prompt_defers_decider_and_summary_to_code():
    assert "종합 판정, 점수, 요건 id 새로 만들기, 판정 주체 표시는 내지 않는다" in EVIDENCE


# ---------------------------------------------------------------- 정책 13-1 <-> 응답 스키마

def test_policy_13_1_lists_every_extraction_field():
    body = POLICY[POLICY.index("### 13-1."):POLICY.index("## 14.")]
    missing = [f for f in ExtractedRequirement.model_fields if f"`{f}`" not in body]
    assert missing == [], f"정책 13-1이 적지 않은 응답 칸: {missing}"


def test_policy_13_1_lists_the_evidence_fields_the_model_returns():
    body = POLICY[POLICY.index("### 13-1."):POLICY.index("## 14.")]
    for field in ("status", "reason_code", "profile_evidence", "question_for_user"):
        assert f"`{field}`" in body, field
