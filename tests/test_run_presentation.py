"""tools/run_presentation.py의 mock dry-run (정책 presentation.md 3·4·6장; 이슈 #24). 네트워크를 쓰지 않는다."""
import dataclasses
import re
from datetime import datetime, timezone

import pytest

from agents.application_writer import DraftError
from core.llm_client import LLMQuotaExceeded, LLMStageError
from core.schemas import DISPLAY_LABELS
from pipeline_helpers import PROFILE, Wrap
from tools import demo_support as ds
from tools import run_demo as demo
from tools import run_presentation as rp

KINDS = ("cover_letter", "portfolio")
READ_LINE = rp.read_line  # conftest의 autouse가 바꾸기 전의 원래 함수


@pytest.fixture
def rdir(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "RESULTS_DIR", tmp_path / "results")
    return tmp_path / "results"


def plan_for(*argv):
    return rp.build_mock_plan(rp.build_parser().parse_args(list(argv)))


def main(*argv):
    with pytest.raises(SystemExit) as e:
        rp.main(list(argv))
    return e.value.code


def statuses(result):
    return {n: s.status for n, s in result.steps.items()}


# ---------------------------------------------------------------- 끝까지 연결

def test_the_whole_flow_runs_with_mock_and_exits_0(rdir, capsys):
    assert main() == 0
    out = capsys.readouterr().out
    assert "mock dry-run" in out and "demo_run_id: demo-" in out and "pipeline_run_id: " in out
    positions = [out.index(f"[{i}]") for i in rp.EXPECTED_RANKING]
    assert positions == sorted(positions)  # 순위 3,1,4,5,2,6
    assert "[4] 선택 공고: sample-3" in out and "[6] 자기소개서: 완료" in out and "[7] 포트폴리오: 완료" in out
    assert "참고: 정책 14장 기대 순위와 같음" in out and "모든 공고에 자기소개서를 만들지 않는다" in out
    (saved,) = list((rdir / "pipeline").glob("*.json"))
    assert demo.load_run(saved).ranking == rp.EXPECTED_RANKING


def test_execute_returns_both_drafts_and_every_step_completed(rdir):
    result = rp.execute(plan_for())
    assert statuses(result) == {n: "completed" for n in rp.STEP_NAMES} and result.exit_code == 0
    assert set(result.drafts) == set(KINDS) and all(d.job_id == "sample-3" and d.meta.mode == "mock" for d in result.drafts.values())
    assert result.fact_counts and result.fact_counts["usable_facts"] > 0 and result.fact_ids["usable_facts"][0] == "F1"


def test_the_shared_helpers_are_the_same_objects_in_both_tools():
    for name in ("build_scenario", "ScenarioError", "save_run", "load_run", "render", "gate_ai_budget", "save_draft_md", "render_draft", "KIND_LABEL"):
        assert getattr(demo, name) is getattr(ds, name)
    assert demo._job_info is ds.job_info
    assert "run_demo" not in open(rp.__file__, encoding="utf-8").read().split('"""')[2]  # run_presentation은 run_demo를 가져오지 않는다


# ---------------------------------------------------------------- 메모리 원본과 저장본, WriterInput

def test_sample_3_is_selected_from_the_memory_original_not_the_saved_copy(rdir):
    result = rp.execute(plan_for())
    assert result.pipeline_run.profile_evidence_included is True  # 메모리 원본은 프로필 인용을 갖고 있다
    assert result.saved_run.profile_evidence_included is False  # 저장본은 인용이 없다
    assert result.saved_run == demo.load_run(result.saved_path)
    # 저장본으로는 WriterInput의 사실을 만들 수 없다는 것도 확인한다
    from core.selection import select_job
    from core.writer_input import WriterInputError, build_writer_input

    stripped = select_job(result.saved_run, "sample-3", include_profile_evidence=False)
    with pytest.raises(WriterInputError):
        build_writer_input(stripped, PROFILE, ds.job_info("sample-3"))


def test_writer_input_and_profile_text_are_never_written_to_disk(rdir, capsys):
    main()
    out = capsys.readouterr().out
    files = [p for p in rdir.rglob("*") if p.is_file()]
    assert files and not any("writer" in p.name.lower() and "input" in p.name.lower() for p in files)
    pieces = [a for e in PROFILE.experiences for a in e.actions] + [e.title for e in PROFILE.experiences]
    assert len(pieces) > 5
    for text in (out, *(p.read_text(encoding="utf-8") for p in files)):
        assert all(piece not in text for piece in pieces)  # 프로필 인용·경험 원문이 화면·결과 파일에 없다
    assert not (rdir / "drafts").exists()  # mock은 초안 파일을 만들지 않는다


def test_no_network_client_is_created_without_ai(rdir, monkeypatch):
    monkeypatch.setattr(rp, "GeminiClient", lambda *a, **k: pytest.fail("--ai 없이 클라이언트를 만들었다"))
    plan = plan_for()
    assert plan.mode == "mock" and not plan.external and plan.usage is None and plan.gate_client is None
    assert main() == 0


# ---------------------------------------------------------------- 자소서 ↔ 포트폴리오 독립

def writer_calls(plan):
    return [k for k in plan.writer_client.calls]


def test_each_draft_is_requested_exactly_once(rdir):
    plan = plan_for()
    plan.writer_client = Wrap(plan.writer_client)
    result = rp.execute(plan)
    assert plan.writer_client.calls == [("write_cover_letter", "sample-3"), ("write_portfolio", "sample-3")] and result.exit_code == 0


@pytest.mark.parametrize("error,code", [
    (LLMStageError("통신 실패 SECRET-DETAIL"), "stage_failed"),
    (DraftError("unsupported_token", "SECRET-DETAIL"), "unsupported_token"),
])
def test_a_cover_letter_failure_does_not_remove_the_portfolio_or_the_ranking(rdir, capsys, error, code):
    plan = plan_for()
    plan.writer_client = Wrap(plan.writer_client, {("write_cover_letter", "sample-3"): error})
    result = rp.execute(plan)
    assert (result.steps["cover_letter"].status, result.steps["cover_letter"].code) == ("failed", code)
    assert result.steps["portfolio"].status == "completed" and set(result.drafts) == {"portfolio"}
    assert result.saved_run.ranking == rp.EXPECTED_RANKING and result.exit_code == 1
    text = "\n".join(rp.render_result(result))
    assert "SECRET-DETAIL" not in text and "[6] 자기소개서: 실패" in text and "[7] 포트폴리오: 완료" in text


def test_a_portfolio_failure_keeps_the_ranking_and_the_cover_letter(rdir):
    plan = plan_for()
    plan.writer_client = Wrap(plan.writer_client, {("write_portfolio", "sample-3"): DraftError("missing_evidence")})
    result = rp.execute(plan)
    assert result.steps["cover_letter"].status == "completed" and result.steps["portfolio"].status == "failed"
    assert set(result.drafts) == {"cover_letter"} and result.saved_run.ranking == rp.EXPECTED_RANKING and result.exit_code == 1
    assert list((rdir / "pipeline").glob("*.json"))  # 저장된 결과는 그대로다


def test_quota_exhaustion_in_the_cover_letter_leaves_the_portfolio_not_run(rdir, capsys):
    plan = plan_for()
    plan.writer_client = Wrap(plan.writer_client, {("write_cover_letter", "sample-3"): LLMQuotaExceeded("SECRET-DETAIL")})
    result = rp.execute(plan)
    assert (result.steps["cover_letter"].status, result.steps["cover_letter"].code, result.steps["portfolio"].status) == ("failed", "quota_exhausted", "not_run")
    assert plan.writer_client.calls == [("write_cover_letter", "sample-3")] and result.exit_code == 3
    assert main() == 0  # 정상 실행은 그대로
    capsys.readouterr()


# ---------------------------------------------------------------- sample-3을 선택할 수 없을 때: 대체하지 않는다

@pytest.mark.parametrize("error,code,exit_code", [
    (LLMStageError("근거 단계 실패"), "no_candidate", 4),
    (LLMQuotaExceeded("한도 소진"), "no_candidate", 3),  # 한도 때문에 평가가 멈춰 후보가 없으면 종료 코드는 한도 소진(3)이다
])
def test_when_sample_3_cannot_be_selected_nothing_else_is_substituted(rdir, error, code, exit_code):
    plan = plan_for()
    plan.evidence_client = Wrap(plan.evidence_client, {("find_evidence", "sample-3"): error})
    plan.writer_client = Wrap(plan.writer_client)
    result = rp.execute(plan)
    assert (result.steps["selection"].status, result.steps["selection"].code) == ("failed", code)
    assert [result.steps[n].status for n in ("writer_input", "cover_letter", "portfolio")] == ["not_run"] * 3
    assert plan.writer_client.calls == [] and result.drafts == {}  # 다른 공고로 대체하지 않고 Writer를 부르지 않는다
    assert result.exit_code == exit_code
    assert result.saved_run is not None and "sample-3" not in result.saved_run.ranking  # 저장된 순위는 남는다
    if exit_code == 4:
        assert result.saved_run.ranking and result.saved_run.ranking[0] != "sample-3"


def test_the_screen_names_the_reason_when_the_selection_failed(rdir):
    plan = plan_for()
    plan.evidence_client = Wrap(plan.evidence_client, {("find_evidence", "sample-3"): LLMStageError("x")})
    text = "\n".join(rp.render_result(rp.execute(plan)))
    assert "[4] 선택 공고: - - 공고 선택: 실패 (no_candidate)" in text and "[6] 자기소개서: 안 함" in text


# ---------------------------------------------------------------- 저장·재로딩 검증이 Writer의 선행 조건

def test_a_save_failure_stops_before_any_writer_call(rdir, monkeypatch):
    def broken_save(*a, **k):
        raise OSError("디스크 가득 SECRET-DETAIL")

    monkeypatch.setattr(rp, "save_run", broken_save)
    plan = plan_for()
    plan.writer_client = Wrap(plan.writer_client)
    result = rp.execute(plan)
    assert (result.steps["persistence"].status, result.steps["persistence"].code) == ("failed", "persistence_failed")
    assert [result.steps[n].status for n in ("selection", "writer_input", "cover_letter", "portfolio")] == ["not_run"] * 4
    assert plan.writer_client.calls == [] and result.exit_code == 4 and result.pipeline_run_id is None
    text = "\n".join(rp.render_result(result))
    assert "SECRET-DETAIL" not in text and "1위 [sample-3]" in text  # 순위 화면은 보인다


def test_a_reload_mismatch_stops_before_any_writer_call(rdir, monkeypatch):
    real = rp.load_run
    monkeypatch.setattr(rp, "load_run", lambda path: real(path).model_copy(update={"created_at": datetime(2000, 1, 1, tzinfo=timezone.utc)}))
    plan = plan_for()
    plan.writer_client = Wrap(plan.writer_client)
    result = rp.execute(plan)
    assert (result.steps["persistence"].status, result.steps["persistence"].code) == ("failed", "reload_mismatch")
    assert plan.writer_client.calls == [] and result.drafts == {} and result.exit_code == 4


def test_an_unreadable_saved_file_is_a_persistence_failure(rdir, monkeypatch):
    def unreadable(path):
        raise ValueError("형식이 틀림 SECRET-DETAIL")

    monkeypatch.setattr(rp, "load_run", unreadable)
    result = rp.execute(plan_for())
    assert result.steps["persistence"].code == "persistence_failed" and result.exit_code == 4


def test_a_validation_defect_while_saving_is_not_hidden_as_a_persistence_failure(rdir, monkeypatch):
    def defective(*a, **k):
        raise ValueError("검증 결함")

    monkeypatch.setattr(rp, "save_run", defective)
    with pytest.raises(ValueError, match="검증 결함"):
        rp.execute(plan_for())


# ---------------------------------------------------------------- run_id: 저장본이 공식 ID

def test_when_the_file_name_is_taken_the_saved_run_id_is_the_official_one_everywhere(rdir, monkeypatch):
    taken, fresh = "20261107T000000Z-aaaaaa", "20261107T000000Z-bbbbbb"
    monkeypatch.setattr("core.pipeline.make_run_id", lambda now=None: taken)
    monkeypatch.setattr(ds, "make_run_id", lambda now=None: fresh)
    (rdir / "pipeline").mkdir(parents=True)
    (rdir / "pipeline" / f"{taken}.json").write_text("{}", encoding="utf-8")  # 같은 이름의 파일이 이미 있다
    result = rp.execute(plan_for())
    assert result.pipeline_run_id == fresh and result.saved_path.name == f"{fresh}.json"
    assert result.pipeline_run.run_id == fresh  # 메모리 원본도 저장본 값으로 맞췄다
    assert all(d.run_id == fresh for d in result.drafts.values())  # 선택 결과와 WriterInput 메타를 거친 초안의 ID
    assert result.demo_run_id.startswith("demo-") and result.demo_run_id != fresh
    assert (rdir / "pipeline" / f"{taken}.json").read_text(encoding="utf-8") == "{}"  # 남의 파일을 덮어쓰지 않았다
    assert fresh in "\n".join(rp.render_result(result))


# ---------------------------------------------------------------- 명령줄

def test_flags_that_are_refused(rdir, capsys):
    assert main("--real-profile") == 2 and "구현하지 않았다" in capsys.readouterr().err
    assert main("--task-links", "ai") == 2 and "--ai와 함께" in capsys.readouterr().err
    assert main("--scenario", "other") == 2
    assert not (rdir / "pipeline").exists()


def test_quiet_prints_states_and_ranking_only(rdir, capsys):
    assert main("-q") == 0
    out = capsys.readouterr().out
    assert "순위: " in out and "persistence completed -" in out and "종료 코드 0" in out and "WriterInput" not in out


# ---------------------------------------------------------------- 요약 화면 (정책 presentation.md 8장)

SECTION_MARKS = ["=== Career Support Agent", "프로필: kim-haneul", "demo_run_id:", "[1~3]", "[4] 선택 공고", "[5] WriterInput", "[6] 자기소개서", "[7] 포트폴리오", "[8] 실행 요약",
                 "이 시스템은 모든 공고에 자기소개서를 만들지 않는다"]


def test_the_summary_sections_come_in_the_policy_order(rdir, capsys):
    main()
    out = capsys.readouterr().out
    out = out[out.index("=== Career Support Agent"):]  # 앞의 진행 줄(정책 8장)은 요약이 아니다
    positions = [out.index(mark) for mark in SECTION_MARKS]
    assert positions == sorted(positions)


def test_the_mock_summary_says_that_no_ai_was_called(rdir, capsys):
    main()
    out = capsys.readouterr().out
    assert "모드: mock" in out and "AI 호출 없음 (mock" in out and "오늘 누적" not in out and "JUDGE" not in out


def test_the_safety_classification_shows_counts_and_ids_but_not_the_sentences(rdir, capsys):
    from core.selection import select_job
    from core.writer_input import build_writer_input

    result = rp.execute(plan_for())
    selected = select_job(result.pipeline_run, "sample-3", include_profile_evidence=True)
    wi = build_writer_input(selected, PROFILE, ds.job_info("sample-3"))
    text = "\n".join(rp.render_result(result))
    section5 = text[text.index("[5] WriterInput"):text.index("[6] 자기소개서")]  # [1~3]의 사유 문구는 코드가 만든 요건 이름이라 따로 보인다
    assert "사용 가능 사실 3개 ['F1', 'F2', 'F3'] / 확인 필요 1개 ['C1'] / 주장 금지 0개 []" in section5
    sentences = [f.text for f in wi.usable_facts] + [c.text for c in wi.to_confirm] + [c.question for c in wi.to_confirm]
    assert sentences and all(sentence not in section5 for sentence in sentences)  # [5]에는 사실·확인 필요 문장과 질문이 없다


def test_a_draft_failure_shows_its_code_and_a_missing_file_shows_the_state(rdir):
    plan = plan_for()
    plan.writer_client = Wrap(plan.writer_client, {("write_cover_letter", "sample-3"): DraftError("unsupported_token", "SECRET-DETAIL")})
    text = "\n".join(rp.render_result(rp.execute(plan)))
    assert "[6] 자기소개서: 실패 (unsupported_token)" in text and "코드 검증 통과" in text.split("[7]")[1] and "SECRET-DETAIL" not in text


def test_show_drafts_adds_both_draft_bodies_after_the_summary_and_the_default_does_not(rdir, capsys):
    main()
    assert "=== 초안:" not in capsys.readouterr().out
    main("--show-drafts")
    out = capsys.readouterr().out
    assert out.index("=== 초안: 자기소개서") > out.index("[8] 실행 요약") and "=== 초안: 포트폴리오" in out


def test_quiet_summary_lists_every_step_and_the_exit_code(rdir, capsys):
    plan = plan_for()
    plan.writer_client = Wrap(plan.writer_client, {("write_portfolio", "sample-3"): LLMStageError("x")})
    lines = rp.render_result(rp.execute(plan), quiet=True)
    assert [line.split()[0] for line in lines[1:7]] == list(rp.STEP_NAMES)
    assert "portfolio failed stage_failed" in lines and lines[-1] == "종료 코드 1"


# ---------------------------------------------------------------- 결함은 숨기지 않는다

@pytest.mark.parametrize("error", [TypeError("초안 코드의 결함"), KeyError("fact_ids"), AttributeError("x")])
def test_defects_in_the_writer_path_propagate_instead_of_becoming_a_failed_step(rdir, error):
    plan = plan_for()
    plan.writer_client = Wrap(plan.writer_client, {("write_cover_letter", "sample-3"): error})
    with pytest.raises(type(error)):
        rp.execute(plan)


def test_configuration_and_permission_errors_are_not_swallowed(rdir):
    from core.llm_client import LLMConfigError

    for error in (LLMConfigError("키 없음"), PermissionError("허용 없음")):
        plan = plan_for()
        plan.writer_client = Wrap(plan.writer_client, {("write_portfolio", "sample-3"): error})
        with pytest.raises(type(error)):
            rp.execute(plan)


def test_a_defect_in_a_judge_stage_is_not_hidden_as_a_selection_failure(rdir):
    plan = plan_for()
    plan.evidence_client = Wrap(plan.evidence_client, {("find_evidence", "sample-3"): TypeError("판정 코드의 결함")})
    with pytest.raises(TypeError):
        rp.execute(plan)


def test_progress_lines_come_one_per_stage_start_and_end_without_changing_the_result(tmp_path):
    lines = []
    missing = ["--gate-cases", str(tmp_path / "none.json")]  # 게이트 입력 파일이 없으면 주입된 게이트라 공고별 게이트 줄이 없다 (정책 3-1)
    with_progress = rp.execute(plan_for(*missing), lines.append)
    plain = rp.execute(plan_for(*missing))
    assert [l.split(":")[0] for l in lines if " 게이트 " not in l and " 자격요건 " not in l] == [
        "[1~3] 직무 게이트·자격요건·순위", "[1~3] 직무 게이트·자격요건·순위", "[저장] 결과 저장·재검증", "[4] 공고 선택",
        "[5] WriterInput 안전 분류", "[6] 자기소개서", "[6] 자기소개서", "[7] 포트폴리오", "[7] 포트폴리오"]
    assert lines[0].endswith("공고 6건") and "수 분" not in lines[0]  # mock은 기다리지 않는다
    assert {n: s.status for n, s in with_progress.steps.items()} == {n: s.status for n, s in plain.steps.items()}
    assert with_progress.exit_code == plain.exit_code == 0


def test_per_job_progress_lines_show_the_stage_the_count_the_job_and_the_state(tmp_path):
    from test_run_demo import write_gate_cases
    cases = write_gate_cases(tmp_path / "gate_cases.json")
    lines = []
    rp.execute(plan_for("--gate-cases", str(cases)), lines.append)
    per_job = [l for l in lines if l.startswith("[1~3] 게이트 ") or l.startswith("[1~3] 자격요건 ")]
    assert per_job == [f"[1~3] {stage} {n}/6: sample-{n} (완료)" for stage in ("게이트", "자격요건") for n in range(1, 7)]
    lines.clear()
    rp.execute(plan_for("--gate-cases", str(tmp_path / "none.json")), lines.append)
    assert [l for l in lines if l.startswith("[1~3] 게이트 ")] == []  # 주입된 게이트에는 게이트 줄이 없다
    assert len([l for l in lines if l.startswith("[1~3] 자격요건 ")]) == 6


def test_a_failed_and_a_not_run_job_are_shown_with_their_states(tmp_path):
    plan = plan_for("--gate-cases", str(tmp_path / "none.json"))
    plan.evidence_client = Wrap(plan.evidence_client, {("find_evidence", "sample-4"): LLMQuotaExceeded("한도")})
    lines = []
    rp.execute(plan, lines.append)
    assert [l for l in lines if l.startswith("[1~3] 자격요건 ")][3:] == [
        "[1~3] 자격요건 4/6: sample-4 (실패)", "[1~3] 자격요건 5/6: sample-5 (안 함)", "[1~3] 자격요건 6/6: sample-6 (안 함)"]


def test_main_prints_progress_before_the_summary_and_not_in_quiet_mode(capsys):
    with pytest.raises(SystemExit):
        rp.main([])
    out = capsys.readouterr().out
    assert out.index("[6] 자기소개서: 시작") < out.index("=== Career Support Agent")
    with pytest.raises(SystemExit):
        rp.main(["-q"])
    assert "시작" not in capsys.readouterr().out


def test_per_job_progress_lines_hold_only_ids_counts_and_states_and_are_off_with_quiet(tmp_path, capsys):
    from test_run_demo import write_gate_cases
    cases = write_gate_cases(tmp_path / "gate_cases.json")
    lines = []
    rp.execute(plan_for("--gate-cases", str(cases)), lines.append)
    per_job = [l for l in lines if l.startswith("[1~3] 게이트 ") or l.startswith("[1~3] 자격요건 ")]
    assert per_job and all(re.fullmatch(r"\[1~3\] (게이트|자격요건) \d/6: sample-\d \((완료|실패|안 함)\)", l) for l in per_job)
    assert main("--gate-cases", str(cases)) == 0
    out = capsys.readouterr().out
    assert "[1~3] 게이트 1/6" in out and out.index("[1~3] 자격요건 6/6") < out.index("=== Career Support Agent")  # 요약보다 먼저 나온다
    assert main("-q", "--gate-cases", str(cases)) == 0
    assert "게이트 1/6" not in capsys.readouterr().out


# ---------------------------------------------------------------- 사용자 공고 선택 (정책 presentation.md 2-1, 이슈 #28)

def stopped_before_choice(tmp_path, errors=None):
    """게이트는 fixture 결과, 자격요건은 mock으로 평가를 끝낸 실행(선택에서 입력 끝으로 멈춘다). (결과, 계획)을 돌려준다."""
    plan = plan_for("--gate-cases", str(tmp_path / "none.json"))
    if errors:
        plan.evidence_client = Wrap(plan.evidence_client, errors)
    return rp.execute(plan, choose=lambda found, notice: (None, "no_input")), plan


def ai_candidates(result, plan, task_links="none"):
    """실제 AI 실행(연결 후보 고정본이 아님)이었다면 후보가 어떻게 나오는지 본다."""
    return rp.candidates(result.pipeline_run, dataclasses.replace(plan, mode="ai", task_links=task_links))


def test_candidates_are_completed_primary_jobs_in_rank_order_with_the_eligibility_state(tmp_path):
    result, plan = stopped_before_choice(tmp_path)
    found, notice = ai_candidates(result, plan)
    assert notice is None and [c.job_id for c in found] == ["sample-3", "sample-1"]  # 직무 불일치(참고용) 공고는 후보가 아니다
    assert [c.number for c in found] == [3, 1]
    assert re.fullmatch(r"1위 \[sample-3\] 3번 .+ — 조건부 적합", found[0].line)
    assert re.fullmatch(r"2위 \[sample-1\] 1번 .+ — 필수 요건 미충족", found[1].line)  # 자격요건 미충족도 후보이고 상태가 보인다
    ranked_primary = [j.job_id for j in sorted(result.pipeline_run.jobs, key=lambda j: j.rank) if j.gate.ranking_group == "primary"]
    assert [c.job_id for c in found] == ranked_primary


def test_the_state_in_a_candidate_line_comes_from_the_eligibility_result(tmp_path):
    result, plan = stopped_before_choice(tmp_path)
    for c in ai_candidates(result, plan)[0]:
        job = next(j for j in result.pipeline_run.jobs if j.job_id == c.job_id)
        assert c.line.endswith(" — " + DISPLAY_LABELS[job.eligibility.verdict])
    assert len({c.line.rsplit(" — ", 1)[1] for c in ai_candidates(result, plan)[0]}) == 2  # 상태가 공고마다 다르게 나온다


def test_a_job_whose_evaluation_failed_is_not_a_candidate(tmp_path):
    result, plan = stopped_before_choice(tmp_path, {("find_evidence", "sample-1"): LLMStageError("x")})
    assert [c.job_id for c in ai_candidates(result, plan)[0]] == ["sample-3"]


def test_mock_and_the_task_link_fixture_leave_only_sample_3_and_say_why(tmp_path):
    result, plan = stopped_before_choice(tmp_path)
    found, notice = rp.candidates(result.pipeline_run, plan)
    assert [c.job_id for c in found] == ["sample-3"] and "mock은 sample-3 초안만" in notice
    found, notice = ai_candidates(result, plan, task_links="fixture")
    assert [c.job_id for c in found] == ["sample-3"] and "연결 후보 고정본" in notice
    assert ai_candidates(result, plan, task_links="ai")[1] is None and len(ai_candidates(result, plan, task_links="ai")[0]) == 2


def test_no_candidate_when_nothing_in_the_top_group_finished(tmp_path):
    ids = [f"sample-{n}" for n in range(1, 7)]
    result, plan = stopped_before_choice(tmp_path, {("find_evidence", i): LLMStageError("x") for i in ids})
    assert ai_candidates(result, plan) == ([], None)


def test_the_question_is_always_asked_and_sample_3_is_not_chosen_for_the_user(rdir, capsys, typed):
    assert main() == 0
    out = capsys.readouterr().out
    assert "자소서/포트폴리오 초안을 생성할 공고 번호를 입력해주세요." in out and "공고 번호만 입력하세요 (예: 3): " in typed.prompts
    assert "[sample-3] 3번" in out and out.index("입력해주세요") < out.index("[4] 공고 선택: 완료")
    typed.answers = []
    assert main() == 4  # 입력이 없으면 sample-3으로 대신하지 않는다
    out = capsys.readouterr().out
    assert "[4] 선택 공고: - - 공고 선택: 실패 (no_input)" in out and "[6] 자기소개서: 안 함" in out


def test_without_input_nothing_after_the_selection_is_started(rdir, typed):
    plan = plan_for()
    plan.writer_client = Wrap(plan.writer_client)
    typed.answers = []
    result = rp.execute(plan)
    assert (result.steps["selection"].status, result.steps["selection"].code) == ("failed", "no_input") and result.exit_code == 4
    assert plan.writer_client.calls == [] and result.drafts == {} and result.selected_job_id is None
    assert [result.steps[n].status for n in ("writer_input", "cover_letter", "portfolio")] == ["not_run"] * 3
    assert result.saved_run is not None  # 순위 결과는 저장돼 있다


def test_a_number_outside_the_candidates_is_asked_again(rdir, typed):
    typed.answers = ["1", "3"]  # mock의 후보는 sample-3뿐이라 1번은 거절된다
    result = rp.execute(plan_for())
    assert result.selected_job_id == "sample-3" and result.exit_code == 0 and len(typed.prompts) == 2


@pytest.mark.parametrize("answers,expected_prompts", [(["sample-3", "", "x"], 3), (["9", "0", "-3"], 3), (["sample-3", "sample-3", "sample-3", "3"], 3)])
def test_three_wrong_answers_end_with_selection_failed(rdir, typed, capsys, answers, expected_prompts):
    typed.answers = list(answers)
    result = rp.execute(plan_for())
    assert (result.steps["selection"].code, result.exit_code) == ("invalid_input", 4) and len(typed.prompts) == expected_prompts
    assert result.drafts == {} and "남은 횟수 1회" in capsys.readouterr().out


def test_an_id_string_is_refused_and_digits_with_spaces_are_accepted(rdir, typed):
    typed.answers = ["sample-3", " 3 "]
    assert rp.execute(plan_for()).selected_job_id == "sample-3"


def test_with_no_candidate_the_question_is_not_asked_and_the_reason_is_shown(rdir, typed, capsys, tmp_path):
    ids = [f"sample-{n}" for n in range(1, 7)]
    plan = plan_for("--gate-cases", str(tmp_path / "none.json"))
    plan.evidence_client = Wrap(plan.evidence_client, {("find_evidence", i): LLMStageError("x") for i in ids})
    result = rp.execute(plan)
    assert typed.prompts == [] and "상위 직무 매칭 공고가 없습니다." in capsys.readouterr().out
    assert (result.steps["selection"].code, result.exit_code) == ("no_candidate", 4)


def test_the_question_and_the_candidates_are_shown_even_with_quiet(rdir, capsys):
    assert main("-q") == 0
    out = capsys.readouterr().out
    assert "공고 번호를 입력해주세요" in out and "[sample-3] 3번" in out


def test_read_line_returns_none_when_input_ends_or_cannot_be_read(monkeypatch):
    def raising(error):
        def fake_input(prompt=""):
            raise error
        return fake_input

    for error in (EOFError(), OSError("입력창 없음")):
        monkeypatch.setattr("builtins.input", raising(error))
        assert READ_LINE("질문") is None
    monkeypatch.setattr("builtins.input", lambda prompt="": "3")
    assert READ_LINE("질문") == "3"


def test_a_piped_answer_is_echoed_after_the_question_but_a_typed_one_is_not(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "3")
    monkeypatch.setattr(rp.sys.stdin, "isatty", lambda: False, raising=False)
    assert READ_LINE("질문") == "3" and capsys.readouterr().out == "3\n"
    monkeypatch.setattr(rp.sys.stdin, "isatty", lambda: True, raising=False)
    assert READ_LINE("질문") == "3" and capsys.readouterr().out == ""
