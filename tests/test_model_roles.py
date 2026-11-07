"""JUDGE / WRITER 모델 역할: 설정, 사용량 기록의 task·model_actual, 모델별 호출 예산 (정책 presentation.md 1·5·6장; 이슈 #24). 네트워크를 쓰지 않는다."""
import json
from argparse import Namespace

import pytest
from pydantic import BaseModel

from core.llm_client import (MAX_RETRIES, GeminiClient, GeminiSettings, LLMConfigError, LLMQuotaExceeded, LLMRequest, UsageLog, task_of)
from tools.ai_common import AiSetupError, prepare_roles


class Out(BaseModel):
    answer: str


def req(stage="extract_requirements", job_id="job1"):
    return LLMRequest(stage=stage, prompt="공고 원문", schema=Out, job_id=job_id, prompt_version="1")


BASE = {"GEMINI_API_KEY": "k", "GEMINI_MODEL": "base-model", "GEMINI_RPM": "15", "GEMINI_RPD": "500"}


# ---------------------------------------------------------------- 설정

def test_roles_fall_back_to_the_single_model_setting():
    for role in ("judge", "writer"):
        s = GeminiSettings.from_env(BASE, role)
        assert (s.model, s.rpm, s.rpd) == ("base-model", 15, 500)


def test_role_specific_settings_win_and_the_other_role_is_not_affected():
    env = {**BASE, "GEMINI_MODEL_JUDGE": "judge-model", "GEMINI_RPM_JUDGE": "5", "GEMINI_RPD_JUDGE": "20", "GEMINI_MODEL_WRITER": "writer-model"}
    judge, writer = GeminiSettings.from_env(env, "judge"), GeminiSettings.from_env(env, "writer")
    assert (judge.model, judge.rpm, judge.rpd) == ("judge-model", 5, 20)
    assert (writer.model, writer.rpm, writer.rpd) == ("writer-model", 15, 500)  # 한도는 역할 변수가 없어 공용 값


def test_no_role_ignores_role_variables_and_behaves_as_before():
    env = {**BASE, "GEMINI_MODEL_JUDGE": "judge-model", "GEMINI_RPM_WRITER": "3"}
    s = GeminiSettings.from_env(env)
    assert (s.model, s.rpm, s.rpd) == ("base-model", 15, 500)
    assert GeminiSettings.from_env({"GEMINI_API_KEY": "k", "GEMINI_MODEL": "m"}).rpm == 5  # 기본값도 그대로


def test_role_settings_need_a_key_and_a_model_and_reject_unknown_roles():
    with pytest.raises(LLMConfigError, match="GEMINI_API_KEY"):
        GeminiSettings.from_env({"GEMINI_MODEL_JUDGE": "m"}, "judge")
    with pytest.raises(LLMConfigError, match="GEMINI_MODEL_WRITER"):
        GeminiSettings.from_env({"GEMINI_API_KEY": "k"}, "writer")
    with pytest.raises(ValueError, match="role"):
        GeminiSettings.from_env(BASE, "small")


def test_role_environment_does_not_put_the_key_into_the_settings_text():
    assert "SECRET-KEY" not in repr(GeminiSettings.from_env({**BASE, "GEMINI_API_KEY": "SECRET-KEY"}, "judge"))


# ---------------------------------------------------------------- 사용량 기록: task, model_actual

@pytest.mark.parametrize("stage,task", [
    ("gate_label", "gate"), ("extract_requirements", "eligibility"), ("find_evidence", "eligibility"),
    ("write_cover_letter", "writer"), ("write_portfolio", "writer"), ("link_tasks", "writer"), ("something_new", "other"),
])
def test_task_follows_the_stage_name(tmp_path, stage, task):
    assert task_of(stage) == task
    log = UsageLog(tmp_path / "u.jsonl", run_id="r1")
    log.record(req(stage), model="m")
    assert json.loads(log.path.read_text(encoding="utf-8"))["task"] == task


def test_usage_rows_record_the_requested_and_the_actual_model_without_content(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl", run_id="r1")
    log.record(req(), model="requested-model", model_actual="actual-model-001")
    log.record(req(job_id="job2"), model="requested-model")  # 실제 모델을 모르면 비운다
    rows = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert (rows[0]["model"], rows[0]["model_actual"]) == ("requested-model", "actual-model-001")
    assert (rows[1]["model"], rows[1]["model_actual"]) == ("requested-model", None)
    assert log.models_used() == {"requested": ["requested-model"], "actual": ["actual-model-001"]}


def test_gemini_client_records_the_model_the_response_reported(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl", run_id="r1")
    outputs = iter([(json.dumps({"answer": "a"}), 10, 2, "served-model"), (json.dumps({"answer": "b"}), 10, 2)])  # 둘째는 3-튜플: 모른다
    client = GeminiClient(GeminiSettings(api_key="k", model="asked-model", rpm=60, rpd=100), log, generate=lambda r: next(outputs),
                          sleep=lambda s: None, clock=lambda: 0.0)
    client.complete_json(req())
    client.complete_json(req(job_id="job2"))
    rows = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert [(r["model"], r["model_actual"]) for r in rows] == [("asked-model", "served-model"), ("asked-model", None)]


def test_old_usage_rows_without_task_are_still_counted_by_task(tmp_path):
    path = tmp_path / "u.jsonl"
    old = {"run_id": "r1", "stage": "gate_label", "job_id": "a", "model": "m", "input_tokens": 5, "output_tokens": 1, "cached": False, "ok": True,
           "date_pt": "2000-01-01"}
    path.write_text(json.dumps(old) + "\n" + json.dumps({**old, "stage": "write_portfolio", "job_id": "b"}) + "\n", encoding="utf-8")
    log = UsageLog(path, run_id="r1")
    assert log.run_totals(task="gate")["calls"] == 1 and log.run_totals(task="writer")["calls"] == 1 and log.logical_calls(task="writer") == 1
    assert log.run_totals()["calls"] == 2 and log.models_used("writer") == {"requested": ["m"], "actual": []}


def test_task_filter_is_separate_from_the_run_and_the_stage_filters(tmp_path):
    path = tmp_path / "u.jsonl"
    log = UsageLog(path, run_id="r1")
    log.record(req("find_evidence"), model="m", input_tokens=3)
    log.record(req("extract_requirements"), model="m", input_tokens=4)
    log.record(req("write_cover_letter"), model="w", input_tokens=9)
    UsageLog(path, run_id="other").record(req("write_cover_letter"), model="w", input_tokens=900)
    assert log.run_totals(task="eligibility")["input_tokens"] == 7 and log.run_totals(task="writer")["input_tokens"] == 9
    assert log.logical_calls(task="eligibility") == 2 and log.run_totals(stage="find_evidence")["input_tokens"] == 3


# ---------------------------------------------------------------- prepare_roles: 모델별 예산, 첫 호출 전 점검

class StubClient:
    """GeminiClient 대신 쓰는 가짜: 한도만 안다. 호출하면 실패한다(점검 단계에서는 호출이 없어야 한다)."""

    external = True
    created = []

    def __init__(self, settings, usage=None):
        self.settings, self.model, self.usage = settings, settings.model, usage
        self.checked = []
        StubClient.created.append(self)

    def ensure_budget(self, needed):
        self.checked.append(needed)
        left = LEFT[self.model]
        if needed > left:
            raise LLMQuotaExceeded(f"{self.model}: 오늘 남은 호출 {left}회, 필요한 호출 {needed}회")

    def complete_json(self, request):
        pytest.fail("예산 점검 중에 호출했다")


LEFT = {}


def args(**kw):
    return Namespace(accept_free_tier_terms=True, fresh_cache=False, cache_profile=False, **kw)


def roles(tmp_path, env, judge_calls=132, writer_calls=8, **kw):
    StubClient.created = []
    return prepare_roles(args(**kw), notice="안내", judge_calls=judge_calls, writer_calls=writer_calls, results_dir=tmp_path,
                         env_loader=lambda: dict(env), client_cls=StubClient, usage_run_id="demo-1")


def test_same_model_adds_both_roles_into_one_check(tmp_path):
    LEFT.update({"base-model": 140})
    r = roles(tmp_path, BASE)
    assert r.shared_model and r.judge.gemini is r.writer.gemini and len(StubClient.created) == 1
    assert StubClient.created[0].checked == [140]  # 84 + 48 + 4 + 4
    assert r.usage.run_id == "demo-1"  # 사용량 기록이 하나의 demo_run_id로 묶인다


def test_same_model_boundary_one_short_stops_before_any_call(tmp_path, capsys):
    LEFT.update({"base-model": 139})
    with pytest.raises(AiSetupError, match="시작하지 않았다"):
        roles(tmp_path, BASE)


def test_different_models_are_checked_separately(tmp_path):
    LEFT.update({"judge-model": 132, "writer-model": 8})
    r = roles(tmp_path, {**BASE, "GEMINI_MODEL_JUDGE": "judge-model", "GEMINI_MODEL_WRITER": "writer-model"})
    assert not r.shared_model and r.judge.gemini is not r.writer.gemini
    assert {c.model: c.checked for c in StubClient.created} == {"judge-model": [132], "writer-model": [8]}
    assert (r.judge.settings.model, r.writer.settings.model) == ("judge-model", "writer-model")


@pytest.mark.parametrize("judge_left,writer_left", [(131, 8), (132, 7), (0, 0)])
def test_either_model_being_short_stops_the_run_and_both_are_checked(tmp_path, judge_left, writer_left):
    LEFT.update({"judge-model": judge_left, "writer-model": writer_left})
    with pytest.raises(AiSetupError, match="시작하지 않았다"):
        roles(tmp_path, {**BASE, "GEMINI_MODEL_JUDGE": "judge-model", "GEMINI_MODEL_WRITER": "writer-model"})
    assert all(c.checked for c in StubClient.created)  # 한쪽이 모자라도 둘 다 점검해 한 번에 알린다


def test_the_writer_alone_being_short_is_reported_even_if_the_judge_fits(tmp_path):
    LEFT.update({"judge-model": 500, "writer-model": 3})
    with pytest.raises(AiSetupError) as e:
        roles(tmp_path, {**BASE, "GEMINI_MODEL_JUDGE": "judge-model", "GEMINI_MODEL_WRITER": "writer-model"})
    assert "writer-model" in str(e.value) and "judge-model" not in str(e.value)


def test_command_line_overrides_apply_per_role(tmp_path):
    LEFT.update({"cli-judge": 500, "cli-writer": 500})
    r = roles(tmp_path, BASE, model_judge="cli-judge", model_writer="cli-writer", rpd_writer=30)
    assert (r.judge.settings.model, r.writer.settings.model, r.writer.settings.rpd, r.judge.settings.rpd) == ("cli-judge", "cli-writer", 30, 500)


def test_same_model_with_different_limits_is_refused(tmp_path):
    LEFT.update({"base-model": 500})
    with pytest.raises(AiSetupError, match="한도 설정이 다르다"):
        roles(tmp_path, {**BASE, "GEMINI_RPD_WRITER": "20"})


def test_no_consent_sends_nothing_and_creates_no_client(tmp_path):
    StubClient.created = []
    with pytest.raises(AiSetupError, match="--accept-free-tier-terms"):
        prepare_roles(Namespace(accept_free_tier_terms=False), notice="안내", judge_calls=1, writer_calls=1, results_dir=tmp_path,
                      env_loader=lambda: dict(BASE), client_cls=StubClient)
    assert StubClient.created == []


def test_missing_key_or_model_is_a_setup_error_not_a_crash(tmp_path):
    with pytest.raises(AiSetupError, match="GEMINI_API_KEY"):
        roles(tmp_path, {"GEMINI_MODEL": "m"})


def test_each_role_has_its_own_cache_directory_and_writer_profiles_stay_off_disk_by_default(tmp_path):
    LEFT.update({"base-model": 500})
    r = roles(tmp_path, BASE)
    assert r.judge.client.extract_dir == tmp_path / "llm_cache" / "extract" and r.writer.client.extract_dir == tmp_path / "llm_cache" / "writer"
    assert not r.writer.client.cache_profile
    first = roles(tmp_path, BASE)
    fresh_args = Namespace(accept_free_tier_terms=True, fresh_cache=True, cache_profile=False)
    r2 = prepare_roles(fresh_args, notice="안내", judge_calls=1, writer_calls=1, results_dir=tmp_path, env_loader=lambda: dict(BASE),
                       client_cls=StubClient, usage_run_id="demo-2")
    assert r2.judge.client.extract_dir.name == "run-demo-2" and r2.writer.client.extract_dir.name == "run-demo-2-writer"
    assert first.usage is not r2.usage


def test_retry_constant_matches_the_budget_formula_in_the_policy():
    assert 21 * (1 + MAX_RETRIES) + 12 * (1 + MAX_RETRIES) + 2 * (1 + MAX_RETRIES) == 140  # 정책 presentation.md 5장
