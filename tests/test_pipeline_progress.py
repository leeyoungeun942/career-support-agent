"""run_pipeline의 진행 알림 on_progress (이슈 #28, 정책 presentation.md 3-1). 네트워크를 쓰지 않는다."""
import pytest

from core.llm_client import LLMQuotaExceeded, LLMStageError
from core.pipeline import ScenarioJob, run_pipeline
from pipeline_helpers import IDS, NOW, PROFILE, REF, SAMPLES, Wrap, clients, run
from test_pipeline_gate_ai import DESIRED, GATE_CLIENT, NEEDS_AI, Judge, gate_run, jobs_with

N = len(IDS)


def collect():
    events = []
    return events, lambda *args: events.append(args)


def expected(gate, eligibility):
    """gate, eligibility: 공고 순서대로의 상태 목록(None이면 그 단계의 알림이 없다)."""
    out = []
    for stage, statuses in (("gate", gate), ("eligibility", eligibility)):
        if statuses is not None:
            out += [(stage, i, N, IDS[i - 1], status) for i, status in enumerate(statuses, start=1)]
    return out


# ---------------------------------------------------------------- 기대 이벤트 목록 (fixture)

def test_events_of_a_normal_ai_gate_run_are_all_gates_first_then_all_eligibility():
    events, cb = collect()
    gate_run(on_progress=cb)
    assert events == expected(["completed"] * N, ["completed"] * N)


def test_events_when_the_gate_stage_runs_out_of_quota_on_the_third_job():
    events, cb = collect()
    gate_run({"sample-3": LLMQuotaExceeded("일일 한도")}, on_progress=cb)
    assert events == expected(["completed", "completed", "failed", "not_run", "not_run", "not_run"],
                              ["not_run", "not_run", "failed", "not_run", "not_run", "not_run"])  # 자격요건은 한 공고도 시작하지 않았다


def test_events_when_the_eligibility_stage_runs_out_of_quota_on_the_fourth_job():
    events, cb = collect()
    mock_e, mock_j = clients()
    gate_run(evidence=Wrap(mock_j, {("find_evidence", "sample-4"): LLMQuotaExceeded("x")}), extract=mock_e, on_progress=cb)
    assert events == expected(["completed"] * N, ["completed"] * 3 + ["failed", "not_run", "not_run"])


def test_events_with_a_gate_input_error_and_a_gate_call_failure():
    events, cb = collect()
    jobs, _ = jobs_with({"sample-1": LLMStageError("x")})
    jobs[1] = ScenarioJob(job_id="sample-2", posting_text=jobs[1].posting_text, gate_posting={"job_title": 5}, gate_judge=Judge(client=GATE_CLIENT))
    mock_e, mock_j = clients()
    run_pipeline(jobs, PROFILE, REF, mock_e, mock_j, desired_family=DESIRED, gate_client=GATE_CLIENT, now=NOW, on_progress=cb)
    assert events == expected(["failed", "failed"] + ["completed"] * 4, ["failed", "failed"] + ["completed"] * 4)


def test_an_injected_gate_run_has_no_gate_events_at_all():
    events, cb = collect()
    run(on_progress=cb)
    assert events == expected(None, ["completed"] * N)


def test_the_eligibility_events_report_a_failed_job_and_an_extraction_failure_as_completed():
    events, cb = collect()
    mock_e, mock_j = clients()
    run(extract=mock_e, evidence=Wrap(mock_j, {("find_evidence", "sample-2"): LLMStageError("x")}), on_progress=cb)
    assert events == expected(None, ["completed", "failed"] + ["completed"] * 4)
    events.clear()
    run(extract=Wrap(mock_e, {("extract_requirements", "sample-2"): LLMStageError("x")}), evidence=mock_j, on_progress=cb)
    assert events == expected(None, ["completed"] * N)  # 요건 추출 실패는 조건부 결과로 끝난 평가다(data-contract 3-3)


# ---------------------------------------------------------------- 알림의 계약

def test_the_result_is_the_same_with_and_without_the_callback():
    _, cb = collect()
    for kw in ({}, {"errors": {"sample-3": LLMStageError("x")}}):
        with_cb = gate_run(kw.get("errors"), on_progress=cb, run_id="20261104T061500Z-abcdef")[0]
        without = gate_run(kw.get("errors"), run_id="20261104T061500Z-abcdef")[0]
        assert with_cb.model_dump() == without.model_dump()
    assert run(on_progress=cb, run_id="20261104T061500Z-abcdef").model_dump() == run(run_id="20261104T061500Z-abcdef").model_dump()


def test_a_callback_cannot_be_called_before_the_job_stage_is_done():
    jobs, judges = jobs_with()
    seen = []
    mock_e, mock_j = clients()
    run_pipeline(jobs, PROFILE, REF, mock_e, mock_j, desired_family=DESIRED, gate_client=GATE_CLIENT, now=NOW,
                 on_progress=lambda stage, done, total, job_id, status: seen.append((stage, job_id, judges[job_id].calls)))
    assert all(calls > 0 for stage, _, calls in seen if stage == "gate")  # 알림은 게이트 호출이 끝난 뒤다


def test_exceptions_from_the_callback_are_not_swallowed():
    def boom(*_):
        raise TypeError("콜백의 결함")

    with pytest.raises(TypeError, match="콜백의 결함"):
        gate_run(on_progress=boom)
    with pytest.raises(TypeError, match="콜백의 결함"):
        run(on_progress=boom)


def test_events_carry_only_the_stage_the_counts_the_job_id_and_the_status():
    events, cb = collect()
    gate_run(on_progress=cb)
    ids = {s["job_id"] for s in SAMPLES}
    for stage, done, total, job_id, status in events:
        assert stage in ("gate", "eligibility") and status in ("completed", "failed", "not_run")
        assert job_id in ids and isinstance(done, int) and total == N and 1 <= done <= N
    assert len({type(v) for event in events for v in event}) == 2  # 문자열과 정수뿐이다


def test_a_run_without_the_callback_is_unchanged():
    assert gate_run()[0].model_dump(exclude={"run_id"}) == gate_run(on_progress=None)[0].model_dump(exclude={"run_id"})
    assert NEEDS_AI  # 게이트 AI 자리를 부르는 입력이 쓰였다
