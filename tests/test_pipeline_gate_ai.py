"""run_pipeline의 게이트 AI 실패 처리: gate_failed, 게이트 단계 한도 소진, 판정 함수 결함 전파, 모델·사용량 기록 (이슈 #16). 네트워크를 쓰지 않는다."""
import pytest

from core.job_gate_mock import make_label_mock
from core.llm_client import FakeClient, LLMQuotaExceeded, LLMRequest, LLMStageError, UsageLog
from core.llm_mock import synthetic_posting
from core.pipeline import ScenarioJob, run_pipeline
from core.pipeline_run import PipelineRun
from core.prompts import load_prompt
from pipeline_helpers import IDS, NOW, PROFILE, REF, SAMPLES, Wrap, clients

# 규칙이 단서를 찾지 못해 게이트 AI 자리를 부르는 입력이다 (POSTING 같은 입력은 규칙만으로 결정돼 AI를 부르지 않는다)
NEEDS_AI = {"job_title": "담당자 모집", "position": "담당자",
            "items": [{"text": "회사의 다양한 업무를 지원하고 필요한 일을 수행"}, {"text": "기타 관련 업무 전반"}]}
DESIRED = "JF_SERVICE_PLANNING"


class Judge:
    """공고 하나의 게이트 AI 자리: 부를 때마다 기록하고, error가 있으면 던진다. 아니면 서비스기획 라벨을 돌려준다."""

    def __init__(self, error=None):
        self.error, self.calls = error, 0

    def __call__(self, request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"label": DESIRED, "reason": "서비스 기획 업무 (테스트)"}


def jobs_with(errors=None, ids=IDS):
    """errors: {job_id: 예외}. 반환: (ScenarioJob 목록, {job_id: Judge})."""
    judges = {i: Judge((errors or {}).get(i)) for i in ids}
    by_id = {s["job_id"]: s for s in SAMPLES}
    return [ScenarioJob(job_id=i, posting_text=synthetic_posting(by_id[i]), gate_posting=NEEDS_AI, gate_judge=judges[i]) for i in ids], judges


def gate_run(errors=None, *, extract=None, evidence=None, gate_client=None, ids=IDS, **kw):
    jobs, judges = jobs_with(errors, ids)
    samples = [s for s in SAMPLES if s["job_id"] in ids]
    mock_e, mock_j = clients(samples)
    run = run_pipeline(jobs, PROFILE, REF, extract or mock_e, evidence or mock_j, desired_family=DESIRED, gate_client=gate_client or FakeClient([], model="mock"),
                       now=NOW, **kw)
    return run, judges


# ---------------------------------------------------------------- 게이트 호출 실패는 그 공고만 gate_failed

@pytest.mark.parametrize("bad", ["sample-1", "sample-3", "sample-6"])  # 처음, 중간, 마지막
def test_gate_call_failure_fails_only_that_job_and_the_rest_continue(bad):
    mock_e, mock_j = clients()
    extract, evidence = Wrap(mock_e), Wrap(mock_j)
    r, judges = gate_run({bad: LLMStageError("통신 실패")}, extract=extract, evidence=evidence)
    failed = next(j for j in r.jobs if j.job_id == bad)
    assert (failed.evaluation_status, failed.failure_code, failed.gate, failed.eligibility, failed.rank) == ("failed", "gate_failed", None, None, None)
    assert failed.included_in_ranking is False and failed.summary == "평가 실패: 게이트 AI 오류"
    assert r.counts == {"completed": 5, "failed": 1, "not_run": 0} and bad not in r.ranking and len(r.ranking) == 5
    assert all(j.evaluation_status == "completed" for j in r.jobs if j.job_id != bad)
    assert all(judge.calls > 0 for judge in judges.values())  # 실패한 뒤 공고의 게이트도 시작했다
    assert bad not in {k[1] for k in extract.calls + evidence.calls}  # 게이트가 없는 공고는 자격요건 단계를 시작하지 않는다
    assert r.run_info.gate_mode == "ai"


def test_every_gate_can_fail_and_the_run_still_exists_and_reloads():
    r, _ = gate_run({i: LLMStageError("x") for i in IDS})
    assert r.counts == {"completed": 0, "failed": 6, "not_run": 0} and r.ranking == []
    assert all(j.failure_code == "gate_failed" and j.gate is None for j in r.jobs)
    assert PipelineRun.model_validate_json(r.model_dump_json()) == r


def test_gate_failed_input_invalid_and_eligibility_failure_can_share_a_run():
    mock_e, mock_j = clients()
    jobs, _ = jobs_with({"sample-2": LLMStageError("x")})
    jobs[0] = ScenarioJob(job_id="sample-1", posting_text=jobs[0].posting_text, gate_posting={"job_title": 5, "items": []}, gate_judge=Judge())
    r = run_pipeline(jobs, PROFILE, REF, mock_e, Wrap(mock_j, {("find_evidence", "sample-4"): LLMStageError("근거 단계 실패")}),
                     desired_family=DESIRED, gate_client=FakeClient([], model="mock"), now=NOW)
    codes = {j.job_id: (j.evaluation_status, j.failure_code) for j in r.jobs}
    assert codes["sample-1"] == ("failed", "input_invalid") and codes["sample-2"] == ("failed", "gate_failed")
    assert codes["sample-4"] == ("failed", "stage_failed") and r.counts == {"completed": 3, "failed": 3, "not_run": 0}


# ---------------------------------------------------------------- 게이트 단계 한도 소진

def test_gate_stage_quota_stops_the_remaining_gates_and_all_eligibility_work():
    mock_e, mock_j = clients()
    extract, evidence = Wrap(mock_e), Wrap(mock_j)
    r, judges = gate_run({"sample-3": LLMQuotaExceeded("일일 한도")}, extract=extract, evidence=evidence)
    assert [(j.job_id, j.evaluation_status, j.failure_code, j.gate is not None) for j in r.jobs] == [
        ("sample-1", "not_run", "quota_exhausted", True), ("sample-2", "not_run", "quota_exhausted", True),
        ("sample-3", "failed", "quota_exhausted", False), ("sample-4", "not_run", "quota_exhausted", False),
        ("sample-5", "not_run", "quota_exhausted", False), ("sample-6", "not_run", "quota_exhausted", False)]
    assert r.counts == {"completed": 0, "failed": 1, "not_run": 5} and r.ranking == []
    assert [judges[i].calls > 0 for i in IDS] == [True, True, True, False, False, False]  # 소진 뒤 공고의 게이트는 시작하지 않는다
    assert extract.calls == [] and evidence.calls == []  # 자격요건 모델 호출 0회
    assert r.jobs[0].gate.job_id == "sample-1" and r.jobs[0].gate.detail is None  # 앞 결과(게이트)는 남는다
    assert r.jobs[0].summary == "평가 안 함: 호출 한도 소진" and r.jobs[2].summary == "평가 중단: 호출 한도 소진"
    assert PipelineRun.model_validate_json(r.model_dump_json()) == r


def test_gate_stage_quota_on_the_first_job_leaves_nothing_started():
    r, judges = gate_run({"sample-1": LLMQuotaExceeded("x")})
    assert r.counts == {"completed": 0, "failed": 1, "not_run": 5} and [judges[i].calls > 0 for i in IDS] == [True] + [False] * 5


def test_later_jobs_are_not_even_input_checked_after_quota():
    jobs, _ = jobs_with({"sample-1": LLMQuotaExceeded("x")})
    jobs[1] = ScenarioJob(job_id="sample-2", posting_text=jobs[1].posting_text, gate_posting={"job_title": 5}, gate_judge=Judge())  # 입력이 틀렸어도
    mock_e, mock_j = clients()
    r = run_pipeline(jobs, PROFILE, REF, mock_e, mock_j, desired_family=DESIRED, gate_client=FakeClient([], model="mock"), now=NOW)
    assert (r.jobs[1].evaluation_status, r.jobs[1].failure_code) == ("not_run", "quota_exhausted")


def test_earlier_gate_failure_stays_a_gate_failure_when_quota_hits_later():
    r, _ = gate_run({"sample-1": LLMStageError("x"), "sample-2": LLMQuotaExceeded("x")})
    assert [(j.job_id, j.evaluation_status, j.failure_code) for j in r.jobs[:3]] == [
        ("sample-1", "failed", "gate_failed"), ("sample-2", "failed", "quota_exhausted"), ("sample-3", "not_run", "quota_exhausted")]
    assert r.counts == {"completed": 0, "failed": 2, "not_run": 4}


def test_eligibility_stage_quota_still_keeps_completed_jobs_and_every_gate():
    mock_e, mock_j = clients()
    r, judges = gate_run(evidence=Wrap(mock_j, {("find_evidence", "sample-4"): LLMQuotaExceeded("x")}), extract=mock_e)
    assert r.counts == {"completed": 3, "failed": 1, "not_run": 2} and all(j.gate is not None for j in r.jobs)
    assert all(judge.calls > 0 for judge in judges.values())  # 게이트 단계는 끝까지 돌았다


# ---------------------------------------------------------------- 판정 함수의 결함은 숨기지 않는다

@pytest.mark.parametrize("error", [TypeError("판정 함수의 결함"), KeyError("label"), AttributeError("x"), ValueError("결함")])
def test_defects_in_the_gate_judge_still_propagate_with_a_gate_client(error):
    jobs, _ = jobs_with({"sample-2": error})
    mock_e, mock_j = clients()
    with pytest.raises(type(error)):
        run_pipeline(jobs, PROFILE, REF, mock_e, mock_j, desired_family=DESIRED, gate_client=FakeClient([], model="mock"), now=NOW)


def test_config_and_permission_errors_from_the_gate_are_not_swallowed():
    from core.llm_client import LLMConfigError

    for error in (LLMConfigError("키 없음"), PermissionError("허용 없음")):
        jobs, _ = jobs_with({"sample-1": error})
        mock_e, mock_j = clients()
        with pytest.raises(type(error)):
            run_pipeline(jobs, PROFILE, REF, mock_e, mock_j, desired_family=DESIRED, gate_client=FakeClient([], model="mock"), now=NOW)


# ---------------------------------------------------------------- 실행 조건 기록: 모델, 프롬프트, 사용량

def test_gate_ai_run_records_gate_model_prompt_and_mode():
    r, _ = gate_run(gate_client=FakeClient([], model="mock"))
    info = r.run_info
    assert (info.gate_mode, info.gate_model_id, info.gate_prompt_version) == ("ai", "mock", load_prompt("gate_label")[0])
    assert info.eligibility_mode == "mock" and info.model_id is None


def test_same_model_is_required_for_both_stages():
    mock_e, mock_j = clients()
    mock_e.model = mock_j.model = "m-1"
    jobs, judges = jobs_with()
    with pytest.raises(ValueError, match="모델이 다르다"):
        run_pipeline(jobs, PROFILE, REF, mock_e, mock_j, desired_family=DESIRED, gate_client=FakeClient([], model="m-2"), now=NOW)
    assert all(judge.calls == 0 for judge in judges.values())  # 게이트를 돌리기 전에 거부한다(호출을 쓰지 않는다)
    ok = run_pipeline(jobs, PROFILE, REF, mock_e, mock_j, desired_family=DESIRED, gate_client=FakeClient([], model="m-1"), now=NOW)
    assert (ok.run_info.model_id, ok.run_info.gate_model_id) == ("m-1", "m-1")


def test_gate_client_cannot_be_combined_with_injected_gates():
    from pipeline_helpers import scenario

    mock_e, mock_j = clients()
    with pytest.raises(ValueError, match="gate_client"):
        run_pipeline(scenario(), PROFILE, REF, mock_e, mock_j, gate_client=FakeClient([], model="mock"), now=NOW)


def test_fingerprint_changes_with_the_gate_model_but_not_for_runs_without_a_gate_client():
    a, _ = gate_run(gate_client=FakeClient([], model="mock"))
    mock_e, mock_j = clients()
    jobs, _ = jobs_with()
    plain = run_pipeline(jobs, PROFILE, REF, mock_e, mock_j, desired_family=DESIRED, gate_judge=Judge(), now=NOW)
    assert plain.run_info.gate_mode == "mock" and plain.input_fingerprint != a.input_fingerprint
    again, _ = gate_run(gate_client=FakeClient([], model="mock"))
    assert again.input_fingerprint == a.input_fingerprint


def test_usage_is_split_between_the_gate_stage_and_the_eligibility_stage(tmp_path):
    usage = UsageLog(tmp_path / "u.jsonl", run_id="r1")

    def record(stage, job_id, **kw):
        usage.record(LLMRequest(stage=stage, prompt="p", schema=dict, job_id=job_id), model="m", **kw)

    record("gate_label", "sample-1:item:0:subtask:0", ok=False)
    record("gate_label", "sample-1:item:0:subtask:0", input_tokens=10, output_tokens=2)  # 재시도 끝에 성공: 논리 1, 시도 2
    record("gate_label", "sample-1:item:1:subtask:0", cached=True)
    record("extract_requirements", "sample-1", input_tokens=100, output_tokens=20)
    record("find_evidence", "sample-1", cached=True)
    r, _ = gate_run(usage=usage)
    info = r.run_info
    assert (info.gate_logical_calls, info.gate_attempts, info.gate_cache_hits, info.gate_input_tokens, info.gate_output_tokens) == (2, 2, 1, 10, 2)
    assert (info.eligibility_logical_calls, info.eligibility_attempts, info.cache_hits, info.input_tokens, info.output_tokens) == (2, 1, 1, 100, 20)


def test_a_run_without_a_gate_client_keeps_the_old_totals(tmp_path):
    usage = UsageLog(tmp_path / "u.jsonl", run_id="r1")
    usage.record(LLMRequest(stage="find_evidence", prompt="p", schema=dict, job_id="sample-1"), model="m", input_tokens=7, output_tokens=3)
    from pipeline_helpers import run

    info = run(usage=usage).run_info
    assert (info.input_tokens, info.output_tokens, info.eligibility_attempts, info.gate_attempts, info.gate_model_id) == (7, 3, 1, 0, None)


def test_mock_gate_without_a_gate_client_is_unchanged():
    mock_e, mock_j = clients()
    jobs = [ScenarioJob(job_id=s["job_id"], posting_text=synthetic_posting(s), gate_posting=NEEDS_AI,
                        gate_judge=make_label_mock([DESIRED, DESIRED])) for s in SAMPLES]
    r = run_pipeline(jobs, PROFILE, REF, mock_e, mock_j, desired_family=DESIRED, now=NOW)
    assert r.run_info.gate_mode == "mock" and r.run_info.gate_model_id is None and r.counts["completed"] == 6
