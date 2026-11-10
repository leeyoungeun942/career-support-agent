"""tools/run_presentation.py --ai: JUDGE / WRITER 모델 분리, 모델별 호출 예산, 실패 격리 (가짜 클라이언트, 네트워크 없음; 이슈 #24)."""
import json

import pytest

from agents.application_writer import STAGE
from core.job_gate_ai import STAGE as GATE_STAGE
from core.llm_client import MAX_RETRIES, LLMQuotaExceeded, LLMResponse, LLMStageError
from core.llm_mock import synthetic_posting
from pipeline_helpers import SAMPLES, Routing
from test_run_demo import write_gate_cases
from tools import demo_support as ds
from tools import run_presentation as rp

WRITER_ANSWERS = {k: v for k, v in json.loads(rp.WRITER_MOCK_JSON.read_text(encoding="utf-8")).items() if not k.startswith("_")}
LINKS = {k: v for k, v in json.loads(rp.TASK_LINKS_JSON.read_text(encoding="utf-8")).items() if not k.startswith("_")}
KIND_OF_STAGE = {v: k for k, v in STAGE.items()}
BASE_ENV = {"GEMINI_API_KEY": "k", "GEMINI_MODEL": "m-base", "GEMINI_RPM": "15", "GEMINI_RPD": "500"}
CONSENT = ["--ai", "--accept-free-tier-terms"]


def make_fake(errors=None, left=None):
    """GeminiClient 대신 쓰는 가짜. 호출은 log에 (모델, 단계, job_id)로 쌓고, 예산 점검은 budgets에 (모델, 필요한 호출)로 쌓는다.

    errors: {(단계 또는 job_id 접두어): 예외}. 게이트 단계는 job_id가 'sample-N:item:...'이라 접두어로 맞춘다. left: {모델: 오늘 남은 호출 수}.
    """
    log, budgets = [], []

    class Fake(Routing):
        def __init__(self, settings, usage=None):
            super().__init__(settings, usage)
            self.usage = usage

        def ensure_budget(self, needed):
            budgets.append((self.model, needed))
            if left is not None and needed > left.get(self.model, 10**9):
                raise LLMQuotaExceeded(f"{self.model}: 오늘 남은 호출 {left[self.model]}회, 필요한 호출 {needed}회")

        def _complete(self, request):
            log.append((self.model, request.stage, request.job_id))
            for key, error in (errors or {}).items():
                if request.stage == key or (request.stage == GATE_STAGE and request.job_id.startswith(key)):
                    self.usage.record(request, model=self.model, ok=False, error=type(error).__name__)
                    raise error
            if request.stage == GATE_STAGE:
                data = {"label": "common", "reason": "테스트 이유 SECRET-REASON"}
            elif request.stage in KIND_OF_STAGE:
                data = WRITER_ANSWERS[request.job_id][KIND_OF_STAGE[request.stage]]
            elif request.stage == "link_tasks":
                data = {"links": LINKS[request.job_id]["links"]}
            else:
                data = super()._complete(request).data
            self.usage.record(request, model=self.model, input_tokens=7, output_tokens=3, model_actual="actual-" + self.model)
            return LLMResponse(data=data, input_tokens=7, output_tokens=3)

    return Fake, log, budgets


@pytest.fixture
def env(tmp_path, monkeypatch):
    """--ai 실행 준비: 합성 공고 원문, 합성 게이트 입력, 가짜 설정, 결과 폴더. 환경 사전을 돌려준다(테스트가 역할 모델을 더한다)."""
    monkeypatch.setattr(rp, "RESULTS_DIR", tmp_path / "results")
    postings = tmp_path / "postings.json"
    postings.write_text(json.dumps({"postings": {s["job_id"]: {"text": synthetic_posting(s) + f"\n채용공고명: 직무{s['number']}"} for s in SAMPLES}}, ensure_ascii=False),
                        encoding="utf-8")
    gate_cases = write_gate_cases(tmp_path / "gate_cases.json")
    monkeypatch.setattr(rp, "CANONICAL_INPUT_SHA256", {"postings": rp.input_digest(postings), "gate_cases": rp.input_digest(gate_cases)})  # 합성 입력을 "고정 발표 입력"으로 본다
    settings = dict(BASE_ENV)
    monkeypatch.setattr(rp, "load_env", lambda: dict(settings))
    return type("Env", (), {"settings": settings, "postings": postings, "gate_cases": gate_cases, "results": tmp_path / "results"})()


def run(env, *extra, fake=None):
    """rp.main을 실행해 종료 코드를 돌려준다."""
    argv = [*CONSENT, "--postings", str(env.postings), "--gate-cases", str(env.gate_cases), *extra]
    with pytest.raises(SystemExit) as e:
        rp.main(argv)
    return e.value.code


def usage_rows(env):
    return [json.loads(line) for line in (env.results / "llm_usage.jsonl").read_text(encoding="utf-8").splitlines()]


def budget(env, writer_extra=0):
    args = type("A", (), {"gate_cases": str(env.gate_cases)})()
    logical, gate_attempts, elig_attempts, judge_calls = ds.gate_ai_budget(args, 6)
    return judge_calls, (2 + writer_extra) * (1 + MAX_RETRIES)


# ---------------------------------------------------------------- 아무것도 보내기 전의 거부

def test_no_consent_sends_nothing(env, monkeypatch, capsys):
    monkeypatch.setattr(rp, "GeminiClient", lambda *a, **k: pytest.fail("동의 없이 클라이언트를 만들었다"))
    with pytest.raises(SystemExit) as e:
        rp.main(["--ai", "--postings", str(env.postings), "--gate-cases", str(env.gate_cases)])
    assert e.value.code == 2 and "--accept-free-tier-terms" in capsys.readouterr().err


def test_missing_gate_input_file_stops_before_the_notice_and_any_client(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rp, "GeminiClient", lambda *a, **k: pytest.fail("입력 파일이 없는데 클라이언트를 만들었다"))
    with pytest.raises(SystemExit) as e:
        rp.main([*CONSENT, "--postings", str(env.postings), "--gate-cases", str(tmp_path / "none.json")])
    cap = capsys.readouterr()
    assert e.value.code == 2 and "샘플 공고 원문 파일이 없다" in cap.err and "무료 티어" not in cap.out


def test_missing_postings_file_gives_guidance_before_any_client(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rp, "GeminiClient", lambda *a, **k: pytest.fail("공고 원문이 없는데 클라이언트를 만들었다"))
    with pytest.raises(SystemExit) as e:
        rp.main([*CONSENT, "--postings", str(tmp_path / "none.json"), "--gate-cases", str(env.gate_cases)])
    assert e.value.code == 2 and "공고 원문 파일이 없다" in capsys.readouterr().err


# ---------------------------------------------------------------- 같은 모델: 합산 예산, 끝까지 실행

def test_one_model_for_both_roles_adds_the_budgets_into_one_check_and_runs_to_the_end(env, monkeypatch, capsys):
    Fake, log, budgets = make_fake()
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    judge_calls, writer_calls = budget(env)
    assert run(env) == 0
    assert budgets == [("m-base", judge_calls + writer_calls)]  # 같은 모델이면 합쳐서 한 번 점검한다
    out = capsys.readouterr().out
    assert "Full AI Demo" in out and "(두 역할이 같은 모델)" in out and "[6] 자기소개서: 완료" in out and "[7] 포트폴리오: 완료" in out
    stages = [stage for _, stage, _ in log]
    assert stages.count("write_cover_letter") == stages.count("write_portfolio") == 1 and "link_tasks" not in stages  # 연결 후보는 고정본이다
    assert {m for m, _, _ in log} == {"m-base"}
    (saved,) = list((env.results / "pipeline").glob("*.json"))
    info = ds.load_run(saved).run_info
    assert (info.gate_mode, info.gate_model_id, info.eligibility_mode, info.model_id) == ("ai", "m-base", "ai", "m-base")
    assert sorted(p.name for p in (env.results / "drafts").glob("*.md")) == sorted(
        f"{ds.load_run(saved).run_id}-sample-3-{kind}.md" for kind in rp.KINDS)  # 초안 파일명이 저장본의 run_id를 쓴다


def test_usage_rows_share_the_demo_run_id_and_split_by_task(env, monkeypatch, capsys):
    Fake, log, budgets = make_fake()
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    assert run(env) == 0
    rows = usage_rows(env)
    out = capsys.readouterr().out
    demo_ids = {r["run_id"] for r in rows}
    assert len(demo_ids) == 1 and next(iter(demo_ids)).startswith("demo-") and f"demo_run_id: {next(iter(demo_ids))}" in out  # 모든 단계가 한 ID로 묶인다
    by_task = {}
    for r in rows:
        by_task.setdefault(r["task"], set()).add(r["stage"])
    assert by_task == {"gate": {"gate_label"}, "eligibility": {"extract_requirements", "find_evidence"}, "writer": {"write_cover_letter", "write_portfolio"}}
    assert all(r["model"] == "m-base" and r["model_actual"] == "actual-m-base" for r in rows if not r["cached"])
    assert "실제 actual-m-base" in out


def test_one_attempt_short_of_the_combined_budget_stops_before_any_call(env, monkeypatch, capsys):
    judge_calls, writer_calls = budget(env)
    Fake, log, budgets = make_fake(left={"m-base": judge_calls + writer_calls - 1})
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    assert run(env) == 2
    assert "시작하지 않았다" in capsys.readouterr().err and log == [] and not (env.results / "pipeline").exists()


def test_the_start_is_allowed_when_the_remaining_budget_equals_the_total(env, monkeypatch):
    judge_calls, writer_calls = budget(env)
    Fake, log, budgets = make_fake(left={"m-base": judge_calls + writer_calls})
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    assert run(env) == 0


# ---------------------------------------------------------------- 다른 모델: 역할별 요청, 따로 점검, fallback 없음

def two_models(env):
    env.settings.update({"GEMINI_MODEL_JUDGE": "m-judge", "GEMINI_MODEL_WRITER": "m-writer"})


def test_judge_requests_go_to_the_judge_model_and_writer_requests_to_the_writer_model(env, monkeypatch, capsys):
    two_models(env)
    Fake, log, budgets = make_fake()
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    judge_calls, writer_calls = budget(env)
    assert run(env) == 0
    assert sorted(budgets) == [("m-judge", judge_calls), ("m-writer", writer_calls)]  # 모델별로 따로 점검한다
    by_model = {}
    for model, stage, _ in log:
        by_model.setdefault(model, set()).add(stage)
    assert by_model == {"m-judge": {GATE_STAGE, "extract_requirements", "find_evidence"}, "m-writer": {"write_cover_letter", "write_portfolio"}}
    rows = usage_rows(env)
    assert {(r["task"], r["model"]) for r in rows if not r["cached"]} == {("gate", "m-judge"), ("eligibility", "m-judge"), ("writer", "m-writer")}
    out = capsys.readouterr().out
    assert "JUDGE(판정 모델): m-judge" in out and "WRITER(생성 모델): m-writer" in out and "(두 역할이 같은 모델)" not in out
    assert "오늘 누적" in out and "(모델 m-judge)" in out and "(모델 m-writer)" in out
    (saved,) = list((env.results / "pipeline").glob("*.json"))
    assert ds.load_run(saved).run_info.model_id == "m-judge" == ds.load_run(saved).run_info.gate_model_id  # 게이트·자격요건은 같은 JUDGE 모델


def test_command_line_models_override_the_env_per_role(env, monkeypatch):
    Fake, log, budgets = make_fake()
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    assert run(env, "--model-judge", "cli-judge", "--model-writer", "cli-writer") == 0
    assert {m for m, s, _ in log if s in ("write_cover_letter", "write_portfolio")} == {"cli-writer"}
    assert {m for m, s, _ in log if s == "find_evidence"} == {"cli-judge"}


@pytest.mark.parametrize("left", [{"m-judge": 10, "m-writer": 500}, {"m-judge": 500, "m-writer": 7}, {"m-judge": 0, "m-writer": 0}])
def test_either_model_being_short_stops_the_run_before_any_call(env, monkeypatch, capsys, left):
    two_models(env)
    Fake, log, budgets = make_fake(left=left)
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    assert run(env) == 2
    assert "시작하지 않았다" in capsys.readouterr().err and log == [] and len(budgets) == 2  # 두 모델을 모두 점검했고 호출은 없다


def test_writer_boundary_is_exactly_eight_attempts(env, monkeypatch):
    two_models(env)
    _, writer_calls = budget(env)
    assert writer_calls == 2 * (1 + MAX_RETRIES) == 8
    Fake, log, _ = make_fake(left={"m-writer": 8})
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    assert run(env) == 0


def test_no_automatic_fallback_a_failed_writer_call_is_not_retried_on_the_judge_model(env, monkeypatch, capsys):
    two_models(env)
    Fake, log, _ = make_fake({"write_cover_letter": LLMStageError("통신 실패 SECRET-DETAIL")})
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    assert run(env) == 1
    writes = [(m, s) for m, s, _ in log if s.startswith("write_")]
    assert writes == [("m-writer", "write_cover_letter"), ("m-writer", "write_portfolio")]  # judge 모델로 다시 부르지 않았다
    cap = capsys.readouterr()
    assert "[6] 자기소개서: 실패" in cap.out and "[7] 포트폴리오: 완료" in cap.out and "SECRET-DETAIL" not in cap.out + cap.err


def test_ai_task_links_use_the_writer_model_and_add_one_attempt_budget(env, monkeypatch):
    two_models(env)
    Fake, log, budgets = make_fake()
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    _, writer_calls = budget(env, writer_extra=1)
    assert writer_calls == 3 * (1 + MAX_RETRIES) == 12
    assert run(env, "--task-links", "ai") == 0
    assert ("m-writer", writer_calls) in budgets
    assert [(m, s) for m, s, _ in log if s == "link_tasks"] == [("m-writer", "link_tasks")]


# ---------------------------------------------------------------- 게이트·자격요건 실패가 있을 때

def test_a_gate_failure_of_another_job_does_not_stop_the_demo(env, monkeypatch, capsys):
    Fake, log, _ = make_fake({"sample-2:": LLMStageError("게이트 통신 실패 SECRET-DETAIL")})  # 합성 입력은 요청이 겹쳐 캐시에 걸리므로 항목이 가장 많은 공고로 한다
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    assert run(env) == 1  # 끝까지 갔지만 실패 단계(다른 공고의 gate_failed)가 있다
    cap = capsys.readouterr()
    assert "평가 실패: 게이트 AI 오류" in cap.out and "[sample-2]" in cap.out and "[6] 자기소개서: 완료" in cap.out and "SECRET-DETAIL" not in cap.out + cap.err
    (saved,) = list((env.results / "pipeline").glob("*.json"))
    assert ds.load_run(saved).counts == {"completed": 5, "failed": 1, "not_run": 0}


def test_a_gate_failure_of_sample_3_stops_the_demo_without_calling_the_writer(env, monkeypatch, capsys):
    Fake, log, _ = make_fake({"sample-3:": LLMStageError("x")})
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    assert run(env) == 4
    out = capsys.readouterr().out
    assert "[4] 선택 공고: sample-3 - 공고 선택: 실패 (gate_failed)" in out
    assert not any(s.startswith("write_") for _, s, _ in log)
    assert "sample-3" not in [line for line in out.splitlines() if line.startswith("순위")] and (env.results / "pipeline").exists()  # 순위 결과는 저장돼 있다
    assert not (env.results / "drafts").exists()


def test_gate_stage_quota_stops_everything_after_it_and_exits_3(env, monkeypatch, capsys):
    Fake, log, _ = make_fake({"sample-3:": LLMQuotaExceeded("한도 SECRET-DETAIL")})
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    assert run(env) == 3
    cap = capsys.readouterr()
    assert "태평양" in cap.err and "SECRET-DETAIL" not in cap.out + cap.err
    stages = {s for _, s, _ in log}
    assert not stages & {"extract_requirements", "find_evidence", "write_cover_letter", "write_portfolio"}  # 자격요건·Writer 호출 0회
    (saved,) = list((env.results / "pipeline").glob("*.json"))
    run_ = ds.load_run(saved)
    assert run_.counts["failed"] == 1 and run_.counts["completed"] == 0 and run_.jobs[0].gate is not None  # 앞 공고의 게이트 결과는 남는다


def test_an_eligibility_failure_of_sample_3_stops_the_demo(env, monkeypatch, capsys):
    Fake, log, _ = make_fake({"find_evidence": LLMStageError("x")})  # 모든 공고의 근거 찾기가 실패한다
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    assert run(env) == 4
    assert "[4] 선택 공고: sample-3 - 공고 선택: 실패 (stage_failed)" in capsys.readouterr().out
    assert not any(s.startswith("write_") for _, s, _ in log)


# ---------------------------------------------------------------- 저장 실패, 비노출

def test_a_draft_file_save_failure_is_shown_as_generated_but_not_saved(env, monkeypatch, capsys):
    Fake, log, _ = make_fake()
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    real = rp.save_draft_md

    def flaky(draft, directory):
        if draft.kind == "cover_letter":
            raise OSError("디스크 가득 SECRET-DETAIL")
        return real(draft, directory)

    monkeypatch.setattr(rp, "save_draft_md", flaky)
    assert run(env) == 1
    cap = capsys.readouterr()
    assert "[6] 자기소개서: 생성 성공, 저장 실패 (save_failed)" in cap.out and "[7] 포트폴리오: 완료" in cap.out and "SECRET-DETAIL" not in cap.out + cap.err
    assert [p.name for p in (env.results / "drafts").glob("*.md")] == [p.name for p in (env.results / "drafts").glob("*-portfolio.md")]


def test_screen_and_saved_files_have_no_posting_text_model_output_or_error_text(env, monkeypatch, capsys):
    Fake, log, _ = make_fake({"sample-2:": LLMStageError("SECRET-ERROR")})
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    run(env, "--show-drafts")
    cap = capsys.readouterr()
    files = [p for p in env.results.rglob("*") if p.is_file() and p.name != "llm_usage.jsonl" and "llm_cache" not in p.parts]
    for secret in ("SECRET-REASON", "SECRET-ERROR", "직무1", "포지션1"):
        assert secret not in cap.out + cap.err and all(secret not in p.read_text(encoding="utf-8") for p in files)
    assert "=== 초안:" in cap.out  # --show-drafts는 본문도 낸다


def test_fresh_cache_gives_the_gate_and_extraction_their_own_new_directories(env, monkeypatch):
    Fake, log, _ = make_fake()
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    assert run(env, "--fresh-cache") == 0
    names = sorted(p.name for p in (env.results / "llm_cache").iterdir())
    assert len(names) == 2 and names[1] == names[0] + "-gate" and names[0].startswith("run-demo-")
    assert not (env.results / "llm_cache" / "profile").exists()  # 프로필이 든 요청은 디스크에 남지 않는다


# ---------------------------------------------------------------- 요약 화면 (AI)

def test_the_ai_summary_numbers_are_the_ones_in_the_usage_log(env, monkeypatch, capsys):
    two_models(env)
    Fake, log, _ = make_fake()
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    assert run(env) == 0
    out = capsys.readouterr().out
    rows = [r for r in usage_rows(env)]
    for task, label in rp.TASKS:
        mine = [r for r in rows if r["task"] == task]
        attempts = sum(1 for r in mine if not r["cached"])
        hits = sum(1 for r in mine if r["cached"])
        logical = len({(r["stage"], r["job_id"]) for r in mine})
        tokens_in = sum(r["input_tokens"] for r in mine if not r["cached"])
        line = next(line for line in out.splitlines() if line.strip().startswith(f"{label}: 호출"))
        assert f"논리 {logical}회, 시도 {attempts}회, 캐시 적중 {hits}" in line and f"입력 {tokens_in} /" in line
    assert "모델 요청 m-judge / 실제 actual-m-judge" in out and "모델 요청 m-writer / 실제 actual-m-writer" in out
    attempts_today = {m: sum(1 for r in rows if r["model"] == m and not r["cached"]) for m in ("m-judge", "m-writer")}
    for model, used in attempts_today.items():
        assert f"오늘 누적 {used}/500 (모델 {model})" in out


def test_the_ai_summary_shows_the_sections_in_order_the_state_of_every_step_and_the_ids(env, monkeypatch, capsys):
    Fake, log, _ = make_fake()
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    assert run(env) == 0
    out = capsys.readouterr().out
    out = out[out.index("=== Career Support Agent"):]  # 앞의 진행 줄(정책 8장)은 요약이 아니다
    marks = ["=== Career Support Agent Full AI Demo", "모드: AI", "[1~3]", "[4] 선택 공고: sample-3", "[5] WriterInput", "[6] 자기소개서: 완료", "[7] 포트폴리오: 완료",
             "확인용 md:", "[8] 실행 요약", "게이트·자격요건·순위: 완료", "결과 저장·재검증: 완료", "JUDGE(판정 모델): m-base", "WRITER(생성 모델): m-base", "종료 상태: 코드 0"]
    positions = [out.index(m) for m in marks if m != "확인용 md:"]
    assert positions == sorted(positions) and out.count("확인용 md:") == 2


def test_the_summary_shows_a_stage_failure_of_the_judge_model_by_name_only(env, monkeypatch, capsys):
    Fake, log, _ = make_fake({"sample-2:": LLMStageError("SECRET-DETAIL")})
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    run(env)
    out = capsys.readouterr().out
    assert "게이트 AI 오류" in out and "SECRET-DETAIL" not in out and "시도" in out


# ---------------------------------------------------------------- 고정 입력 대조와 모델 불일치 표시 (PR 리뷰)

def test_other_input_files_are_refused_before_any_notice_client_or_call(env, monkeypatch, capsys, tmp_path):
    Fake, log, _ = make_fake()
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    other = tmp_path / "other_postings.json"
    data = json.loads(env.postings.read_text(encoding="utf-8"))
    data["postings"]["sample-1"]["text"] += " 개인정보가 들어 있을 수 있는 다른 내용"
    other.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for flag, path in (("--postings", other), ("--gate-cases", other), ("--postings", tmp_path / "not_json.txt")):
        if path.name == "not_json.txt":
            path.write_text("not json", encoding="utf-8")
        argv = [*CONSENT, "--postings", str(env.postings), "--gate-cases", str(env.gate_cases)]
        argv[argv.index(flag) + 1] = str(path)
        with pytest.raises(SystemExit) as e:
            rp.main(argv)
        assert e.value.code == 2
    captured = capsys.readouterr()
    assert "고정된 발표 입력과 내용이 다르다" in captured.err and "무료 티어" not in captured.out
    assert log == [] and not (env.results / "llm_usage.jsonl").exists()  # 클라이언트를 만들지도, 기록을 남기지도 않았다


def test_the_same_content_in_a_reformatted_file_at_another_path_is_accepted(env, monkeypatch, tmp_path):
    Fake, log, _ = make_fake()
    monkeypatch.setattr(rp, "GeminiClient", Fake)
    copy = tmp_path / "copy.json"
    copy.write_text(json.dumps(json.loads(env.postings.read_text(encoding="utf-8")), ensure_ascii=False, indent=4), encoding="utf-8")
    env.postings = copy
    assert run(env) == 0


def test_the_pinned_digests_match_the_local_sample_files_when_they_exist():
    if not (rp.ev.POSTINGS_JSON.exists() and rp.GATE_CASES_JSON.exists()):
        pytest.skip("로컬에 발표 입력 파일이 없다(커밋하지 않는 파일)")
    assert rp.input_digest(rp.ev.POSTINGS_JSON) == rp.CANONICAL_INPUT_SHA256["postings"]
    assert rp.input_digest(rp.GATE_CASES_JSON) == rp.CANONICAL_INPUT_SHA256["gate_cases"]


def test_a_different_actual_model_is_marked_as_a_mismatch_and_an_unknown_one_is_not():
    differ = rp.PresentationResult("ai", "demo-x", {n: rp.Step(n) for n in rp.STEP_NAMES}, models={"judge": "m-a", "writer": "m-a"})
    differ.usage = {"gate": rp.TaskUsage(1, 1, 0, 1, 1, ["m-a"], ["m-b"]), "eligibility": rp.TaskUsage(1, 1, 0, 1, 1, ["m-a"], ["m-a"]),
                    "writer": rp.TaskUsage(1, 1, 0, 1, 1, ["m-a"], [])}
    lines = [l for l in rp.render_result(differ) if "모델 요청" in l]
    gate = next(l for l in lines if l.strip().startswith("직무 게이트"))
    assert "모델 요청 m-a / 실제 m-b (불일치)" in gate
    assert not any("불일치" in l for l in lines if l is not gate)
    assert any("실제 알 수 없음)" in l for l in lines)
