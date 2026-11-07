"""정책 docs/policies/presentation.md와 코드가 같은 말을 하는지 대조한다 (이슈 #24 Step 6). 네트워크를 쓰지 않는다."""
import re
from pathlib import Path

from agents import job_analyzer, profile_analyzer, task_linker
from agents.application_writer import STAGE as WRITER_STAGE
from core import job_gate_ai
from core.llm_client import MAX_RETRIES, STAGE_TASKS, GeminiSettings, task_of
from tools import run_presentation as rp

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "policies" / "presentation.md").read_text(encoding="utf-8")


def section(start, end):
    return DOC[DOC.index(start):DOC.index(end, DOC.index(start))]


def rows(text):
    return [[c.strip() for c in line.strip().strip("|").split("|")] for line in text.splitlines() if line.startswith("|") and not line.startswith("|---")]


def test_the_stage_names_in_the_role_table_are_the_ones_the_agents_really_use():
    table = rows(section("## 1. 모델 역할", "- **JUDGE는 SMALL"))
    judge = re.findall(r"`(\w+)`", next(r for r in table if r[0].startswith("**JUDGE**"))[2])
    writer = re.findall(r"`(\w+)`", next(r for r in table if r[0].startswith("**WRITER**"))[2])
    assert set(judge) == {job_gate_ai.STAGE, job_analyzer.STAGE, profile_analyzer.STAGE}
    assert set(writer) >= set(WRITER_STAGE.values())  # 연결 후보 단계는 문장으로 적혀 있다: 이름은 아래에서 따로 확인
    assert {task_of(s) for s in judge} == {"gate", "eligibility"} and {task_of(s) for s in writer} == {"writer"}
    assert task_of(task_linker.STAGE) == "writer"


def test_the_settings_table_names_are_read_by_the_code_and_listed_in_the_env_example():
    names = re.findall(r"`(GEMINI_[A-Z_]+)`", section("**설정.**", "- 명령줄은"))
    role_vars = {n for n in names if n.endswith(("_JUDGE", "_WRITER"))}
    assert role_vars == {f"GEMINI_{k}_{role}" for k in ("MODEL", "RPM", "RPD") for role in ("JUDGE", "WRITER")}
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert all(n in example for n in role_vars)
    env = {"GEMINI_API_KEY": "k", "GEMINI_MODEL": "base", "GEMINI_RPM": "9", "GEMINI_RPD": "99"}
    env.update({f"GEMINI_MODEL_{r}": f"model-{r.lower()}" for r in ("JUDGE", "WRITER")})
    env.update({f"GEMINI_RPM_{r}": str(i + 1) for i, r in enumerate(("JUDGE", "WRITER"))})
    env.update({f"GEMINI_RPD_{r}": str(i + 11) for i, r in enumerate(("JUDGE", "WRITER"))})
    got = {role: GeminiSettings.from_env(env, role) for role in ("judge", "writer")}
    assert (got["judge"].model, got["judge"].rpm, got["judge"].rpd) == ("model-judge", 1, 11)
    assert (got["writer"].model, got["writer"].rpm, got["writer"].rpd) == ("model-writer", 2, 12)


def test_the_exit_code_table_lists_exactly_the_codes_the_tool_returns():
    table = section("## 4. 실패 처리와 종료 코드", "- **초안 생성 성공")
    documented = {int(r[-1]) for r in rows(table)[1:]}
    assert documented == {rp.EXIT_OK, rp.EXIT_FAILED, rp.EXIT_REFUSED, rp.EXIT_QUOTA, rp.EXIT_STOPPED} == {0, 1, 2, 3, 4}


def test_the_step_table_matches_the_steps_of_the_result_object():
    table = rows(section("| 번호 | 단계", "단계 상태는"))[1:]
    documented = [re.search(r"`(\w+)`", r[1]).group(1) for r in table]
    assert documented == ["scenario", "budget", *rp.STEP_NAMES]  # scenario와 budget은 첫 호출 전의 단계라 종료 코드 2로만 나타난다
    assert rp.STEP_NAMES[-2:] == ("cover_letter", "portfolio")


def test_the_budget_numbers_in_the_policy_follow_the_retry_constant():
    attempts = 1 + MAX_RETRIES
    assert MAX_RETRIES == 3 and "재시도 상한 3" in DOC
    gate, eligibility, link, cover, portfolio = 21 * attempts, 12 * attempts, attempts, attempts, attempts
    assert (gate, eligibility) == (84, 48)
    budget = section("## 5. 호출 예산", "## 6.")
    for number in (gate, eligibility, gate + eligibility, gate + eligibility + cover + portfolio, gate + eligibility + cover + portfolio + link, cover + portfolio, cover + portfolio + link):
        assert str(number) in budget, number  # 84, 48, 132, 140, 144, 8, 12


def test_the_usage_task_mapping_in_the_policy_matches_the_code():
    row = next(r for r in rows(section("**사용량 기록 칸**", "이전 기록")) if r[0] == "`task`")
    for stage, task in (("gate_label", "gate"), ("extract_requirements", "eligibility"), ("find_evidence", "eligibility")):
        assert re.search(rf"`{stage}`[^→]*→ `{task}`", row[1]) and STAGE_TASKS[stage] == task
    assert {STAGE_TASKS[s] for s in (*WRITER_STAGE.values(), task_linker.STAGE)} == {"writer"} and "→ `writer`" in row[1] and "→ `other`" in row[1]


def test_the_fixed_scenario_in_the_policy_is_the_one_in_the_code():
    table = section("## 2. 발표 시나리오 (고정)", "**자동 대체를 하지 않는다.**")
    assert f"`{rp.PRESENTATION_JOB_ID}` 고정" in table and "sample-3" == rp.PRESENTATION_JOB_ID
    assert rp.EXPECTED_RANKING == ["sample-3", "sample-1", "sample-4", "sample-5", "sample-2", "sample-6"]
    assert "--scenario full-ai-demo" in table
