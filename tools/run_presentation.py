"""발표용 통합 실행: 게이트 AI -> 자격요건 AI -> 코드 순위 -> 사용자 공고 선택 -> WriterInput -> 자소서 AI -> 포트폴리오 AI -> 요약 (이슈 #24, #28).

사용법 (저장소 루트에서):
    python -m tools.run_presentation                       # mock (기본): 네트워크를 쓰지 않는다 (dry-run)
    python -m tools.run_presentation --ai --accept-free-tier-terms [--fresh-cache] [--model-judge ...] [--model-writer ...]
                                                           # 게이트·자격요건은 JUDGE 모델, 자소서·포트폴리오는 WRITER 모델 (가상 프로필과 샘플 공고만)

정책: docs/policies/presentation.md. core.pipeline에는 Writer를 넣지 않고, 이 파일이 기존 함수를 부르는 순서만 맡는다.
- 프로필은 김하늘(가상), 공고는 샘플 6건이다. 초안을 만들 공고는 평가가 끝난 뒤 사용자가 번호(숫자)로 고른다. 자동 선택은 없고, 입력이 없거나 후보가 없으면 다른 공고로 바꾸지 않고 멈춘다.
- PipelineRun을 저장하고 다시 읽어 검증한 뒤에만 Writer 단계로 간다. 선택과 WriterInput은 저장본이 아니라 메모리 원본에서 만든다. WriterInput은 저장하지 않는다.
- 자소서와 포트폴리오는 서로 독립이다. 한쪽 실패가 다른 쪽 결과를 지우지 않는다.
- 종료 코드: 0 모두 완료, 1 끝까지 갔지만 실패가 있음, 2 시작하지 않음, 3 호출 한도 소진, 4 한도와 무관한 시연 중단(공고 선택 실패, 저장·재로딩 검증 실패).
화면에는 단계 상태, 순위, 코드가 만든 사유, 개수와 id만 낸다. 공고 원문, 프로필 인용, 모델 출력, 오류 내용은 출력하지 않는다.
"""
import argparse
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agents.application_writer import KINDS, ApplicationDraft, DraftError, make_writer_mock, write_application
from agents.task_linker import build_links, link_tasks
from core.llm_client import MAX_RETRIES, GeminiClient, LLMClient, LLMQuotaExceeded, LLMStageError, UsageLog, load_env
from core.llm_mock import make_evidence_mock, make_extraction_mock
from core.pipeline import ScenarioJob, run_pipeline
from core.pipeline_run import PipelineRun
from core.schemas import DISPLAY_LABELS, EligibilityProfile
from core.selection import SelectionRefused, select_job
from core.writer_input import WriterInputError, build_writer_input
from tools import eval_eligibility as ev
from tools.ai_common import AiSetupError, extra_cached_client, prepare_roles
from tools.demo_support import (
    GATE_CASES_JSON,
    PROFILE_REF,
    ScenarioError,
    build_scenario,
    gate_ai_budget,
    job_info,
    load_run,
    render,
    render_draft,
    save_draft_md,
    save_run,
)

REPO = Path(__file__).resolve().parents[1]
RESULTS_DIR = ev.RESULTS_DIR
TASK_LINKS_JSON = REPO / "tests" / "fixtures" / "task_links_sample3.json"  # 사람이 확정한 가상 프로필의 연결 후보 (합성 fixture, 정책 application-writing.md D18)
WRITER_MOCK_JSON = REPO / "tests" / "fixtures" / "writer_mock_sample3.json"
PRESENTATION_JOB_ID = "sample-3"  # 기대 순위 확인과, 초안 문장·연결 후보 고정본이 있는 유일한 공고(mock의 후보)를 가리킨다. 선택은 사용자가 한다 (정책 presentation.md 2-1)
SELECTION_ATTEMPTS = 3  # 잘못된 입력을 다시 묻는 횟수 (정책 2-1)
EXPECTED_RANKING = ["sample-3", "sample-1", "sample-4", "sample-5", "sample-2", "sample-6"]  # 정책 eligibility.md 14장. mock에서만 단언한다 (D13)
STEP_NAMES = ("pipeline", "persistence", "selection", "writer_input", "cover_letter", "portfolio")
EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_QUOTA, EXIT_STOPPED = 0, 1, 2, 3, 4
TASKS = (("gate", "직무 게이트"), ("eligibility", "자격요건"), ("writer", "자소서·포트폴리오"))


# ---------------------------------------------------------------- 결과 객체 (메모리에만 있다. 저장하지 않는다)

@dataclass
class Step:
    name: str
    status: str = "not_run"           # completed / failed / not_run (data-contract 3-3과 같은 말)
    code: str | None = None           # 범주 이름. 오류 내용은 담지 않는다
    saved: bool | None = None         # 초안 단계만: 확인용 md를 저장했는가
    path: Path | None = None


@dataclass
class TaskUsage:
    logical: int = 0
    attempts: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    requested: list[str] = field(default_factory=list)
    actual: list[str] = field(default_factory=list)


@dataclass
class PresentationResult:
    mode: str
    demo_run_id: str
    steps: dict[str, Step]
    pipeline_run: PipelineRun | None = None      # 메모리 원본. run_id는 저장본과 맞춘다
    saved_run: PipelineRun | None = None         # 저장한 뒤 돌려받은 것. 공식 pipeline_run_id는 saved_run.run_id
    saved_path: Path | None = None
    drafts: dict[str, ApplicationDraft] = field(default_factory=dict)
    fact_counts: dict[str, int] | None = None    # WriterInput 안전 분류 개수 (문장은 담지 않는다)
    fact_ids: dict[str, list[str]] | None = None
    usage: dict[str, TaskUsage] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)   # 역할 -> 요청한 모델 ID
    today: dict[str, tuple[int, int]] = field(default_factory=dict)  # 모델 -> (오늘 누적, 일일 한도)
    selected_job_id: str | None = None
    exit_code: int = EXIT_OK

    @property
    def pipeline_run_id(self) -> str | None:
        return self.saved_run.run_id if self.saved_run else None


@dataclass
class Plan:
    """실행에 필요한 것을 모두 준비한 상태. mock과 실제 AI는 여기서만 다르다."""

    mode: str                                # "mock" | "ai"
    demo_run_id: str
    jobs: list[ScenarioJob]
    profile: EligibilityProfile
    desired_family: str
    extract_client: LLMClient
    evidence_client: LLMClient
    writer_client: LLMClient
    results_dir: Path
    gate_client: LLMClient | None = None
    usage: UsageLog | None = None
    gate_cases: Path | None = None
    task_links: str = "auto"        # auto(sample-3은 고정본, 그 외는 AI) / fixture(후보를 sample-3로 제한) / ai / none (정책 presentation.md 2장)
    cache_mode: str = "memory"
    models: dict[str, str] = field(default_factory=dict)
    limits: dict[str, int] = field(default_factory=dict)   # 모델 -> 일일 한도
    posting_ref: str | None = None                      # 공고 출처 문구. {job_id} 자리에 선택한 공고 ID가 들어간다
    numbers: dict[str, int] = field(default_factory=dict)   # 공고 ID -> 입력할 번호 (샘플의 number)

    @property
    def external(self) -> bool:
        return self.mode == "ai"


def make_demo_run_id() -> str:
    return "demo-" + uuid.uuid4().hex[:8]


# ---------------------------------------------------------------- 실행

def _fail(step: Step, code: str) -> None:
    step.status, step.code = "failed", code


def _task_links(plan: Plan, selected, info, job_id: str):
    """--task-links에 따라 업무 연결 후보를 만든다. none은 없음, fixture는 사람이 확정한 합성 고정본, ai는 호출 1회(WRITER 모델)."""
    mode = plan.task_links if plan.task_links != "auto" else ("fixture" if job_id == PRESENTATION_JOB_ID else "ai")  # 고정본은 sample-3에만 있다
    if mode == "none":
        return []
    if mode == "fixture":
        entry = {k: v for k, v in json.loads(TASK_LINKS_JSON.read_text(encoding="utf-8")).items() if not k.startswith("_")}.get(job_id)
        if entry is None:
            raise WriterInputError("고정된 업무 연결 후보가 없다")
        return build_links(plan.profile, entry["tasks"], {"links": entry["links"]})
    return link_tasks(plan.profile, info.main_tasks, plan.writer_client, allow_external_profile=plan.external,
                      is_synthetic=selected.profile.is_synthetic, job_id=job_id)


@dataclass
class Candidate:
    job_id: str
    number: int
    line: str


def candidates(run: PipelineRun, plan: Plan) -> tuple[list[Candidate], str | None]:
    """선택 후보와 제한 안내를 돌려준다 (정책 2-1). 후보는 평가를 마치고(completed) 직무 일치 상위 그룹(primary)인 공고이고 순위 순서다.

    자격요건 미충족도 후보다(상태를 줄에 보인다). mock이거나 연결 후보 고정본(fixture)을 쓰면 초안 재료가 sample-3에만 있어 sample-3만 남긴다.
    select_job()의 계약(참고용 공고도 선택 가능)은 바꾸지 않는다: 후보 제한은 여기서만 한다.
    """
    pool = sorted((j for j in run.jobs if j.evaluation_status == "completed" and j.gate.ranking_group == "primary" and j.job_id in plan.numbers),
                  key=lambda j: j.rank)
    notice = None
    if plan.mode == "mock" or plan.task_links == "fixture":
        kept = [j for j in pool if j.job_id == PRESENTATION_JOB_ID]
        if len(kept) < len(pool):
            notice = ("mock은 sample-3 초안만 준비돼 있어 sample-3만 고를 수 있다" if plan.mode == "mock"
                      else "연결 후보 고정본은 sample-3에만 있어 sample-3만 고를 수 있다 (--task-links ai로 다른 공고를 고른다)")
        pool = kept
    return [Candidate(j.job_id, plan.numbers[j.job_id], f"{j.rank}위 [{j.job_id}] {j.job_label or ''} — {DISPLAY_LABELS.get(j.eligibility.verdict, j.eligibility.verdict)}") for j in pool], notice


def read_line(prompt: str) -> str | None:
    """한 줄을 읽는다. 입력이 끝났거나 읽을 수 없으면 None. 테스트가 이 함수를 바꿔 끼운다."""
    try:
        return input(prompt)
    except (EOFError, OSError):
        return None


def ask_for_job(found: list[Candidate], notice: str | None = None) -> tuple[str | None, str | None]:
    """후보를 보이고 번호(숫자)를 입력받는다 (정책 2-1). (job_id, None) 또는 (None, 실패 코드)를 돌려준다. 기본값은 없다."""
    if not found:
        print("상위 직무 매칭 공고가 없습니다.", flush=True)
        return None, "no_candidate"
    print("자소서/포트폴리오 초안을 생성할 공고 번호를 입력해주세요.")
    for c in found:
        print(f"  {c.line}")
    if notice:
        print(f"  ({notice})")
    by_number = {c.number: c.job_id for c in found}
    for attempt in range(SELECTION_ATTEMPTS):
        raw = read_line(f"공고 번호만 입력하세요 (예: {found[0].number}): ")
        if raw is None:
            print(flush=True)
            return None, "no_input"
        text = raw.strip()
        if re.fullmatch(r"[0-9]+", text) and int(text) in by_number:
            if not sys.stdin.isatty():  # 파이프로 받은 답은 화면에 보이지 않으므로, 받아들인 번호만 질문 뒤에 보인다. 거절한 입력의 원문은 화면에 내지 않는다
                print(int(text), flush=True)
            return by_number[int(text)], None
        left = SELECTION_ATTEMPTS - attempt - 1
        print("후보에 있는 공고 번호(숫자)만 입력할 수 있다." + (f" 남은 횟수 {left}회." if left else ""), flush=True)
    return None, "invalid_input"


def _persist(plan: Plan, run: PipelineRun, result: PresentationResult) -> PipelineRun | None:
    """PipelineRun을 저장하고(프로필 인용 제외) 다시 읽어 저장본과 같은지 확인한다. 성공하면 run_id를 저장본에 맞춘 메모리 원본을 돌려준다.

    save_run은 같은 이름의 파일이 있으면 새 run_id를 뽑는다. 그래서 공식 pipeline_run_id는 저장본의 run_id다 (정책 presentation.md 6장).
    """
    step = result.steps["persistence"]
    try:
        path, saved = save_run(run, plan.results_dir / "pipeline", save_profile_evidence=False)
    except OSError:  # 저장 오류. 오류 내용은 옮기지 않는다. 검증 오류(ValueError)는 코드 결함이라 잡지 않는다
        _fail(step, "persistence_failed")
        return None
    try:
        reloaded = load_run(path)
    except (OSError, ValueError):  # 저장한 파일이 읽히지 않거나 형식이 틀림
        _fail(step, "persistence_failed")
        return None
    if reloaded != saved:
        _fail(step, "reload_mismatch")
        return None
    step.status, result.saved_run, result.saved_path = "completed", saved, path
    return run.model_copy(update={"run_id": saved.run_id})


def _done(progress, label: str, step: Step) -> None:
    """단계가 끝났음을 한 줄로 알린다 (정책 presentation.md 8장 진행 줄). 상태와 코드만 낸다."""
    progress(f"{label}: {_step_line(step).split(': ', 1)[1]}")


def _write_draft(plan: Plan, wi, kind: str, result: PresentationResult) -> bool:
    """초안 1건을 만든다. 한도 소진이면 True(남은 Writer 단계를 시작하지 않는다)를 돌려준다. 다른 실패는 이 초안만 failed로 남긴다."""
    step = result.steps[kind]
    try:
        draft = write_application(wi, plan.writer_client, kind, allow_external_profile=plan.external, mode=plan.mode)
    except LLMQuotaExceeded:
        _fail(step, "quota_exhausted")
        return True
    except DraftError as e:
        _fail(step, e.code)
        return False
    except LLMStageError:
        _fail(step, "stage_failed")
        return False
    result.drafts[kind] = draft
    step.status = "completed"
    if plan.mode == "ai":  # 실제 AI 초안은 확인용 md를 항상 쓴다 (정책 application-writing.md 결정 D13)
        try:
            step.path = save_draft_md(draft, plan.results_dir / "drafts")
            step.saved = True
        except OSError:  # FileExistsError 포함. 생성한 초안은 result.drafts에 남는다
            step.saved, step.code = False, "save_failed"
    return False


def _usage_summary(plan: Plan, result: PresentationResult) -> None:
    result.models = dict(plan.models)
    if plan.usage is None:
        return
    for task, _ in TASKS:
        totals = plan.usage.run_totals(task=task)
        models = plan.usage.models_used(task)
        result.usage[task] = TaskUsage(plan.usage.logical_calls(task=task), totals["calls"], totals["cache_hits"],
                                       totals["input_tokens"], totals["output_tokens"], models["requested"], models["actual"])
    for model, limit in plan.limits.items():
        result.today[model] = (plan.usage.calls_today(model), limit)


def _exit_code(result: PresentationResult) -> int:
    steps = result.steps.values()
    run = result.saved_run or result.pipeline_run
    quota = any(s.code == "quota_exhausted" for s in steps) or (run is not None and any(j.failure_code == "quota_exhausted" for j in run.jobs))
    if result.steps["selection"].code == "quota_exhausted":
        return EXIT_QUOTA
    if result.steps["persistence"].status == "failed":
        return EXIT_STOPPED
    if result.steps["selection"].status == "failed":
        # 평가가 호출 한도 때문에 멈춰 후보가 없는 경우는 한도 소진이다 (정책 4장). 그 밖의 선택 실패는 한도와 무관한 중단이다
        return EXIT_QUOTA if (quota and result.steps["selection"].code == "no_candidate") else EXIT_STOPPED
    if quota:
        return EXIT_QUOTA
    failed = any(s.status == "failed" or s.saved is False for s in steps) or (run is not None and (run.counts["failed"] or run.counts["not_run"]))
    return EXIT_FAILED if failed else EXIT_OK


def _select_and_write(plan: Plan, original: PipelineRun, result: PresentationResult, progress, choose, wait: str) -> None:
    """후보를 보이고 사용자가 고른 공고로 선택 -> WriterInput -> 자소서 -> 포트폴리오를 진행한다 (정책 presentation.md 2-1, 3장)."""
    steps = result.steps
    found, notice = candidates(original, plan)
    job_id, code = (choose or ask_for_job)(found, notice)
    if job_id is None:  # 입력이 없거나 후보가 없다: 다른 공고로 바꾸지 않는다
        _fail(steps["selection"], code)
        _done(progress, "[4] 공고 선택", steps["selection"])
        return
    result.selected_job_id = job_id
    try:
        selected = select_job(original, job_id, include_profile_evidence=True, posting_ref=(plan.posting_ref or "").replace("{job_id}", job_id) or None)
    except SelectionRefused:  # 다른 공고로 바꾸지 않는다
        job = next((j for j in original.jobs if j.job_id == job_id), None)
        _fail(steps["selection"], (job.failure_code or job.evaluation_status) if job else "no_such_job")
        _done(progress, "[4] 공고 선택", steps["selection"])
        return
    steps["selection"].status = "completed"
    progress(f"[4] 공고 선택: 완료 ({job_id})")
    try:
        info = job_info(job_id, plan.gate_cases)
        links = _task_links(plan, selected, info, job_id)
        wi = build_writer_input(selected, plan.profile, info, task_links=links)
    except LLMQuotaExceeded:
        _fail(steps["writer_input"], "quota_exhausted")
        progress("[5] WriterInput: 실패 (quota_exhausted)")
        return
    except LLMStageError:
        _fail(steps["writer_input"], "task_links_failed")
        progress("[5] WriterInput: 실패 (task_links_failed)")
        return
    except WriterInputError:
        _fail(steps["writer_input"], "writer_input_failed")
        progress(f"[5] WriterInput: 실패 ({steps['writer_input'].code})")
        return
    steps["writer_input"].status = "completed"
    progress("[5] WriterInput 안전 분류: 완료")
    result.fact_counts = {"usable_facts": len(wi.usable_facts), "to_confirm": len(wi.to_confirm), "do_not_claim": len(wi.do_not_claim)}
    result.fact_ids = {"usable_facts": [f.fact_id for f in wi.usable_facts], "to_confirm": [c.confirm_id for c in wi.to_confirm],
                       "do_not_claim": [x.forbidden_id for x in wi.do_not_claim]}
    stop = False
    for kind in KINDS:  # 서로 독립: 한쪽 실패가 다른 쪽을 건너뛰게 하지 않는다 (한도 소진만 예외)
        if stop:
            continue
        progress(f"[{6 + KINDS.index(kind)}] {STEP_TEXT[kind]}: 시작{wait}")
        stop = _write_draft(plan, wi, kind, result)
        _done(progress, f"[{6 + KINDS.index(kind)}] {STEP_TEXT[kind]}", result.steps[kind])


def execute(plan: Plan, progress=None, choose=None) -> PresentationResult:
    """발표 흐름 전체(정책 presentation.md 3장)를 실행한다. 단계 실패는 상태로 남기고, 코드 결함(TypeError 등)이나 설정 오류는 그대로 올린다.

    progress(line)가 있으면 단계가 시작하고 끝날 때마다 한 줄씩 부른다 (정책 8장). choose(후보, 안내)는 공고를 고르는 함수이고 기본은 사용자 입력(ask_for_job)이다.
    """
    progress = progress or (lambda line: None)
    result = PresentationResult(plan.mode, plan.demo_run_id, {n: Step(n) for n in STEP_NAMES})
    steps = result.steps

    wait = " (수 분 걸릴 수 있음)" if plan.external else ""
    progress(f"[1~3] 직무 게이트·자격요건·순위: 시작, 공고 {len(plan.jobs)}건{wait}")
    run = run_pipeline(plan.jobs, plan.profile, PROFILE_REF, plan.extract_client, plan.evidence_client, desired_family=plan.desired_family,
                       gate_client=plan.gate_client, allow_external_profile=plan.external, usage=plan.usage, cache_mode=plan.cache_mode,
                       on_progress=lambda stage, done, total, job_id, status: progress(f"[1~3] {PROGRESS_STAGE[stage]} {done}/{total}: {job_id} ({STATUS_TEXT[status]})"))
    steps["pipeline"].status = "completed"
    result.pipeline_run = run
    progress(f"[1~3] 직무 게이트·자격요건·순위: 완료 (완료 {run.counts['completed']}, 실패 {run.counts['failed']}, 안 함 {run.counts['not_run']})")

    original = _persist(plan, run, result)
    _done(progress, "[저장] 결과 저장·재검증", steps["persistence"])
    if original is None:  # 저장·재로딩 검증이 선행 조건이다: Writer를 한 번도 부르지 않고 멈춘다
        result.pipeline_run = run
    else:
        result.pipeline_run = original
        _select_and_write(plan, original, result, progress, choose, wait)
    _usage_summary(plan, result)
    result.exit_code = _exit_code(result)
    return result


# ---------------------------------------------------------------- 화면

PROGRESS_STAGE = {"gate": "게이트", "eligibility": "자격요건"}  # 정책 presentation.md 3-1, 8장의 공고별 진행 줄
STATUS_TEXT = {"completed": "완료", "failed": "실패", "not_run": "안 함"}
STEP_TEXT = {"pipeline": "게이트·자격요건·순위", "persistence": "결과 저장·재검증", "selection": "공고 선택", "writer_input": "초안 입력(WriterInput)",
             "cover_letter": "자기소개서", "portfolio": "포트폴리오"}


def _step_line(step: Step) -> str:
    extra = f" ({step.code})" if step.code else ""
    if step.status == "completed" and step.saved is False:
        return f"{STEP_TEXT[step.name]}: 생성 성공, 저장 실패{extra}"
    return f"{STEP_TEXT[step.name]}: {STATUS_TEXT[step.status]}{extra}"


def render_result(result: PresentationResult, quiet: bool = False) -> list[str]:
    """발표용 요약 (정책 presentation.md 8장). 개수·id·상태·코드가 만든 사유만 낸다."""
    run, saved = result.pipeline_run, result.saved_run
    ai = result.mode == "ai"
    lines = []
    if quiet:
        lines.append(f"demo {result.demo_run_id} pipeline {result.pipeline_run_id or '-'}")
        lines += [f"{n} {s.status} {s.code or '-'}" for n, s in result.steps.items()]
        lines.append(f"순위: {saved.ranking if saved else '-'}")
        lines.append(f"종료 코드 {result.exit_code}")
        return lines
    lines.append(f"=== Career Support Agent {'Full AI Demo' if ai else 'Demo (mock dry-run)'} ===")
    lines.append(f"프로필: {PROFILE_REF.profile_id}(가상 프로필) / 공고 {len(run.jobs) if run else 0}건 / 모드: {'AI' if ai else 'mock'}")
    lines.append(f"demo_run_id: {result.demo_run_id} / pipeline_run_id: {result.pipeline_run_id or '(저장 전)'}")
    if run is not None:
        lines.append("")
        lines.append("[1~3] 직무 게이트 · 자격요건 · 최종 순위")
        lines += [f"  {line}" for line in render(run)[1:]]  # 제목 줄은 위에서 대신 보였다
        order_same = (run.ranking == EXPECTED_RANKING)
        lines.append(f"  참고: 정책 14장 기대 순위와 {'같음' if order_same else '다름'}" + ("" if ai else " (mock에서는 같아야 한다)"))
    lines.append("")
    lines.append(f"[4] 선택 공고: {result.selected_job_id or '-'} - {_step_line(result.steps['selection'])}")
    if result.fact_counts:
        c, i = result.fact_counts, result.fact_ids
        lines.append("[5] WriterInput 안전 분류 (개수와 id만)")
        lines.append(f"  사용 가능 사실 {c['usable_facts']}개 {i['usable_facts']} / 확인 필요 {c['to_confirm']}개 {i['to_confirm']} / 주장 금지 {c['do_not_claim']}개 {i['do_not_claim']}")
    else:
        lines.append(f"[5] {_step_line(result.steps['writer_input'])}")
    for n, kind in (("6", "cover_letter"), ("7", "portfolio")):
        step = result.steps[kind]
        line = f"[{n}] {_step_line(step)}"
        if step.status == "completed":
            line += " / 코드 검증 통과"
        lines.append(line)
        if step.path:
            rel = step.path.relative_to(REPO) if step.path.is_relative_to(REPO) else step.path
            lines.append(f"    확인용 md: {rel} (커밋하지 않는 파일)")
        elif kind in result.drafts and step.saved is None:
            lines.append("    저장하지 않았다 (mock)")
    lines.append("")
    lines.append("[8] 실행 요약")
    lines += [f"  {_step_line(s)}" for s in result.steps.values() if s.name in ("pipeline", "persistence")]
    if not ai:
        lines.append("  AI 호출 없음 (mock: 정답 fixture를 돌려주는 클라이언트). 실제 Gemini로는 --ai --accept-free-tier-terms로 실행한다")
    same = ai and len(set(result.models.values())) == 1
    for role, label in (("judge", "JUDGE(판정 모델)"), ("writer", "WRITER(생성 모델)")):
        if role in result.models:
            lines.append(f"  {label}: {result.models[role]}" + (" (두 역할이 같은 모델)" if same else ""))
    for task, label in TASKS:
        u = result.usage.get(task)
        if u is None:
            continue
        actual = ", ".join(u.actual) if u.actual else "알 수 없음"
        mismatch = " (불일치)" if u.actual and set(u.actual) != set(u.requested) else ""  # 실제 모델을 모르면 불일치가 아니다
        lines.append(f"  {label}: 호출 논리 {u.logical}회, 시도 {u.attempts}회, 캐시 적중 {u.cache_hits}, 토큰 입력 {u.input_tokens} / 출력 {u.output_tokens}"
                     f" (모델 요청 {', '.join(u.requested) or '-'} / 실제 {actual}{mismatch})")
    for model, (used, limit) in result.today.items():
        lines.append(f"  오늘 누적 {used}/{limit} (모델 {model}). 결제 연결 여부는 코드가 확인할 수 없다: AI Studio에서 확인하라.")
    lines.append(f"  종료 상태: 코드 {result.exit_code}")
    lines.append("이 시스템은 모든 공고에 자기소개서를 만들지 않는다. 평가한 공고 중 선택한 1건에만 만든다.")
    return lines


def render_drafts(result: PresentationResult) -> list[str]:
    lines = []
    for kind in KINDS:
        if kind in result.drafts:
            lines += [""] + render_draft(result.drafts[kind], None)
    return lines


# ---------------------------------------------------------------- 준비 (mock)

def _job_numbers() -> dict[str, int]:
    """공고 ID -> 입력할 번호 (샘플의 number). 번호가 겹치면 어느 공고인지 알 수 없으므로 시작하지 않는다."""
    numbers = {sample["job_id"]: sample["number"] for sample in ev.load_fixtures()[0]}
    if len(set(numbers.values())) != len(numbers):
        raise ScenarioError("샘플 공고의 번호가 겹친다")
    return numbers


def build_mock_plan(args) -> Plan:
    """--ai 없이: 네트워크를 쓰지 않는다. 게이트는 입력 파일이 있으면 규칙 + mock AI 자리, 없으면 fixture 결과."""
    jobs, profile, desired, _ = build_scenario("auto", args.gate_cases)
    samples, _, evidence, _ = ev.load_fixtures()
    answers = {k: v for k, v in json.loads(WRITER_MOCK_JSON.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    return Plan(
        mode="mock", demo_run_id=make_demo_run_id(), jobs=jobs, profile=profile, desired_family=desired,
        extract_client=make_extraction_mock(samples), evidence_client=make_evidence_mock(samples, evidence),
        writer_client=make_writer_mock(answers), results_dir=RESULTS_DIR, gate_cases=Path(args.gate_cases), task_links=args.task_links or "auto",
        posting_ref="synthetic-posting:{job_id} (fixture에서 만든 합성 공고, 실제 원문이 아니다)", numbers=_job_numbers(),
    )


# ---------------------------------------------------------------- 명령줄

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ai", action="store_true", help="실제 Gemini로 실행 (가상 프로필과 샘플 공고만)")
    p.add_argument("--accept-free-tier-terms", action="store_true", help="무료 티어 입력이 제품 개선에 쓰일 수 있음에 동의")
    p.add_argument("--scenario", choices=("full-ai-demo",), default="full-ai-demo", help="발표 시나리오 (선택지는 하나: 김하늘, 샘플 6건, 사용자가 고른 공고, 자소서 + 포트폴리오)")
    p.add_argument("--task-links", choices=("none", "fixture", "ai"), default=None,
                   help="초안에 쓸 업무 연결 후보. 기본(자동)=sample-3은 사람이 확정한 합성 고정본, 그 외 공고는 AI(호출 1회). "
                        "fixture=고정본만(후보가 sample-3로 제한됨), ai=항상 AI(--ai일 때만), none=없음")
    p.add_argument("--gate-cases", default=str(GATE_CASES_JSON), help="게이트 입력 JSON 경로")
    p.add_argument("--postings", default=str(ev.POSTINGS_JSON), help="공고 원문 JSON 경로 (--ai)")
    p.add_argument("--fresh-cache", action="store_true", help="이번 실행만의 새 캐시를 쓴다")
    p.add_argument("--cache-profile", action="store_true", help="프로필이 든 요청도 디스크에 캐시한다(가상 프로필 개발용)")
    p.add_argument("--show-drafts", action="store_true", help="요약 뒤에 초안 본문도 낸다")
    p.add_argument("--real-profile", action="store_true", help="구현하지 않음")
    for role in ("judge", "writer"):
        p.add_argument(f"--model-{role}", help=f".env의 GEMINI_MODEL_{role.upper()}를 덮어쓴다")
        p.add_argument(f"--rpm-{role}", type=int, help=f".env의 GEMINI_RPM_{role.upper()}를 덮어쓴다")
        p.add_argument(f"--rpd-{role}", type=int, help=f".env의 GEMINI_RPD_{role.upper()}를 덮어쓴다")
    p.add_argument("-q", "--quiet", action="store_true", help="단계 상태와 순위만 출력")
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if args.real_profile:
        p.error("--real-profile은 구현하지 않았다")
    if args.task_links == "ai" and not args.ai:
        p.error("--task-links ai는 --ai와 함께 쓴다")
    try:
        plan = build_ai_plan(args) if args.ai else build_mock_plan(args)
    except AiSetupError as e:
        print(e, file=sys.stderr)
        sys.exit(e.code)
    except (ScenarioError, FileNotFoundError) as e:
        print(f"실행할 수 없다: {e}", file=sys.stderr)
        sys.exit(EXIT_REFUSED)
    result = execute(plan, None if args.quiet else lambda line: print(line, flush=True))
    if not args.quiet:
        print()
    print("\n".join(render_result(result, args.quiet)))
    if args.show_drafts:
        print("\n".join(render_drafts(result)))
    if result.exit_code == EXIT_QUOTA:
        print("호출 한도 소진으로 멈췄다. 태평양 시간 자정 이후에 이어서 하라.", file=sys.stderr)
    sys.exit(result.exit_code)


def build_ai_plan(args) -> Plan:
    """--ai: 게이트·자격요건은 JUDGE 모델, 자소서·포트폴리오(와 --task-links ai)는 WRITER 모델로 부른다.

    동의, 설정, 입력 파일, **모델별** 하루 호출 예산(최대 시도 수)을 모두 점검한 뒤에만 클라이언트를 돌려준다. 하나라도 안 되면 아무것도 보내지 않는다.
    게이트 입력 파일이 없으면 안내 문구와 동의 점검에 들어가기 전에 멈춘다 (예산 계산에 필요하다).
    """
    demo_run_id = make_demo_run_id()
    samples, _, _, _ = ev.load_fixtures()
    ids = [s["job_id"] for s in samples]
    logical, gate_attempts, elig_attempts, judge_calls = gate_ai_budget(args, len(ids))  # 입력 파일이 없으면 ScenarioError, 샘플 6건 밖이면 AiSetupError
    writer_calls = 3 * (1 + MAX_RETRIES)  # 자소서 + 포트폴리오 + 연결 후보. 어떤 공고가 선택될지 호출 전에 모르므로 연결 후보는 항상 포함한다. 각각 재시도 포함
    notice = ev.FREE_TIER_NOTICE + (
        f"\n예상 호출(최대 시도, 재시도 {MAX_RETRIES}회 포함): JUDGE 게이트 최대 논리 {logical}회(시도 {gate_attempts}) + 자격요건 {elig_attempts} = {judge_calls}회, "
        f"WRITER {writer_calls}회. 두 역할이 같은 모델이면 합계 {judge_calls + writer_calls}회를 그 모델의 남은 한도와 비교한다. 게이트는 공고의 업무 문장만 보낸다.")
    loaded: dict[str, str] = {}

    def load_inputs():
        try:
            loaded.update(ev.load_postings(Path(args.postings)))
        except FileNotFoundError as e:
            raise AiSetupError(f"실행할 수 없다: {e}") from None
        if missing := [i for i in ids if i not in loaded]:
            raise AiSetupError(f"공고 원문이 없는 표본: {missing}")

    roles = prepare_roles(args, notice=notice, judge_calls=judge_calls, writer_calls=writer_calls, results_dir=RESULTS_DIR, env_loader=load_env,
                          client_cls=GeminiClient, before_client=load_inputs, usage_run_id=demo_run_id)
    gate_client = extra_cached_client(roles.judge, args, results_dir=RESULTS_DIR, cache_dir_name="gate")
    jobs, profile, desired, _ = build_scenario("ai", args.gate_cases, loaded, gate_client)
    postings_file = Path(args.postings)
    postings_file = postings_file.relative_to(REPO) if postings_file.is_relative_to(REPO) else postings_file
    judge, writer = roles.judge.settings, roles.writer.settings
    return Plan(
        mode="ai", demo_run_id=demo_run_id, jobs=jobs, profile=profile, desired_family=desired,
        extract_client=roles.judge.client, evidence_client=roles.judge.client, writer_client=roles.writer.client, results_dir=RESULTS_DIR,
        gate_client=gate_client, usage=roles.usage, gate_cases=Path(args.gate_cases), task_links=args.task_links or "auto",
        cache_mode="fresh" if args.fresh_cache else ("disk" if args.cache_profile else "memory"),
        models={"judge": judge.model, "writer": writer.model}, limits={judge.model: judge.rpd, writer.model: writer.rpd},
        posting_ref=f"{postings_file.as_posix()}#{{job_id}}", numbers=_job_numbers(),
    )


if __name__ == "__main__":
    main()
