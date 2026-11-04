"""시연 시나리오 통합 테스트: 김하늘(가상) + 샘플 6건 (이슈 #7 완료 조건, 정책 eligibility.md 14·17장).

두 테스트를 따로 보고한다 (정책 18장).
- test_pipeline_with_injected_gate: 항상 돈다. 게이트 결과는 fixture 값을 넣는다.
- test_pipeline_with_real_sample_input: 로컬 입력 파일(커밋하지 않음)이 있을 때만 돈다. 실제 게이트 규칙을 mock AI 자리로 돌린다.
둘 다 통과하기 전에는 "전체 통합 테스트 통과"라고 쓰지 않는다. 입력 파일이 없으면 건너뛴 사유가 pytest 출력에 남는다.
"""
import subprocess
from pathlib import Path

import pytest

from core.llm_mock import make_evidence_mock, make_extraction_mock
from core.pipeline import run_pipeline
from tools import eval_eligibility as ev
from tools import run_demo as demo

SAMPLES, PROFILE, EVIDENCE, GATES = ev.load_fixtures()
REPO = Path(__file__).resolve().parents[1]
FIXTURE_ORDER = ["sample-3", "sample-1", "sample-4", "sample-5", "sample-2", "sample-6"]  # 정책 14·17장
REFERENCE_ONLY = {"sample-2", "sample-4", "sample-5", "sample-6"}
BY_ID = {s["job_id"]: s for s in SAMPLES}

needs_gate_input = pytest.mark.skipif(
    not demo.GATE_CASES_JSON.exists(),
    reason="gate input unavailable: data/sample-jobs/gate_sample_cases.json (커밋하지 않는 파일, 팀 Drive에서 받아 넣는다)",
)
needs_both_inputs = pytest.mark.skipif(
    not (demo.GATE_CASES_JSON.exists() and ev.POSTINGS_JSON.exists()),
    reason="sample input unavailable: gate_sample_cases.json 또는 eligibility_sample_postings.json (커밋하지 않는 파일)",
)


def run_scenario(gate_source):
    jobs, profile, desired, actual = demo.build_scenario(gate_source)
    run = run_pipeline(jobs, profile, demo.PROFILE_REF, make_extraction_mock(SAMPLES), make_evidence_mock(SAMPLES, EVIDENCE),
                       desired_family=desired)
    assert actual == gate_source
    return run


def check_issue_completion_conditions(run):
    """이슈 #7 완료 조건과 정책 17장을 검사한다. 기대값은 fixture에서 가져오고 파이프라인 코드에 기대지 않는다."""
    ranking = run.ranking
    assert len(run.jobs) == 6 and sorted(ranking) == sorted(BY_ID)  # 공고를 지우지 않는다
    assert ranking[0] == "sample-3" and run.jobs[2].eligibility.verdict == "conditional"  # 직무 일치, 조건부 적합이 1순위
    assert ranking[1] == "sample-1" and run.jobs[0].eligibility.verdict == "not_eligible"  # 직무 일치, 필수 미충족이 2순위
    assert {j.job_id for j in run.jobs if j.reference_only} == REFERENCE_ONLY == set(ranking[2:])  # 불일치 4건은 참고용으로 아래
    assert ranking == FIXTURE_ORDER
    for job in run.jobs:  # 종합 판정과 결정적 요건이 17장 표와 같다
        sample = BY_ID[job.job_id]
        cmp = ev.compare_sample(sample, job.eligibility.requirements, job.eligibility)
        assert cmp["verdict_ok"] and cmp["decisive_ok"], job.job_id
        assert job.gate.ranking_group == sample["gate_ranking_group"] == job.eligibility.gate_ranking_group


def check_screen_and_saved_file(run, tmp_path):
    lines = demo.render(run)
    screen = "\n".join(lines)
    positions = [screen.index(f"[{i}]") for i in FIXTURE_ORDER]
    assert positions == sorted(positions)
    divider = screen.index(demo.DISPLAY_LABELS["reference_only_notice"])
    assert screen.index("[sample-1]") < divider < screen.index("[sample-4]")
    assert screen.count("[참고용 평가]") == 4
    assert "6개월 < 36개월(3년)" in screen and "수준 확인 불가" in screen  # 1번과 3번의 결정적 사유 (17장 요약표)
    path, saved = demo.save_run(run, tmp_path)
    reloaded = demo.load_run(path)
    assert reloaded == saved and reloaded.ranking == run.ranking == FIXTURE_ORDER
    assert reloaded.profile_evidence_included is False


def test_pipeline_with_injected_gate(tmp_path):
    run = run_scenario("injected")
    assert run.run_info.gate_mode == "injected" and run.counts == {"completed": 6, "failed": 0, "not_run": 0}
    check_issue_completion_conditions(run)
    check_screen_and_saved_file(run, tmp_path)


@needs_gate_input
def test_pipeline_with_real_sample_input(tmp_path):
    run = run_scenario("mock")
    assert run.run_info.gate_mode == "mock" and run.counts == {"completed": 6, "failed": 0, "not_run": 0}
    for job in run.jobs:  # 실제 게이트 규칙이 만든 판정이 기대 판정(job-gate.md 7-1)과 같다
        want = GATES[job.job_id]
        assert (job.gate.result, job.gate.core_family) == (want.result, want.core_family), job.job_id
        assert job.gate.detail is None  # 실행 결과에 공고 업무 항목 원문을 남기지 않는다
    check_issue_completion_conditions(run)
    check_screen_and_saved_file(run, tmp_path)


@needs_both_inputs
def test_real_sample_files_describe_the_same_postings():
    """게이트용 입력 파일과 자격요건용 공고 원문 파일이 같은 공고 6건이다 (계획 D4)."""
    texts = ev.load_postings()
    assert sorted(texts) == sorted(BY_ID)
    jobs, *_ = demo.build_scenario("mock", posting_texts=texts)  # 직무명이 원문에 없으면 ScenarioError
    assert [j.job_id for j in jobs] == [s["job_id"] for s in SAMPLES]


def test_result_files_and_local_inputs_are_not_tracked():
    done = subprocess.run(["git", "ls-files", "data/results", "data/sample-jobs", "data/raw", ".env"], cwd=REPO,
                          capture_output=True, text=True)
    if done.returncode != 0:
        pytest.skip("git 저장소가 아니라 추적 여부를 확인할 수 없다")
    assert set(done.stdout.split()) <= {"data/sample-jobs/.gitkeep", "data/raw/README.md"}
