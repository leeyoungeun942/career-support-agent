"""job_gate_result.v2 형식 검증 (docs/data-contract.md 1장)."""
import pytest
from pydantic import ValidationError

from core.schemas import DISPLAY_LABELS, RANKING_GROUP, GateResultCode, JobGateResult

VERSIONS = {"config_version": "2026-11-02.1", "policy_revised": "2026-11-02", "map_modified": "2026-10-27"}
ITEM_TEXT = "자사 SaaS 서비스 로드맵 수립 및 유관 조직 요구사항 조율"


def make(result, **kw):
    base = {"job_id": "wanted-393719", "desired_family": "JF_SERVICE_PLANNING", "result": result,
            "summary": "테스트"}
    if result in ("match", "adjacent", "mismatch"):
        base["core_family"] = "JF_SERVICE_PLANNING" if result == "match" else "JF_DEV"
    if result == "review_needed":
        base["reason_code"] = "mixed_job"
    base.update(kw)
    return JobGateResult(**base)


@pytest.mark.parametrize("result,group,flag", [
    ("match", "primary", None),
    ("adjacent", "primary", "job_scope_differs"),
    ("review_needed", "primary", "job_needs_check"),
    ("mismatch", "job_mismatch", None),
    ("out_of_scope", "out_of_scope", None),
])
def test_derived_fields(result, group, flag):
    r = make(result)
    assert r.ranking_group == group
    assert r.display_flag == flag
    dumped = r.model_dump()
    assert dumped["ranking_group"] == group
    assert "proceed_to_eligibility" not in dumped
    assert dumped["schema_version"] == "job_gate_result.v2"


def test_derived_fields_cannot_be_overridden_by_input():
    r = JobGateResult.model_validate({**make("mismatch").model_dump(), "ranking_group": "primary"})
    assert r.ranking_group == "job_mismatch"


def test_v1_input_rejected():
    with pytest.raises(ValidationError):
        JobGateResult.model_validate({**make("match").model_dump(), "schema_version": "job_gate_result.v1"})


@pytest.mark.parametrize("kw", [
    {"result": "review_needed", "reason_code": None},
    {"result": "match", "reason_code": "mixed_job"},
    {"result": "match", "core_family": "JF_DEV"},
    {"result": "mismatch", "core_family": None},
    {"result": "out_of_scope", "core_family": "JF_DEV"},
])
def test_inconsistent_values_rejected(kw):
    result = kw.pop("result")
    with pytest.raises(ValidationError):
        make(result, **kw)


def test_review_needed_keeps_estimated_core_family():
    assert make("review_needed", core_family="JF_DEV").core_family == "JF_DEV"


def detail(quote, item_index=0):
    return {
        "ended_at_step": 6,
        "core_ratio": {"count": 2, "total": 2},
        "oos_items": {"count": 0, "total": 3},
        "evidence": {"supporting": [{"item_index": item_index, "quote": quote, "decided_by": "rule"}]},
        "items": [{"index": 0, "text": ITEM_TEXT, "label": "JF_SERVICE_PLANNING", "why": "과반"}],
        "versions": VERSIONS,
    }


def test_quote_from_posting_accepted():
    r = make("match", detail=detail("로드맵 수립"))
    assert r.detail.evidence.supporting[0].source == "job_posting"


@pytest.mark.parametrize("quote,item_index", [("AI가 지어낸 근거 문장", 0), ("로드맵 수립", 5)])
def test_quote_not_in_posting_rejected(quote, item_index):
    with pytest.raises(ValidationError):
        make("match", detail=detail(quote, item_index))


@pytest.mark.parametrize("ratio", [{"count": 3, "total": 2}, {"count": -1, "total": 2}])
def test_bad_ratio_rejected(ratio):
    d = detail("로드맵 수립") | {"core_ratio": ratio}
    with pytest.raises(ValidationError):
        make("match", detail=d)


def test_bad_item_label_rejected():
    d = detail("로드맵 수립")
    d["items"][0]["label"] = "공통"  # 저장 형식은 영어 코드만 허용한다
    with pytest.raises(ValidationError):
        make("match", detail=d)


def test_ranking_groups_have_display_labels():
    assert set(RANKING_GROUP.values()) <= set(DISPLAY_LABELS)


def test_result_codes_have_display_labels():
    codes: tuple[GateResultCode, ...] = (
        "match", "adjacent", "mismatch", "review_needed", "out_of_scope"
    )
    assert {code: DISPLAY_LABELS[code] for code in codes} == {
        "match": "일치",
        "adjacent": "인접",
        "mismatch": "불일치",
        "review_needed": "검토 필요",
        "out_of_scope": "범위 외",
    }
