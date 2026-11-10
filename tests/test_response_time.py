"""시도별 응답 시간(duration_ms) 기록 테스트 (정책 presentation.md 6장, 이슈 #26). 가짜 시계만 쓰고 네트워크는 쓰지 않는다."""
import json

from test_llm_client import OK, FakeAPIError, GeminiClient, GeminiSettings, req

from core.llm_client import CachedClient, FakeClient, UsageLog, format_timing


class SlowClock:
    """generate가 호출될 때마다 정해 둔 시간(초)만큼 시계를 흘려보낸다. sleep도 시계를 흘려보낸다."""

    def __init__(self, durations):
        self.t, self.durations, self.sleeps = 1000.0, list(durations), []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def generate(self, script):
        def run(request):
            self.t += self.durations.pop(0)
            item = script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return run


def client(tmp_path, durations, script, rpm=5):
    clock = SlowClock(durations)
    log = UsageLog(tmp_path / "u.jsonl")
    c = GeminiClient(GeminiSettings("k", "gemini-test", rpm, 100), log, generate=clock.generate(script), sleep=clock.sleep, clock=clock.now)
    return c, clock, log


def rows(log):
    return [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]


def test_attempt_duration_is_recorded_in_milliseconds(tmp_path):
    c, _, log = client(tmp_path, [2.5], [OK])
    c.complete_json(req())
    assert [r["duration_ms"] for r in rows(log)] == [2500]


def test_rate_limit_wait_is_not_part_of_the_duration(tmp_path):
    c, clock, log = client(tmp_path, [2.0, 3.0], [OK, OK], rpm=5)  # 최소 간격 13초
    c.complete_json(req(job_id="a"))
    c.complete_json(req(job_id="b"))
    assert max(clock.sleeps) > 5  # 두 번째 호출 앞에서 한도 대기가 실제로 있었다
    assert [r["duration_ms"] for r in rows(log)] == [2000, 3000]


def test_retry_wait_is_not_part_of_the_duration_and_failed_attempts_are_recorded(tmp_path):
    c, clock, log = client(tmp_path, [1.0, 2.0], [FakeAPIError(503), OK], rpm=60)
    c.complete_json(req())
    first, second = rows(log)
    assert first["ok"] is False and first["duration_ms"] == 1000
    assert second["ok"] is True and second["duration_ms"] == 2000
    assert len(clock.sleeps) >= 1  # 재시도 대기가 있었지만 시간에 들어가지 않았다


def test_cache_hits_have_no_duration(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl")
    cached = CachedClient(FakeClient([{"answer": "a"}]), usage=log, extract_dir=tmp_path / "ex", profile_dir=tmp_path / "pr")
    cached.complete_json(req())
    cached.complete_json(req())
    assert "duration_ms" not in rows(log)[0] and rows(log)[0]["cached"] is True


def test_timing_counts_only_timed_attempts_and_old_rows_are_read(tmp_path):
    path = tmp_path / "u.jsonl"
    log = UsageLog(path, run_id="r")
    log.record(req(), model="m", duration_ms=1000)
    log.record(req(), model="m", ok=False, error="ServerError 503", duration_ms=3000)
    log.record(req(), model="m", cached=True)
    log.record(req(), model="m")  # 이전 기록처럼 시간 칸이 없는 줄
    other = UsageLog(path, run_id="other")
    other.record(req(), model="m", duration_ms=9000)
    assert log.timing() == {"timed": 2, "total_ms": 4000, "max_ms": 3000}  # 다른 실행은 더하지 않는다
    assert log.run_totals()["calls"] == 3 and set(log.run_totals()) == {"calls", "failed", "cache_hits", "input_tokens", "output_tokens"}
    assert UsageLog(path, run_id="nobody").timing() == {"timed": 0, "total_ms": 0, "max_ms": 0}


def test_timing_can_be_split_by_task_and_stage(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl", run_id="r")
    log.record(req(stage="gate_label"), model="m", duration_ms=1000)
    log.record(req(stage="write_cover_letter"), model="m", duration_ms=5000)
    assert log.timing(task="gate")["total_ms"] == 1000 and log.timing(task="writer")["total_ms"] == 5000
    assert log.timing(exclude_stage="gate_label")["total_ms"] == 5000 and log.timing(stage="gate_label")["timed"] == 1


def test_summary_text_averages_over_timed_attempts_and_says_when_there_is_none():
    assert format_timing({"timed": 0, "total_ms": 0, "max_ms": 0}) == "응답 시간 기록 없음"
    assert format_timing({"timed": 4, "total_ms": 10000, "max_ms": 4000}) == "평균 응답 시간 2.5초(최대 4.0초, 4회 기록)"


# ---------------------------------------------------------------- 도구의 요약 줄

def test_presentation_summary_shows_the_timing_per_task_and_tells_when_it_is_not_recorded():
    from tools import run_presentation as rp

    base = dict(logical=2, attempts=2, cache_hits=0, input_tokens=1, output_tokens=1, requested=["m"], actual=["m"])
    result = rp.PresentationResult("ai", "demo-x", {n: rp.Step(n) for n in rp.STEP_NAMES}, models={"judge": "m", "writer": "m"})
    result.usage = {"gate": rp.TaskUsage(**base, timing={"timed": 2, "total_ms": 3000, "max_ms": 2000}), "writer": rp.TaskUsage(**base)}
    text = " ".join(rp.render_result(result))
    assert "평균 응답 시간 1.5초(최대 2.0초, 2회 기록)" in text and "응답 시간 기록 없음" in text


def test_cached_rows_never_count_even_if_a_duration_is_written(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl", run_id="r")
    log.record(req(), model="m", cached=True, duration_ms=500)
    log.record(req(), model="m", duration_ms=1000)
    assert log.timing() == {"timed": 1, "total_ms": 1000, "max_ms": 1000}


def test_usage_summary_passes_each_tasks_timing_to_the_result(tmp_path):
    from types import SimpleNamespace

    from tools import run_presentation as rp

    log = UsageLog(tmp_path / "u.jsonl", run_id="r")
    log.record(req(stage="gate_label"), model="m", duration_ms=1000)
    log.record(req(stage="write_cover_letter"), model="m", duration_ms=4000)
    result = rp.PresentationResult("ai", "demo-x", {n: rp.Step(n) for n in rp.STEP_NAMES})
    rp._usage_summary(SimpleNamespace(models={"judge": "m", "writer": "m"}, usage=log, limits={"m": 100}), result)
    assert result.usage["gate"].timing["total_ms"] == 1000 and result.usage["writer"].timing["total_ms"] == 4000
