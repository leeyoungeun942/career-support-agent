"""pipeline_run.v1의 게이트 AI 확장: gate_failed, gate가 없는 조합, 게이트 단계 한도 소진, RunInfo 칸 (data-contract 3-1, 3-3; 이슈 #16). 네트워크를 쓰지 않는다."""
from pathlib import Path

import pytest

from core.llm_client import LLMRequest, UsageLog
from core.pipeline_run import FAILURE_TEXT, PipelineJob, PipelineRun, RunInfo
from pipeline_helpers import GATES, POLICY_14_ORDER, run

BEFORE_GATE_AI = Path(__file__).parent / "fixtures" / "pipeline_run_v1_before_gate_ai.json"


def job(sid, status, code=None, *, gate=True):
    return PipelineJob(job_id=sid, evaluation_status=status, failure_code=code, gate=GATES[sid] if gate else None)


def with_jobs(jobs, **info):
    """run()의 나머지 칸은 그대로 두고 jobs(와 run_info 일부)만 바꾼 실행을 검증하며 만든다."""
    data = run().model_dump()
    data["jobs"] = [j.model_dump() for j in jobs]
    data["run_info"].update(info)
    return PipelineRun.model_validate(data)


# ---------------------------------------------------------------- PipelineJob: gate_failed와 gate가 없는 조합

def test_gate_failed_has_a_screen_text_and_only_the_failed_status():
    j = job("sample-1", "failed", "gate_failed", gate=False)
    assert (j.gate, j.eligibility, j.rank, j.included_in_ranking) == (None, None, None, False)
    assert j.summary == FAILURE_TEXT[("failed", "gate_failed")] == "평가 실패: 게이트 AI 오류"
    with pytest.raises(ValueError, match="함께 쓸 수 없다"):
        job("sample-1", "not_run", "gate_failed", gate=False)
    with pytest.raises(ValueError, match="completed이면 failure_code"):
        job("sample-1", "completed", "gate_failed", gate=False)


def test_gate_failed_with_a_gate_result_is_rejected():
    with pytest.raises(ValueError, match="gate_failed이면 gate가 없어야"):
        job("sample-1", "failed", "gate_failed", gate=True)


@pytest.mark.parametrize("extra", ["eligibility", "rank"])
def test_gate_failed_cannot_carry_an_eligibility_result_or_rank(extra):
    done = run().jobs[0].model_dump()
    with pytest.raises(ValueError, match="eligibility와 rank"):
        PipelineJob(job_id="sample-1", evaluation_status="failed", failure_code="gate_failed", **{extra: done[extra]})


@pytest.mark.parametrize("code", ["stage_failed", "evidence_check_failed", "assembly_failed"])
def test_other_failure_codes_still_need_a_gate(code):
    with pytest.raises(ValueError, match="input_invalid"):
        job("sample-1", "failed", code, gate=False)


@pytest.mark.parametrize("status", ["failed", "not_run"])
def test_quota_exhausted_may_have_no_gate_and_may_have_one(status):
    assert job("sample-1", "failed" if status == "failed" else "not_run", "quota_exhausted", gate=False).gate is None
    assert job("sample-1", status, "quota_exhausted", gate=True).gate is not None


def test_completed_still_needs_a_gate():
    done = run().jobs[0].model_dump()
    with pytest.raises(ValueError):
        PipelineJob(**{**done, "gate": None})


# ---------------------------------------------------------------- PipelineRun: 한도 소진이 난 단계

def gate_stage_exhaustion(override=None):
    """sample-1 게이트는 끝남(not_run), sample-2 소진(failed), 뒤 둘은 게이트를 시작하지 않음."""
    jobs = [job("sample-1", "not_run", "quota_exhausted"), job("sample-2", "failed", "quota_exhausted", gate=False),
            job("sample-3", "not_run", "quota_exhausted", gate=False), job("sample-4", "not_run", "quota_exhausted", gate=False)]
    for index, replacement in (override or {}).items():
        jobs[index] = replacement
    return with_jobs(jobs)


def test_gate_stage_exhaustion_is_a_valid_run_and_keeps_the_earlier_gate():
    r = gate_stage_exhaustion()
    assert r.counts == {"completed": 0, "failed": 1, "not_run": 3} and r.ranking == []
    assert r.jobs[0].gate is not None and r.jobs[1].gate is None


def test_gate_stage_exhaustion_cannot_have_a_completed_job():
    done = run().jobs[0]
    with pytest.raises(ValueError, match="completed 공고가 없어야"):
        gate_stage_exhaustion({0: done})


def test_gate_stage_exhaustion_needs_exactly_one_failed_job():
    with pytest.raises(ValueError, match="정확히 하나"):
        gate_stage_exhaustion({1: job("sample-2", "not_run", "quota_exhausted", gate=False)})
    with pytest.raises(ValueError, match="정확히 하나"):
        gate_stage_exhaustion({3: job("sample-4", "failed", "quota_exhausted", gate=False)})


def test_jobs_after_the_exhausted_one_must_not_have_started():
    with pytest.raises(ValueError, match="게이트 단계 소진 뒤"):
        gate_stage_exhaustion({2: job("sample-3", "not_run", "quota_exhausted", gate=True)})  # 게이트를 시작하지 않았는데 결과가 있다
    with pytest.raises(ValueError, match="게이트 단계 소진 뒤"):
        gate_stage_exhaustion({3: job("sample-4", "failed", "input_invalid", gate=False)})  # 뒤 공고는 입력 검사도 하지 않는다


def test_jobs_before_the_exhausted_one_keep_their_gate_or_ended_with_a_gate_failure():
    with pytest.raises(ValueError, match="게이트 단계 소진 앞"):
        gate_stage_exhaustion({0: job("sample-1", "not_run", "quota_exhausted", gate=False)})  # 게이트가 끝났는데 결과가 없다
    with pytest.raises(ValueError, match="게이트 단계 소진 앞"):
        gate_stage_exhaustion({0: job("sample-1", "failed", "stage_failed")})
    ok = gate_stage_exhaustion({0: job("sample-1", "failed", "gate_failed", gate=False)})  # 앞에서 이미 게이트 실패로 끝난 공고
    assert ok.jobs[0].failure_code == "gate_failed"


def test_eligibility_stage_exhaustion_may_keep_completed_jobs():
    done = run().jobs[2]  # sample-3, 혼자 완료되면 1위
    r = with_jobs([done.model_copy(update={"rank": 1}), job("sample-1", "failed", "quota_exhausted"), job("sample-2", "not_run", "quota_exhausted")])
    assert r.ranking == ["sample-3"] and r.counts == {"completed": 1, "failed": 1, "not_run": 1}


def test_gate_failed_and_completed_jobs_may_be_in_the_same_run():
    jobs = [job("sample-1", "failed", "gate_failed", gate=False)] + [j.model_copy(update={"rank": n}) for n, j in
            enumerate([j for j in run().jobs if j.job_id in ("sample-3", "sample-4")], start=1)]
    r = with_jobs(jobs)
    assert r.ranking == ["sample-3", "sample-4"] and r.counts == {"completed": 2, "failed": 1, "not_run": 0}


def test_run_with_gate_failed_survives_save_and_reload():
    r = with_jobs([job("sample-2", "failed", "gate_failed", gate=False)] + [j.model_copy(update={"rank": 1}) for j in run().jobs if j.job_id == "sample-3"])
    again = PipelineRun.model_validate_json(r.model_dump_json())
    assert again == r and again.jobs[0].summary == "평가 실패: 게이트 AI 오류" and again.ranking == ["sample-3"]
    tampered = r.model_dump()
    tampered["jobs"][1]["rank"] = 2  # 저장 뒤 순위를 바꾸면 다시 읽을 때 거부된다
    with pytest.raises(ValueError, match="rank_jobs"):
        PipelineRun.model_validate(tampered)


# ---------------------------------------------------------------- RunInfo

def test_gate_mode_ai_needs_the_gate_model_and_prompt_version():
    info = run().run_info.model_dump()
    with pytest.raises(ValueError, match="gate_model_id"):
        RunInfo(**{**info, "gate_mode": "ai"})
    with pytest.raises(ValueError, match="gate_model_id"):
        RunInfo(**{**info, "gate_mode": "ai", "gate_model_id": "m"})  # 프롬프트 버전이 없다
    with pytest.raises(ValueError, match="gate_model_id"):
        RunInfo(**{**info, "gate_mode": "mock", "gate_model_id": "m", "gate_prompt_version": "4"})  # ai가 아닌데 모델이 있다
    assert RunInfo(**{**info, "gate_mode": "ai", "gate_model_id": "m", "gate_prompt_version": "4", "model_id": None,
                      "eligibility_mode": "mock"}).gate_model_id == "m"


def test_gate_and_eligibility_must_use_the_same_model():
    info = {**run().run_info.model_dump(), "eligibility_mode": "ai", "model_id": "m1", "gate_mode": "ai", "gate_prompt_version": "4"}
    assert RunInfo(**{**info, "gate_model_id": "m1"}).gate_model_id == "m1"
    with pytest.raises(ValueError, match="모델이 다르다"):
        RunInfo(**{**info, "gate_model_id": "m2"})


# ---------------------------------------------------------------- 변경 전 형식의 실제 파일

def test_a_file_saved_before_the_gate_ai_change_still_loads_with_defaults():
    r = PipelineRun.model_validate_json(BEFORE_GATE_AI.read_text(encoding="utf-8"))
    assert r.schema_version == "pipeline_run.v1" and r.ranking == POLICY_14_ORDER
    info = r.run_info
    assert (info.gate_mode, info.gate_model_id, info.gate_prompt_version) == ("injected", None, None)
    assert (info.gate_logical_calls, info.gate_attempts, info.gate_cache_hits, info.gate_input_tokens, info.gate_output_tokens) == (0, 0, 0, 0, 0)
    assert (info.eligibility_logical_calls, info.eligibility_attempts) == (0, 0)
    assert PipelineRun.model_validate_json(r.model_dump_json()) == r  # 다시 저장해도 같다


# ---------------------------------------------------------------- UsageLog: 단계별 합계와 논리 호출

def record(usage, stage, job_id, **kw):
    usage.record(LLMRequest(stage=stage, prompt="p", schema=dict, job_id=job_id), model="m", **kw)


def test_run_totals_split_by_stage_and_logical_calls_collapse_retries(tmp_path):
    usage = UsageLog(tmp_path / "u.jsonl", run_id="r1")
    for _ in range(3):  # 같은 요청이 재시도 끝에 성공: 시도 4, 논리 호출 1
        record(usage, "gate_label", "sample-1:item:0:subtask:0", ok=False)
    record(usage, "gate_label", "sample-1:item:0:subtask:0", input_tokens=10, output_tokens=2)
    record(usage, "gate_label", "sample-1:item:0:subtask:1", cached=True)
    record(usage, "gate_label", "sample-1:item:1:subtask:0", input_tokens=5, output_tokens=1)
    record(usage, "extract_requirements", "sample-1", input_tokens=100, output_tokens=20)
    record(usage, "find_evidence", "sample-1", input_tokens=200, output_tokens=30)
    UsageLog(tmp_path / "u.jsonl", run_id="other")  # 다른 실행의 기록은 더하지 않는다
    record(UsageLog(tmp_path / "u.jsonl", run_id="other"), "gate_label", "sample-9:item:0:subtask:0", input_tokens=999)

    gate = usage.run_totals(stage="gate_label")
    assert gate == {"calls": 5, "failed": 3, "cache_hits": 1, "input_tokens": 15, "output_tokens": 3}
    assert usage.logical_calls(stage="gate_label") == 3  # 재시도 4행은 하나, 캐시 적중도 센다
    rest = usage.run_totals(exclude_stage="gate_label")
    assert rest == {"calls": 2, "failed": 0, "cache_hits": 0, "input_tokens": 300, "output_tokens": 50}
    assert usage.logical_calls(exclude_stage="gate_label") == 2
    whole = usage.run_totals()  # 필터가 없으면 지금까지와 같다
    assert whole["calls"] == 7 and whole["cache_hits"] == 1 and usage.logical_calls() == 5


def test_usage_filters_on_an_empty_log_are_zero(tmp_path):
    usage = UsageLog(tmp_path / "none.jsonl", run_id="r1")
    assert usage.run_totals(stage="gate_label")["calls"] == 0 and usage.logical_calls(stage="gate_label") == 0
