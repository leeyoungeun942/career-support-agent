"""직무 게이트 판정 기록 -> job_gate_result.v2 변환 검증 (이슈 #5, docs/data-contract.md 1장, job-gate.md 5-4).

합성 공고(원문 없음)로 판정 5종과 근거 규칙을 확인하고, 샘플 6건(원문 필요)은 원문이 없으면 건너뛴다.
합성 공고의 업무 문구는 "업무 N를 처리합니다"이고 AI 자리가 라벨을 정한다. 그래서 하위 업무의 출처(decided_by)는 ai다.
"""
import json
from datetime import date

from core import job_gate as gate
from core import job_gate_config as cfg
from gate_data import RAW_CASES, load_expected, needs_raw
from core.job_gate_result import build_gate_result, judge_posting, make_summary
from core.schemas import DISPLAY_LABELS, JobGateResult

SP, PM, DEV = "JF_SERVICE_PLANNING", "JF_PROJECT_MGMT", "JF_DEV"
COMMON, MIXED, OOS = "common", "mixed", "out_of_scope"

def posting(n, title="담당자"):
    return {"job_title": title, "position": "", "items": [{"text": f"업무 {i}를 처리합니다"} for i in range(n)]}


def stub(labels):
    return lambda request: {"label": labels[request["item_index"]], "reason": f"스텁 {request['item_index']}"}


def run(labels, title="담당자", desired=SP, **options):
    return judge_posting("job-1", posting(len(labels), title), desired, stub(labels), **options)


def quotes(entries):
    return [(e.item_index, e.quote, e.decided_by) for e in entries]


# ---------------------------------------------------------------------
# 판정 5종: summary, 정렬 그룹, 표시 플래그, 근거
# ---------------------------------------------------------------------
def test_match_result_summary_group_and_supporting_evidence():
    r = run([SP, SP, COMMON])
    assert (r.result, r.core_family, r.reason_code, r.warnings) == ("match", SP, None, [])
    assert (r.ranking_group, r.display_flag) == ("primary", None)
    assert r.summary == "공통을 뺀 업무 2개 중 2개가 서비스기획"
    ev = r.detail.evidence
    assert quotes(ev.supporting) == [(0, "업무 0를 처리합니다", "ai"), (1, "업무 1를 처리합니다", "ai")]  # 공통 항목은 인용하지 않는다
    assert ev.conflicting == [] and ev.missing == []
    assert all(e.source == "job_posting" for e in ev.supporting)
    assert (r.detail.ended_at_step, r.detail.core_ratio.count, r.detail.core_ratio.total) == (6, 2, 2)


def test_adjacent_result_has_conflicting_evidence_from_the_desired_family_item():
    r = run([PM, PM, SP])  # 핵심은 프로젝트관리(2/3), 희망 직무(서비스기획) 항목도 있다
    assert (r.result, r.core_family, r.ranking_group, r.display_flag) == ("adjacent", PM, "primary", "job_scope_differs")
    assert r.summary == "핵심 직무는 프로젝트관리(2/3)이며 서비스기획 업무도 있음"
    assert quotes(r.detail.evidence.supporting) == [(0, "업무 0를 처리합니다", "ai"), (1, "업무 1를 처리합니다", "ai")]
    assert quotes(r.detail.evidence.conflicting) == [(2, "업무 2를 처리합니다", "ai")]


def test_mismatch_result_summary_and_group():
    r = run([DEV, DEV, SP])
    assert (r.result, r.core_family, r.ranking_group, r.display_flag) == ("mismatch", DEV, "job_mismatch", None)
    assert r.summary == "공통을 뺀 업무 3개 중 2개가 개발"
    assert [e.item_index for e in r.detail.evidence.supporting] == [0, 1]
    assert [e.item_index for e in r.detail.evidence.conflicting] == [2]


def test_common_and_mixed_labeled_items_are_never_quoted():
    r = run([SP, SP, SP, MIXED, COMMON])
    assert r.result == "match" and r.summary == "공통을 뺀 업무 4개 중 3개가 서비스기획"
    assert [e.item_index for e in r.detail.evidence.supporting] == [0, 1, 2]
    assert r.detail.evidence.conflicting == []  # 혼합(3번)과 공통(4번) 항목은 상충 근거가 아니다


def test_review_needed_insufficient_info_has_only_missing_text():
    r = run([DEV])
    assert (r.result, r.reason_code, r.core_family, r.ranking_group, r.display_flag) == ("review_needed", "insufficient_info", None, "primary", "job_needs_check")
    assert r.summary == "직무 확인 필요: 정보 부족"
    ev = r.detail.evidence
    assert ev.missing == ["업무 항목 1개 < 최소 2개"] and ev.supporting == [] and ev.conflicting == []
    assert (r.detail.ended_at_step, r.detail.items, r.detail.ai_log) == (2, [], [])  # 라벨링 전에 끝났다


def test_review_needed_no_core_tasks():
    r = run([COMMON, COMMON])
    assert (r.result, r.reason_code, r.core_family) == ("review_needed", "no_core_tasks", None)
    assert r.summary == "직무 확인 필요: 핵심 업무 없음"
    assert r.detail.evidence.missing == ["공통을 뺀 업무 항목 0개"]
    assert [it.label for it in r.detail.items] == [COMMON, COMMON]


def test_review_needed_mixed_job_keeps_the_estimated_core_family():
    r = run([SP, DEV])
    assert (r.result, r.reason_code, r.core_family) == ("review_needed", "mixed_job", SP)  # 추정값은 검토 필요여도 보존
    assert r.summary == "직무 확인 필요: 혼합 직무"
    assert r.detail.evidence.missing == ["가장 많은 라벨 1/2이 과반이 아님"]
    assert r.detail.evidence.supporting == [] and r.detail.evidence.conflicting == []


def test_review_needed_mixed_job_when_the_top_label_is_mixed():
    r = run([MIXED, MIXED])
    assert (r.result, r.reason_code, r.core_family) == ("review_needed", "mixed_job", None)
    assert r.detail.evidence.missing == ["가장 많은 라벨이 혼합"]


def test_review_needed_title_task_conflict_missing_text():
    r = run([SP, SP, SP, DEV, DEV], title="백엔드 개발자 모집")  # 3/5 < 2/3, 직무명은 개발
    assert (r.result, r.reason_code, r.core_family) == ("review_needed", "title_task_conflict", SP)
    assert r.summary == "직무 확인 필요: 직무명-업무 충돌"
    assert r.detail.evidence.missing == ["직무명은 개발, 업무 핵심은 서비스기획(3/5), 명확성 기준 2/3 미만"]
    assert (r.detail.ended_at_step, r.detail.core_ratio.count, r.detail.core_ratio.total) == (5, 3, 5)


def test_title_differs_warning_is_a_code():
    r = run([SP, SP, SP, SP], title="백엔드 개발자 모집")
    assert (r.result, r.warnings) == ("match", ["title_differs"])


def test_out_of_scope_at_step_1_has_no_labels_and_no_evidence():
    r = judge_posting("job-2", {"job_title": "담당자", "position": "", "items": [
        {"text": "마케팅 캠페인을 운영합니다"}, {"text": "광고 소재를 만듭니다"}]}, SP, stub([]))
    assert (r.result, r.core_family, r.ranking_group, r.display_flag) == ("out_of_scope", None, "out_of_scope", None)
    assert r.detail.ended_at_step == 1 and r.detail.items == [] and r.detail.ai_log == []
    ev = r.detail.evidence
    assert (ev.supporting, ev.conflicting, ev.missing) == ([], [], [])
    assert r.summary == f"지원 범위 밖 업무가 중심 (범위외 근거 {r.detail.oos_items.count}/{r.detail.oos_items.total})"
    assert r.detail.oos_items.count > 0


def test_out_of_scope_at_step_4_quotes_the_out_of_scope_labeled_items():
    r = judge_posting("job-3", {"job_title": "담당자", "position": "", "items": [
        {"text": "알 수 없는 업무를 처리합니다"}, {"text": "또 다른 알 수 없는 업무를 합니다"}]}, SP, lambda req: {"label": OOS, "reason": "스텁"})
    assert (r.result, r.core_family, r.ranking_group) == ("out_of_scope", None, "out_of_scope")
    assert r.detail.ended_at_step == 4
    assert quotes(r.detail.evidence.supporting) == [(0, "알 수 없는 업무를 처리합니다", "ai"), (1, "또 다른 알 수 없는 업무를 합니다", "ai")]
    assert r.summary == "지원 범위 밖 업무가 중심 (범위외 근거 0/2)"


def test_evidence_quotes_the_subtask_where_the_label_was_found_as_a_candidate():
    """한 항목에 하위 업무가 둘이면, 핵심 직무군이 후보로 잡힌 하위 업무를 인용한다(첫 번째가 아니라)."""
    p = {"job_title": "담당자", "position": "", "items": [
        {"text": "회의록 작성 / 서비스 기획서 작성"}, {"text": "서비스 기획서 검토"}]}
    r = judge_posting("job-4", p, SP, lambda req: {"label": SP, "reason": "스텁"})
    assert r.result == "match"
    first = r.detail.items[0]
    assert [s.text for s in first.subtasks] == ["회의록 작성", "서비스 기획서 작성"]
    assert SP in first.subtasks[1].candidates and SP not in first.subtasks[0].candidates
    assert quotes(r.detail.evidence.supporting)[0] == (0, "서비스 기획서 작성", "rule")


# ---------------------------------------------------------------------
# detail, versions, 변환 충실도
# ---------------------------------------------------------------------
def test_versions_come_from_config_and_ai_fields_are_empty_in_mock_mode():
    v = run([SP, SP]).detail.versions
    assert v.config_version == cfg.CONFIG_VERSION
    assert v.policy_revised == date.fromisoformat(cfg.POLICY_REVISED) and v.map_modified == date.fromisoformat(cfg.MAP_MODIFIED)
    assert (v.ai_model, v.prompt_version) == (None, None)
    v = run([SP, SP], ai_model="m-1", prompt_version="p-3").detail.versions
    assert (v.ai_model, v.prompt_version) == ("m-1", "p-3")


def test_detail_can_be_left_out_and_summary_is_the_same():
    full, lean = run([SP, SP, COMMON]), run([SP, SP, COMMON], with_detail=False)
    assert lean.detail is None
    assert lean.model_dump(exclude={"detail"}) == full.model_dump(exclude={"detail"})  # 요약 칸은 detail 유무와 상관없다


def test_there_is_no_proceed_to_eligibility_and_the_result_validates_again():
    r = run([SP, SP])
    dumped = r.model_dump(mode="json")
    assert "proceed_to_eligibility" not in dumped and "proceed_to_eligibility" not in dumped["detail"]
    assert dumped["schema_version"] == "job_gate_result.v2"
    assert JobGateResult.model_validate(dumped) == r  # 계산 칸(ranking_group, display_flag)까지 되돌려 읽힌다


def test_items_and_subtasks_are_converted_with_their_records():
    r = run([SP, SP])
    item = r.detail.items[0]
    assert (item.index, item.heading, item.text, item.label) == (0, "", "업무 0를 처리합니다", SP)
    sub = item.subtasks[0]
    assert (sub.text, sub.label, sub.source, sub.candidates) == ("업무 0를 처리합니다", SP, "ai", [])
    assert sub.ai_reason == "스텁 0" and sub.warnings == []


def test_ai_log_entries_are_converted_one_per_ai_call():
    calls = []

    def recorder(request):
        calls.append(request)
        return {"label": SP, "reason": f"스텁 {request['item_index']}"}

    r = judge_posting("job-5", posting(3), SP, recorder)
    assert len(r.detail.ai_log) == len(calls) == 3  # 하위 업무마다 1회
    assert [(e.item, e.sub, e.label, e.reason) for e in r.detail.ai_log] == [(0, 0, SP, "스텁 0"), (1, 0, SP, "스텁 1"), (2, 0, SP, "스텁 2")]
    assert all(e.why_ai for e in r.detail.ai_log)  # AI를 부른 이유가 남는다


def test_ai_request_level_values_are_english_codes():
    levels = []

    def recorder(request):
        levels.append(request["level"])
        return {"label": SP, "reason": ""}

    judge_posting("job-6", posting(2), SP, recorder)
    assert set(levels) == {"subtask"}
    # 항목 전체를 AI로 판별하는 경우(공통이 아닌 하위 업무가 정확히 절반)는 item
    levels.clear()
    judge_posting("job-7", {"job_title": "담당자", "position": "", "items": [{"text": "회의록 작성 / 업무 1를 처리합니다"}, {"text": "업무 2를 처리합니다"}]},
                  SP, lambda req: (levels.append(req["level"]), {"label": COMMON if req["level"] == "subtask" and "회의록" in req["text"] else SP, "reason": ""})[1])
    assert "item" in levels


def test_summary_templates_match_data_contract_1_6():
    """data-contract 1-6 표의 다섯 줄. 판정 값만 템플릿에 들어간다."""
    names = {fid: f["name"] for fid, f in cfg.JOB_FAMILIES.items()}
    base = {"desired_family": SP, "core_family": DEV, "reason_code": None, "core_ratio": "2/3", "oos_items": "1/4"}
    assert make_summary({**base, "result": "match", "core_family": SP}) == f"공통을 뺀 업무 3개 중 2개가 {names[SP]}"
    assert make_summary({**base, "result": "adjacent", "core_family": PM}) == f"핵심 직무는 {names[PM]}(2/3)이며 {names[SP]} 업무도 있음"
    assert make_summary({**base, "result": "mismatch"}) == f"공통을 뺀 업무 3개 중 2개가 {names[DEV]}"
    for reason in ("insufficient_info", "no_core_tasks", "mixed_job", "title_task_conflict"):
        assert make_summary({**base, "result": "review_needed", "reason_code": reason}) == f"직무 확인 필요: {DISPLAY_LABELS[reason]}"
    assert make_summary({**base, "result": "out_of_scope"}) == "지원 범위 밖 업무가 중심 (범위외 근거 1/4)"


def test_build_gate_result_works_on_a_raw_record_directly():
    raw = gate.judge(posting(2), SP, stub([SP, SP]))
    assert build_gate_result("job-8", raw).summary == "공통을 뺀 업무 2개 중 2개가 서비스기획"


# ---------------------------------------------------------------------
# 샘플 6건 (원문 필요)
# ---------------------------------------------------------------------
SAMPLE_SUMMARIES = {
    1: "공통을 뺀 업무 2개 중 2개가 서비스기획",
    2: "공통을 뺀 업무 5개 중 5개가 개발",
    3: "공통을 뺀 업무 4개 중 4개가 서비스기획",
    4: "공통을 뺀 업무 2개 중 2개가 개발",
    5: "공통을 뺀 업무 2개 중 2개가 영업",
    6: "공통을 뺀 업무 4개 중 4개가 경영기획",
}


@needs_raw
def test_sample_cases_as_v2_results():
    """7-1의 6건이 v2 결과로 만들어지고, 판정·그룹·요약·근거가 기대와 같다."""
    desired, expected = load_expected()
    raw = {c["no"]: c for c in json.loads(RAW_CASES.read_text(encoding="utf-8"))["cases"]}
    for exp in expected:
        case = raw[exp["no"]]
        r = judge_posting(f"sample-{exp['no']}", {"job_title": case["job_title"], "position": case["position"], "items": case["items"]},
                          desired, stub(exp["item_labels"]))
        assert (r.result, r.core_family) == (exp["expected"]["gate"], exp["expected"]["core_family"]), exp["no"]
        assert r.ranking_group == ("primary" if exp["expected"]["gate"] == "match" else "job_mismatch"), exp["no"]
        assert r.summary == SAMPLE_SUMMARIES[exp["no"]], exp["no"]
        assert r.warnings == [] and r.reason_code is None  # 7-1: 6건 모두 경고 없음
        # 근거: 라벨이 핵심 직무군인 항목마다 인용 1개, 공통 항목은 인용하지 않는다
        core_items = [i for i, label in enumerate(exp["item_labels"]) if label == exp["expected"]["core_family"]]
        assert [e.item_index for e in r.detail.evidence.supporting] == core_items, exp["no"]
        assert r.detail.evidence.conflicting == [] and r.detail.evidence.missing == []
        for e in r.detail.evidence.supporting:  # 인용은 항목 원문의 일부다 (스키마도 검사한다)
            item = r.detail.items[e.item_index]
            assert e.quote in f"{item.heading} {item.text}"
