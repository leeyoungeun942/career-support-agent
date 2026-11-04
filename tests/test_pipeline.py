"""run_pipeline과 pipeline_run.v1 검증 (data-contract 3장, 정책 eligibility.md 14장; 이슈 #7). 네트워크를 쓰지 않는다."""
import json
import random

import pytest

from core.job_gate_mock import make_label_mock
from core.job_gate_result import judge_posting
from core.llm_client import FakeClient, LLMConfigError, LLMQuotaExceeded, LLMRequest, LLMStageError, UsageLog
from core.llm_mock import evidence_answer, synthetic_posting
from core.pipeline import ScenarioJob, make_run_id, run_pipeline
from core.pipeline_run import FAILURE_TEXT, PipelineJob, PipelineRun
from core.rules import ProfileEvidenceError
from pipeline_helpers import (BY_ID, EVIDENCE, GATES, IDS, NOW, POLICY_14_ORDER, PROFILE, REF, SAMPLES, Wrap, clients, run,
                              scenario)

# ---------------------------------------------------------------- 시연 시나리오: 고정 입력 순서

def test_fixed_input_order_gives_policy_14_order_and_ranks():
    r = run()
    assert r.ranking == POLICY_14_ORDER
    assert [j.job_id for j in r.jobs] == IDS  # jobs는 입력 순서
    assert {j.job_id: j.rank for j in r.jobs} == {i: n for n, i in enumerate(POLICY_14_ORDER, start=1)}
    assert r.counts == {"completed": 6, "failed": 0, "not_run": 0}
    assert all(j.included_in_ranking and j.failure_code is None for j in r.jobs)


def test_reference_only_marks_exactly_the_job_mismatch_group():
    r = run()
    assert {j.job_id for j in r.jobs if j.reference_only} == {"sample-2", "sample-4", "sample-5", "sample-6"}
    assert all(j.summary == j.eligibility.summary for j in r.jobs)  # 사유는 2-7 템플릿 그대로


def test_every_input_job_is_in_the_result_and_there_is_no_score_field():
    dump = run().model_dump()
    assert [j["job_id"] for j in dump["jobs"]] == IDS
    assert not any("score" in k for k in dump) and not any("score" in k for j in dump["jobs"] for k in j)


def test_run_info_for_mock_run():
    info = run().run_info
    assert (info.gate_mode, info.eligibility_mode, info.model_id) == ("injected", "mock", None)
    assert info.prompt_versions.extract_requirements and info.prompt_versions.find_evidence
    assert (info.cache_hits, info.input_tokens, info.output_tokens) == (0, 0, 0)


# ---------------------------------------------------------------- 입력 순서를 바꾼 경우: 순서 속성만 본다 (정책 14장 안정 정렬)

GROUP_ORDER = {"primary": 0, "job_mismatch": 1, "out_of_scope": 2}
VERDICT_ORDER = {"eligible": 0, "conditional": 1, "not_eligible": 2}


def expected_key(job_id):  # fixture 기대값에서 계산한다. 파이프라인 코드와 독립이다
    s = BY_ID[job_id]
    return GROUP_ORDER[s["gate_ranking_group"]], VERDICT_ORDER[s["expected"]["verdict"]]


@pytest.mark.parametrize("seed", range(40))
def test_shuffled_input_keeps_partial_order_and_stable_ties(seed):
    order = IDS[:]
    random.Random(seed).shuffle(order)
    r = run(samples=[BY_ID[i] for i in order])
    assert [j.job_id for j in r.jobs] == order
    ranking = r.ranking
    assert sorted(ranking) == sorted(IDS)
    keys = [expected_key(i) for i in ranking]
    assert keys == sorted(keys)  # 그룹 -> 판정의 부분 순서
    pos = ranking.index
    assert pos("sample-3") < pos("sample-1") < min(pos(i) for i in ("sample-2", "sample-4", "sample-5", "sample-6"))
    for a, b in (("sample-4", "sample-5"), ("sample-2", "sample-6")):  # 동점 쌍은 입력 순서를 유지한다
        assert (pos(a) < pos(b)) == (order.index(a) < order.index(b))


def test_no_extra_tie_breaker_such_as_job_id():
    samples = [BY_ID[i] for i in ("sample-6", "sample-2", "sample-5", "sample-4", "sample-1", "sample-3")]
    assert run(samples=samples).ranking == ["sample-3", "sample-1", "sample-5", "sample-4", "sample-6", "sample-2"]


# ---------------------------------------------------------------- 실패와 중단 (D6)

def test_extraction_failure_is_a_completed_conditional_result_not_a_failed_job():
    mock_e, mock_j = clients()
    r = run(extract=Wrap(mock_e, {("extract_requirements", "sample-2"): LLMStageError("통신 실패")}), evidence=mock_j)
    job = r.jobs[1]
    assert job.evaluation_status == "completed" and job.eligibility.extraction_issues == ["extraction_failed"]


def test_stage_failure_fails_only_that_job_and_the_rest_keep_their_order():
    mock_e, mock_j = clients()
    r = run(extract=mock_e, evidence=Wrap(mock_j, {("find_evidence", "sample-2"): LLMStageError("통신 실패")}))
    bad = r.jobs[1]
    assert (bad.evaluation_status, bad.failure_code, bad.included_in_ranking, bad.rank) == ("failed", "stage_failed", False, None)
    assert bad.gate is not None and bad.eligibility is None  # 게이트 결과는 남는다
    assert bad.summary == FAILURE_TEXT[("failed", "stage_failed")]
    assert r.counts == {"completed": 5, "failed": 1, "not_run": 0}
    assert r.ranking == ["sample-3", "sample-1", "sample-4", "sample-5", "sample-6"]


def test_invented_quote_is_evidence_check_failed_and_others_continue():
    bad = evidence_answer(BY_ID["sample-1"], EVIDENCE)
    bad["judgements"][1]["profile_evidence"] = [{"experience_id": "exp-1", "quote": "프로필에 없는 문장"}]
    mock_e, mock_j = clients()
    r = run(extract=mock_e, evidence=Wrap(mock_j, answers={("find_evidence", "sample-1"): bad}))
    assert r.jobs[0].failure_code == "evidence_check_failed"
    assert r.counts == {"completed": 5, "failed": 1, "not_run": 0}


def test_quota_exhaustion_fails_that_job_and_marks_the_rest_not_run_keeping_earlier_results():
    mock_e, mock_j = clients()
    r = run(extract=mock_e, evidence=Wrap(mock_j, {("find_evidence", "sample-4"): LLMQuotaExceeded("한도 소진")}))
    assert [(j.job_id, j.evaluation_status, j.failure_code) for j in r.jobs] == [
        ("sample-1", "completed", None), ("sample-2", "completed", None), ("sample-3", "completed", None),
        ("sample-4", "failed", "quota_exhausted"), ("sample-5", "not_run", "quota_exhausted"),
        ("sample-6", "not_run", "quota_exhausted")]
    assert r.ranking == ["sample-3", "sample-1", "sample-2"]
    assert r.counts == {"completed": 3, "failed": 1, "not_run": 2}
    assert all(j.gate is not None for j in r.jobs)  # 게이트 결과는 평가 상태와 무관하게 남는다
    assert r.jobs[4].summary == FAILURE_TEXT[("not_run", "quota_exhausted")]


def test_jobs_after_quota_exhaustion_are_not_started():
    mock_e, mock_j = clients()
    extract, evidence = Wrap(mock_e), Wrap(mock_j, {("find_evidence", "sample-1"): LLMQuotaExceeded("한도 소진")})
    r = run(extract=extract, evidence=evidence)
    assert r.counts == {"completed": 0, "failed": 1, "not_run": 5} and r.ranking == []
    assert [k[1] for k in extract.calls] == ["sample-1"] and [k[1] for k in evidence.calls] == ["sample-1"]


# ---------------------------------------------------------------- 게이트 실행 모드와 입력 오류

POSTING = {"job_title": "서비스 기획자", "position": "서비스 기획",
           "items": [{"text": "서비스 기획 및 화면 설계"}, {"text": "요구사항 정의서 작성"}]}
SERVICE_JUDGE = make_label_mock(["JF_SERVICE_PLANNING", "JF_SERVICE_PLANNING"])
G_SAMPLE = {**BY_ID["sample-3"], "job_id": "g-1"}


def gate_job(job_id="g-1", posting=POSTING):
    return ScenarioJob(job_id=job_id, posting_text=synthetic_posting(G_SAMPLE), gate_posting=posting)


def run_gate(jobs, **kw):
    mock_e, mock_j = clients([G_SAMPLE])
    return run_pipeline(jobs, PROFILE, REF, mock_e, mock_j, desired_family="JF_SERVICE_PLANNING", gate_judge=SERVICE_JUDGE, now=NOW, **kw)


def test_gate_runs_with_mock_ai_and_keeps_no_detail():
    r = run_gate([gate_job()])
    job = r.jobs[0]
    assert r.run_info.gate_mode == "mock"
    assert job.gate.result == "match" and job.gate.ranking_group == "primary" and job.gate.detail is None
    assert job.evaluation_status == "completed" and job.eligibility.gate_ranking_group == "primary"


def test_per_job_gate_judge_overrides_the_shared_one():
    job = ScenarioJob(job_id="g-1", posting_text=synthetic_posting(G_SAMPLE), gate_posting=POSTING, gate_judge=SERVICE_JUDGE)
    mock_e, mock_j = clients([G_SAMPLE])
    r = run_pipeline([job], PROFILE, REF, mock_e, mock_j, desired_family="JF_SERVICE_PLANNING", now=NOW)  # 공용 gate_judge 없음
    assert r.jobs[0].gate.result == "match"


def test_malformed_gate_input_is_input_invalid_for_that_job_only():
    bad = gate_job("g-0", {"job_title": "x", "position": "y", "items": [], "category": "z"})
    r = run_gate([bad, gate_job("g-1")])
    assert (r.jobs[0].evaluation_status, r.jobs[0].failure_code, r.jobs[0].gate) == ("failed", "input_invalid", None)
    assert r.jobs[1].evaluation_status == "completed" and r.ranking == ["g-1"]


def injected_job_with_detail():
    """detail(공고 업무 항목 원문)이 든 게이트 결과를 주입한 공고."""
    gate = judge_posting("g-1", POSTING, "JF_SERVICE_PLANNING", SERVICE_JUDGE)  # with_detail 기본값은 True
    assert gate.detail is not None and POSTING["items"][0]["text"] in gate.detail.model_dump_json()
    return ScenarioJob(job_id="g-1", posting_text=synthetic_posting(G_SAMPLE), gate=gate)


def test_injected_gate_detail_is_dropped_like_an_executed_gate():
    mock_e, mock_j = clients([G_SAMPLE])
    r = run_pipeline([injected_job_with_detail()], PROFILE, REF, mock_e, mock_j, now=NOW)
    assert r.jobs[0].gate.detail is None and r.jobs[0].evaluation_status == "completed"
    assert POSTING["items"][0]["text"] not in r.model_dump_json()  # 저장될 JSON에 업무 항목 원문이 없다


def test_a_run_whose_gate_still_has_detail_is_rejected_when_loaded():
    gate = injected_job_with_detail().gate
    data = run().jobs[0].model_dump()
    data["gate"] = json.loads(gate.model_dump_json())
    data["job_id"] = data["eligibility"]["job_id"] = gate.job_id
    data["gate"]["job_id"] = gate.job_id
    with pytest.raises(ValueError, match="gate.detail"):
        PipelineJob(**data)


@pytest.mark.parametrize("bad", [
    None, [], {}, {"job_title": 5, "items": []}, {"job_title": "x", "items": "업무"}, {"job_title": "x", "items": [5]},
    {"job_title": "x", "items": [{"heading": "h"}]}, {"job_title": "x", "items": [{"text": 5}]},
    {"job_title": "x", "items": [{"text": "t", "heading": 5}]}, {"job_title": "x", "position": 5, "items": []},
    {"job_title": "x", "items": [], "category": "z"},
])
def test_each_malformed_gate_input_is_input_invalid_and_the_next_job_continues(bad):
    r = run_gate([gate_job("g-0", bad), gate_job("g-1")])
    assert (r.jobs[0].evaluation_status, r.jobs[0].failure_code, r.jobs[0].gate) == ("failed", "input_invalid", None)
    assert r.jobs[1].evaluation_status == "completed"


def test_gate_input_with_no_items_is_not_an_input_error():
    r = run_gate([gate_job("g-1", {"job_title": "서비스 기획자", "items": []})])
    assert r.jobs[0].evaluation_status == "completed" and r.jobs[0].gate.result == "review_needed"  # 정보 부족 판정이다


@pytest.mark.parametrize("error", [TypeError("판정 함수의 결함"), KeyError("label"), AttributeError("x")])
def test_defects_inside_the_gate_judge_are_not_hidden_as_input_errors(error):
    def broken(request):
        raise error

    mock_e, mock_j = clients([G_SAMPLE])
    # 규칙이 단서를 찾지 못해 판정 AI 자리를 부르는 입력이다 (POSTING은 규칙만으로 결정돼 AI 자리를 부르지 않는다)
    needs_ai = {"job_title": "담당자 모집", "position": "담당자", "items": [{"text": "회사의 다양한 업무를 지원하고 필요한 일을 수행"}, {"text": "기타 관련 업무 전반"}]}
    with pytest.raises(type(error)):
        run_pipeline([gate_job("g-1", needs_ai)], PROFILE, REF, mock_e, mock_j, desired_family="JF_SERVICE_PLANNING", gate_judge=broken, now=NOW)


@pytest.mark.parametrize("error,code", [
    (ProfileEvidenceError("원문이 아닌 다른 문구"), "evidence_check_failed"),   # 메시지 문구와 무관하게 타입으로 가른다
    (ValueError("프로필 근거 검증 실패"), "assembly_failed"),                 # 문구가 같아도 타입이 다르면 조립 실패다
    (ValueError("anything"), "assembly_failed"),
])
def test_failure_code_follows_the_exception_type_not_the_message(error, code):
    mock_e, mock_j = clients()
    r = run(extract=mock_e, evidence=Wrap(mock_j, {("find_evidence", "sample-1"): error}))
    assert r.jobs[0].failure_code == code and r.counts["failed"] == 1


def test_invalid_scenario_is_refused_instead_of_recorded_per_job():
    jobs = scenario()
    with pytest.raises(ValueError, match="같은 job_id"):
        run(jobs=[jobs[0], jobs[0]])
    with pytest.raises(ValueError, match="섞을 수 없다"):
        run(jobs=[jobs[0], gate_job("g-1")], desired_family="JF_SERVICE_PLANNING", gate_judge=SERVICE_JUDGE)
    with pytest.raises(ValueError, match="gate_judge"):
        run(jobs=[gate_job()], desired_family="JF_SERVICE_PLANNING")
    with pytest.raises(ValueError, match="desired_family"):
        run(jobs=[gate_job()], gate_judge=SERVICE_JUDGE)
    with pytest.raises(ValueError, match="job_id"):
        run(jobs=[ScenarioJob(job_id="sample-1", posting_text="x", gate=GATES["sample-2"])])
    with pytest.raises(ValueError, match="모델이 다르다"):
        run(extract=FakeClient([], model="other"))


def test_external_client_without_permission_is_not_swallowed():
    mock_e, mock_j = clients()
    mock_j.external = True
    with pytest.raises(PermissionError):
        run(extract=mock_e, evidence=mock_j)


def test_config_error_is_not_swallowed():
    mock_e, mock_j = clients()
    with pytest.raises(LLMConfigError):
        run(extract=mock_e, evidence=Wrap(mock_j, {("find_evidence", "sample-1"): LLMConfigError("키 없음")}))


# ---------------------------------------------------------------- 저장 형식: 재로딩, 인용 제외, 무결성

def all_quotes(r):
    return [e.quote for j in r.jobs if j.eligibility for res in j.eligibility.requirement_results
            for e in [*res.profile_evidence, *(x for it in res.item_results for x in it.profile_evidence)]]


def test_round_trip_through_json_gives_the_same_run():
    r = run()
    again = PipelineRun.model_validate_json(r.model_dump_json())
    assert again == r and again.ranking == POLICY_14_ORDER


def test_without_profile_evidence_removes_quotes_but_keeps_statuses():
    r = run()
    quotes = all_quotes(r)
    assert r.profile_evidence_included and quotes
    plain = r.without_profile_evidence()
    assert plain.profile_evidence_included is False and all_quotes(plain) == []
    for a, b in zip(r.jobs, plain.jobs):
        assert (a.eligibility.verdict, a.eligibility.decisive_requirement_ids, a.eligibility.summary) == (
            b.eligibility.verdict, b.eligibility.decisive_requirement_ids, b.eligibility.summary)
        assert [(x.status, x.reason_code, x.compared, x.job_quote) for x in a.eligibility.requirement_results] == [
            (x.status, x.reason_code, x.compared, x.job_quote) for x in b.eligibility.requirement_results]
    assert plain.ranking == r.ranking
    assert PipelineRun.model_validate_json(plain.model_dump_json()) == plain
    # 공고에도 같은 낱말이 있으면(예: Excel) 저장본에 남는 것이 당연하다. 공고 쪽에 없는 인용만 새지 않았는지 본다
    posting_side = " ".join([q for j in r.jobs for q in (
        [req.text for req in j.eligibility.requirements] + [i.text for req in j.eligibility.requirements for i in req.items]
        + [res.job_quote or "" for res in j.eligibility.requirement_results])])
    private = [q for q in quotes if q not in posting_side]
    dumped = plain.model_dump_json()
    assert private and not any(q in dumped for q in private)
    assert all_quotes(r) == quotes  # 원본은 바뀌지 않는다


def test_flag_false_with_quotes_is_rejected():
    data = run().model_dump()
    data["profile_evidence_included"] = False
    with pytest.raises(ValueError, match="프로필 인용"):
        PipelineRun.model_validate(data)


def test_tampered_rank_is_rejected():
    data = run().model_dump()
    first, third = data["jobs"][0], data["jobs"][2]  # sample-1, sample-3
    first["rank"], third["rank"] = third["rank"], first["rank"]
    with pytest.raises(ValueError, match="rank_jobs"):
        PipelineRun.model_validate(data)


def test_duplicate_job_ids_are_rejected():
    data = run().model_dump()
    data["jobs"][1] = data["jobs"][0]
    with pytest.raises(ValueError, match="같은 job_id"):
        PipelineRun.model_validate(data)


@pytest.mark.parametrize("status,code", [("completed", "stage_failed"), ("failed", None), ("not_run", "stage_failed"), ("not_run", None)])
def test_invalid_status_and_failure_code_pairs_are_rejected(status, code):
    with pytest.raises(ValueError):
        PipelineJob(job_id="sample-1", evaluation_status=status, failure_code=code, gate=GATES["sample-1"])


def test_gate_may_be_missing_only_for_input_invalid():
    PipelineJob(job_id="x", evaluation_status="failed", failure_code="input_invalid")
    with pytest.raises(ValueError, match="input_invalid"):
        PipelineJob(job_id="x", evaluation_status="failed", failure_code="stage_failed")


@pytest.mark.parametrize("drop", ["rank", "gate", "eligibility"])
def test_completed_job_needs_rank_gate_and_eligibility(drop):
    job = run().jobs[0]
    with pytest.raises(ValueError):
        PipelineJob(**{**job.model_dump(), drop: None})


@pytest.mark.parametrize("extra", ["eligibility", "rank"])
def test_job_that_did_not_complete_cannot_carry_a_result_or_rank(extra):
    done = run().jobs[0].model_dump()
    with pytest.raises(ValueError, match="eligibility와 rank"):
        PipelineJob(job_id="sample-1", evaluation_status="failed", failure_code="stage_failed", gate=GATES["sample-1"],
                    **{extra: done[extra]})


def test_job_id_of_gate_and_eligibility_must_match_the_job():
    job = run().jobs[0]
    with pytest.raises(ValueError, match="job_id"):
        PipelineJob(**{**job.model_dump(), "job_id": "sample-9"})


# ---------------------------------------------------------------- 실행 ID, 지문, 사용량

def test_run_id_is_utc_time_plus_random_suffix():
    a, b = make_run_id(NOW), make_run_id(NOW)
    assert a.startswith("20261104T061500Z-") and len(a.split("-")[1]) == 6 and a != b
    assert run().run_id.startswith("20261104T061500Z-")


def test_fingerprint_depends_on_ids_and_versions_only():
    base = run().input_fingerprint
    assert base == run().input_fingerprint
    assert run(samples=SAMPLES[:5]).input_fingerprint != base
    changed = [ScenarioJob(job_id=j.job_id, posting_text="다른 본문", gate=j.gate) for j in scenario()]
    assert run(jobs=changed).input_fingerprint == base  # 공고·프로필 원문은 해시에 들어가지 않는다
    mock_e, mock_j = clients()
    mock_e.model = mock_j.model = "gemini-test"
    assert run(extract=mock_e, evidence=mock_j).input_fingerprint != base  # 모델이 다르면 다른 실행이다


def test_usage_totals_and_cache_mode_reach_run_info(tmp_path):
    usage = UsageLog(tmp_path / "u.jsonl", run_id="r1")
    request = LLMRequest(stage="find_evidence", prompt="p", schema=dict, job_id="sample-1")
    usage.record(request, model="m", input_tokens=7, output_tokens=3)
    usage.record(request, model="m", cached=True)
    info = run(usage=usage, cache_mode="fresh").run_info
    assert (info.cache_mode, info.cache_hits, info.input_tokens, info.output_tokens) == ("fresh", 1, 7, 3)


def test_ai_mode_records_model_and_config():
    mock_e, mock_j = clients()
    mock_e.model = mock_j.model = "gemini-test"
    mock_j.config_id = "cfg1"
    info = run(extract=mock_e, evidence=mock_j).run_info
    assert (info.eligibility_mode, info.model_id, info.generation_config) == ("ai", "gemini-test", "cfg1")
