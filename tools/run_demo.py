"""시연 시나리오 실행: 김하늘(가상 프로필) + 샘플 6건을 게이트 -> 자격요건 -> 순위 정렬까지 한 번에 돌린다 (이슈 #7, data-contract 3장).

사용법 (저장소 루트에서):
    python -m tools.run_demo                      # mock (기본): 네트워크를 쓰지 않는다
    python -m tools.run_demo -q                   # 공고 id·순위·코드만 낸다
    python -m tools.run_demo --gate injected      # 게이트 결과를 fixture 값으로 넣는다 (게이트 입력 파일이 없을 때의 기본)
    python -m tools.run_demo --save-profile-evidence   # 저장 파일에 프로필 원문 인용도 넣는다 (가상 프로필 한정, 정책 16장)
    python -m tools.run_demo --select sample-3    # 고른 공고를 자소서·포트폴리오 Agent에 넘길 파일(selected_job.v1 초안)로 저장한다
    python -m tools.run_demo --select sample-3 --save-profile-evidence   # 파일에 프로필 인용도 담는다 (가상 프로필 한정)
    python -m tools.run_demo --select sample-3 --draft cover_letter   # 고른 공고로 자소서(cover_letter) 또는 포트폴리오(portfolio) 초안을 mock으로 만든다
    python -m tools.run_demo --select sample-3 --draft portfolio --save-draft   # 초안을 data/results/drafts/에 저장한다 (가상 프로필 한정)
    python -m tools.run_demo --select sample-3 --draft cover_letter --ai --accept-free-tier-terms --model ... --rpm ... --rpd ...
                                                  # 초안 작성 1회만 실제 Gemini. 자격요건과 게이트는 mock (가상 프로필과 샘플 공고만)
    python -m tools.run_demo --ai --accept-free-tier-terms [--fresh-cache] [--model ...]
                                                  # 자격요건 단계만 실제 Gemini. 게이트의 AI 자리는 mock이다 (실제 연결은 이슈 #12)

게이트: data/sample-jobs/gate_sample_cases.json(커밋하지 않는 파일)이 있으면 실제 게이트 규칙을 mock AI 자리로 돌린다. 없으면 fixture의 게이트 결과를 넣는다.
공고 수집·항목 분리는 이 도구의 범위 밖이다. 사전에 준비한 입력을 읽는다.
점수는 계산하지 않는다. 순위는 ranking_group -> verdict -> 입력 순서다 (정책 14장).
결과는 data/results/pipeline/<run_id>.json에 저장한다(커밋하지 않음). 기본으로는 프로필 원문 인용을 빼고 저장한다.
선택 파일은 data/results/selection/<run_id>-<job_id>.json이다. 평가를 마치지 못한 공고는 선택할 수 없고, 불일치·미충족 공고는 경고 문구와 함께 선택할 수 있다.
--ai의 안내, 동의, 하루 호출 예산 점검은 tools.eval_eligibility와 같다. 공고 원문은 data/sample-jobs/eligibility_sample_postings.json에서 읽는다.
--draft는 기본 mock이다. --draft --ai는 초안 작성 호출 1회(재시도 포함 최대 4회)만 실제 Gemini로 보내고 자격요건과 게이트는 mock이다 (docs/policies/application-writing.md). 초안은 기본으로 저장하지 않고, 입력(WriterInput)은 어떤 경우에도 저장하지 않는다.
화면에는 순위, 판정, 코드가 만든 사유 문구를 낸다. 프로필 인용, 공고 원문 전체, 모델 출력과 오류 내용은 출력하지 않는다.
"""
import argparse
import json
import re
import sys
from pathlib import Path

from core.job_gate_mock import make_label_mock
from agents.application_writer import KINDS, SECTIONS, ApplicationDraft, DraftError, make_writer_mock, write_application
from agents.task_linker import build_links, link_tasks
from core.llm_client import MAX_RETRIES, GeminiClient, LLMQuotaExceeded, LLMStageError, load_env
from core.llm_mock import make_evidence_mock, make_extraction_mock, synthetic_posting
from core.pipeline import ScenarioJob, make_run_id, run_pipeline
from core.pipeline_run import PipelineRun, ProfileRef
from core.schemas import DISPLAY_LABELS
from core.selection import JOB_ID_PATTERN, SelectedJob, SelectionRefused, select_job
from core.writer_input import JobInfo, WriterInputError, build_writer_input
from tools import eval_eligibility as ev
from tools.ai_common import AiSetupError, prepare_session

REPO = Path(__file__).resolve().parents[1]
RESULTS_DIR = ev.RESULTS_DIR
GATE_CASES_JSON = REPO / "data" / "sample-jobs" / "gate_sample_cases.json"
TASK_LINKS_JSON = REPO / "tests" / "fixtures" / "task_links_sample3.json"  # 사람이 확정한 가상 프로필의 연결 후보 (합성 fixture, 정책 5장 D18)
WRITER_MOCK_JSON = REPO / "tests" / "fixtures" / "writer_mock_sample3.json"
PROFILE_REF = ProfileRef(profile_id="kim-haneul", is_synthetic=True)  # fixture의 가상 프로필 (김하늘)
EXIT_REFUSED = 2


class ScenarioError(Exception):
    """시나리오 입력이 틀려 실행을 시작할 수 없다. 메시지에는 공고 원문을 넣지 않는다."""


def _label(code):
    return DISPLAY_LABELS.get(code, code) if code else "-"


# ---------------------------------------------------------------- 시나리오 만들기

def build_scenario(gate_source: str = "auto", gate_cases_path: Path | None = None, posting_texts: dict[str, str] | None = None):
    """(공고 목록, 프로필, 희망 직무군, 실제 게이트 방식). 입력이 어긋나면 ScenarioError.

    gate_source: injected(fixture의 게이트 결과), mock(게이트 규칙 + mock AI 자리), auto(입력 파일이 있으면 mock, 없으면 injected).
    posting_texts를 주면(--ai) 게이트 입력의 직무명이 그 공고 원문에 들어 있는지도 확인해 두 파일이 같은 공고인지 본다.
    """
    gate_cases_path = Path(gate_cases_path or GATE_CASES_JSON)
    samples, profile, _, gates = ev.load_fixtures()
    fixture = ev._read_json(ev.GATE_FIXTURE)
    desired, expected = fixture["desired_family"], {c["no"]: c for c in fixture["cases"]}
    if gate_source == "auto":
        gate_source = "mock" if gate_cases_path.exists() else "injected"
    texts = posting_texts or {s["job_id"]: synthetic_posting(s) for s in samples}

    raw = {}
    if gate_source == "mock":
        if not gate_cases_path.exists():
            raise ScenarioError(f"게이트 입력 파일이 없다: {gate_cases_path} (팀 Drive에서 받아 넣는다. 커밋하지 않는 파일)")
        raw = {c["no"]: c for c in ev._read_json(gate_cases_path)["cases"]}

    jobs = []
    for s in samples:
        job_id, no = s["job_id"], s["number"]
        exp = expected[no]
        base = dict(job_id=job_id, posting_text=texts[job_id], job_label=f"{no}번 {exp['company']}")
        if gate_source == "injected":
            jobs.append(ScenarioJob(**base, gate=gates[job_id]))
            continue
        case = raw.get(no)
        if case is None:
            raise ScenarioError(f"게이트 입력 파일에 {no}번 공고가 없다")
        if case["company"] != exp["company"]:
            raise ScenarioError(f"{no}번: 게이트 입력 파일의 회사가 기대 판정 fixture와 다르다")
        kept = [it for it in case["items"] if not it.get("exclude")]
        if len(kept) != len(exp["item_labels"]):
            raise ScenarioError(f"{no}번: 업무 항목 수가 기대 판정 fixture와 다르다")
        if posting_texts and case["job_title"] not in texts[job_id]:
            raise ScenarioError(f"{no}번: 게이트 입력의 직무명이 공고 원문({job_id})에 없다. 두 파일이 같은 공고가 아니다")
        posting = {"job_title": case["job_title"], "position": case["position"], "items": case["items"]}
        jobs.append(ScenarioJob(**base, gate_posting=posting, gate_judge=make_label_mock(exp["item_labels"])))
    return jobs, profile, desired, gate_source


# ---------------------------------------------------------------- 화면 출력

def render(run: PipelineRun, quiet: bool = False) -> list[str]:
    """순위와 사유를 줄 목록으로 만든다. 사유는 코드가 만든 문구(data-contract 2-7)다."""
    info = run.run_info
    ranked = [j for j in run.jobs if j.rank is not None]
    ranked.sort(key=lambda j: j.rank)
    unranked = [j for j in run.jobs if j.rank is None]
    lines = []
    if quiet:
        lines.append(f"순위: {run.ranking}")
        lines += [f"{j.rank}위 {j.job_id} {j.gate.ranking_group} {j.eligibility.verdict}{' 참고용' if j.reference_only else ''}" for j in ranked]
        lines += [f"- {j.job_id} {j.evaluation_status} {j.failure_code}" for j in unranked]
        lines.append(f"완료 {run.counts['completed']}, 실패 {run.counts['failed']}, 안 함 {run.counts['not_run']}")
        return lines
    who = "가상 프로필" if run.profile.is_synthetic else "프로필"
    model = f"AI {info.model_id}" if info.eligibility_mode == "ai" else "mock"
    gate = {"injected": "fixture 결과 사용", "mock": "규칙 + mock AI 자리"}[info.gate_mode]
    lines.append(f"=== 시연: {run.profile.profile_id}({who}) / 공고 {len(run.jobs)}건 / 게이트: {gate} / 자격요건: {model} / 실행 {run.run_id} ===")
    lines.append("점수는 계산하지 않았다. 순위는 직무 그룹 -> 자격요건 판정 -> 입력 순서다.")
    notice_shown = False
    for job in ranked:
        if job.reference_only and not notice_shown:
            lines.append(f"--- {DISPLAY_LABELS['reference_only_notice']} ({DISPLAY_LABELS['reference_only']}) ---")
            notice_shown = True
        res, gate_result = job.eligibility, job.gate
        tag = f" [{DISPLAY_LABELS['reference_only']}]" if job.reference_only else ""
        lines.append(f"{job.rank}위 [{job.job_id}] {job.job_label or ''}{tag}")
        lines.append(f"    직무: {_label(gate_result.result)} / {_label(gate_result.ranking_group)} - {gate_result.summary}")
        # 조건부 적합의 사유 문구는 이미 "확인 필요 k개"로 시작한다. 그 밖의 판정에서만 확인 필요 개수를 덧붙인다
        extra = f" (확인 필요 {res.needs_check_count}개)" if res.needs_check_count and res.verdict != "conditional" else ""
        lines.append(f"    자격요건: {_label(res.verdict)} - {res.summary}{extra}")
    if unranked:
        lines.append("--- 평가하지 못한 공고 (순위에 넣지 않음) ---")
        lines += [f"[{j.job_id}] {j.job_label or ''}: {j.summary}" for j in unranked]
    lines.append(f"완료 {run.counts['completed']}, 실패 {run.counts['failed']}, 안 함 {run.counts['not_run']}")
    return lines


# ---------------------------------------------------------------- 저장

def save_run(run: PipelineRun, directory: Path, save_profile_evidence: bool = False) -> tuple[Path, PipelineRun]:
    """실행 결과를 directory/<run_id>.json에 쓴다. 기본은 프로필 원문 인용을 뺀다 (정책 16장).

    인용을 넣는 것은 가상 프로필일 때만 허용한다. 같은 이름의 파일이 있으면 덮어쓰지 않고 새 run_id를 뽑는다.
    저장한 PipelineRun(run_id가 바뀌었을 수 있다)과 경로를 돌려준다.
    """
    if save_profile_evidence and not run.profile.is_synthetic:
        raise ValueError("프로필 인용은 가상 프로필일 때만 저장할 수 있다 (정책 16장)")
    saved = run if save_profile_evidence else run.without_profile_evidence()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for _ in range(10):
        path = directory / f"{saved.run_id}.json"
        try:
            with path.open("x", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(json.loads(saved.model_dump_json()), ensure_ascii=False, indent=2) + "\n")
            return path, saved
        except FileExistsError:
            saved = saved.model_copy(update={"run_id": make_run_id()})
    raise OSError("새 run_id를 뽑지 못했다")


def save_selection(selected: SelectedJob, directory: Path) -> Path:
    """선택 파일을 directory/<run_id>-<job_id>.json에 쓴다. 같은 이름이 있으면 덮어쓰지 않는다(FileExistsError)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{selected.run_id}-{selected.job_id}.json"
    with path.open("x", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(json.loads(selected.model_dump_json()), ensure_ascii=False, indent=2) + "\n")
    return path


def load_selection(path: Path) -> SelectedJob:
    """저장한 선택 파일을 읽고 다시 검증한다. 경고는 읽을 때 다시 계산하므로 파일에서 지워도 사라지지 않는다."""
    return SelectedJob.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_run(path: Path) -> PipelineRun:
    """저장한 실행 결과를 읽고 다시 검증한다."""
    return PipelineRun.model_validate_json(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 실행

def _check_select(args, jobs) -> str | None:
    """--select 값이 입력 공고에 있는지 실행 전에 확인한다(호출을 쓰기 전에 거른다). 문제가 있으면 안내 문구."""
    if args.select is None:
        return None
    if not re.match(JOB_ID_PATTERN, args.select):
        return "--select의 job_id 형식이 올바르지 않다"
    if args.select not in [j.job_id for j in jobs]:
        return f"--select {args.select!r}는 이번 공고 목록에 없다: {[j.job_id for j in jobs]}"
    return None


def _select(saved: PipelineRun, args, posting_ref) -> int:
    """고른 공고를 선택 파일로 저장하고 경고를 보인다. 저장한 실행 결과(saved)에서 고르므로 run_id가 저장 파일과 같다."""
    try:
        selected = select_job(saved, args.select, include_profile_evidence=args.save_profile_evidence, posting_ref=posting_ref)
        path = save_selection(selected, RESULTS_DIR / "selection")
    except SelectionRefused as e:
        print(f"선택하지 않았다: {e}", file=sys.stderr)
        return EXIT_REFUSED
    kind = "프로필 인용 포함" if selected.profile_evidence_included else "프로필 인용 제외"
    print(f"선택: {selected.job_id} -> {path.relative_to(REPO) if path.is_relative_to(REPO) else path} ({kind}, 커밋하지 않는 파일, 초안 형식)")
    if not selected.profile_evidence_included:
        print("  인용은 담지 않았다. 가상 프로필의 인용까지 넘기려면 --save-profile-evidence를 함께 준다.")
    for notice in selected.selection_notices:
        print(f"  경고: {notice}")
    return 0


def _job_info(job_id: str, gate_cases_path: Path | None = None) -> JobInfo:
    """초안 입력의 공고 내용. 회사명은 fixture에서, 직무명·업무 항목은 게이트 입력 파일이 있으면 거기서 읽는다(없으면 비운다)."""
    samples, _, _, _ = ev.load_fixtures()
    no = next(s["number"] for s in samples if s["job_id"] == job_id)
    company = next(c["company"] for c in ev._read_json(ev.GATE_FIXTURE)["cases"] if c["no"] == no)
    path = Path(gate_cases_path or GATE_CASES_JSON)
    case = next((c for c in ev._read_json(path)["cases"] if c["no"] == no), None) if path.exists() else None
    if case is None:
        return JobInfo(company_name=company)
    return JobInfo(company_name=company, job_title=case.get("job_title"), position=case.get("position"),
                   main_tasks=[it["text"] for it in case["items"] if not it.get("exclude")])


def save_draft(draft: ApplicationDraft, directory: Path) -> Path:
    """초안을 directory/<run_id>-<job_id>-<kind>.json에 쓴다. 가상 프로필일 때만 쓰고, 같은 이름이 있으면 덮어쓰지 않는다 (정책 5장)."""
    if not draft.is_synthetic:
        raise ValueError("초안은 가상 프로필일 때만 저장할 수 있다 (정책 5장)")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{draft.run_id}-{draft.job_id}-{draft.kind}.json"
    with path.open("x", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(json.loads(draft.model_dump_json()), ensure_ascii=False, indent=2) + "\n")
    return path


KIND_LABEL = {"cover_letter": "자기소개서", "portfolio": "포트폴리오"}


def draft_markdown(draft: ApplicationDraft) -> str:
    """사람이 전체를 읽는 확인용 md. 화면에 내는 초안과 같은 내용이다(소제목, 문단, 불릿, 근거 사실 id, 확인 필요, 경고, 검증 한계)."""
    ids = lambda xs: f"*(근거 사실 {', '.join(xs)})*"  # noqa: E731
    lines = [f"# {KIND_LABEL[draft.kind]} 초안: 공고 {draft.job_id}", "",
             f"> writer: {draft.meta.mode} ({draft.meta.model}) · 프롬프트 v{draft.meta.prompt_version} · 실행 {draft.run_id} · 가상 프로필 {draft.profile_id}",
             f"> {draft.banner}", f"> 검증 한계: {draft.verification}", ""]
    for sec in draft.sections:
        lines += [f"## {sec.heading}", "", f"*섹션: {SECTIONS[draft.kind][sec.section]} (`{sec.section}`)*", ""]
        for para in sec.paragraphs:
            lines += [para.text, "", ids(para.fact_ids), ""]
        lines += [f"- {b.text} {ids(b.fact_ids)}" for b in sec.bullets]
        if sec.bullets:
            lines.append("")
    lines += [f"## 확인 필요 {len(draft.to_confirm)}개 (초안에서 충족한 것처럼 쓰지 않았다)", ""]
    lines += [f"- {c.confirm_id} {c.text} → {c.question}" for c in draft.to_confirm]
    if draft.notices:
        lines += [""] + [f"**경고: {n}**" for n in draft.notices]
    return "\n".join(lines) + "\n"


def save_draft_md(draft: ApplicationDraft, directory: Path) -> Path:
    """확인용 md를 directory/<run_id>-<job_id>-<kind>.md에 쓴다. 가상 프로필일 때만 쓰고, 같은 이름이 있으면 덮어쓰지 않는다 (정책 5장 D13)."""
    if not draft.is_synthetic:
        raise ValueError("초안은 가상 프로필일 때만 저장할 수 있다 (정책 5장)")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{draft.run_id}-{draft.job_id}-{draft.kind}.md"
    with path.open("x", encoding="utf-8", newline="\n") as f:
        f.write(draft_markdown(draft))
    return path


def render_draft(draft: ApplicationDraft, saved: Path | None, md: Path | None = None) -> list[str]:
    """초안을 화면에 낸다. 첫 줄에 mock / ai를 표시한다. 확인 필요 목록과 경고 문구는 코드가 붙인 것이다."""
    kind = KIND_LABEL[draft.kind]
    lines = [f"=== 초안: {kind} / writer: {draft.meta.mode} ({draft.meta.model}) / 공고 {draft.job_id} / 프롬프트 v{draft.meta.prompt_version} ===",
             draft.banner, f"검증 한계: {draft.verification}"]
    for s in draft.sections:
        lines += [f"[{s.section}] {s.heading}"]
        lines += [f"    {p.text} (사실 {', '.join(p.fact_ids)})" for p in s.paragraphs]
        lines += [f"    - {b.text} (사실 {', '.join(b.fact_ids)})" for b in s.bullets]
    lines.append(f"--- 확인 필요 {len(draft.to_confirm)}개 (초안에서 충족한 것처럼 쓰지 않았다) ---")
    lines += [f"{c.confirm_id} {c.text} -> {c.question}" for c in draft.to_confirm]
    lines += [f"경고: {n}" for n in draft.notices]
    rel = lambda path: path.relative_to(REPO) if path.is_relative_to(REPO) else path  # noqa: E731
    lines.append(f"저장: {rel(saved)}" if saved else "저장하지 않았다 (--save-draft로 저장)")
    if md:
        lines.append(f"확인용 md: {rel(md)} (커밋하지 않는 파일)")
    return lines


def _task_links(args, profile, selected, info):
    """--task-links에 따라 업무 연결 후보를 만든다. none은 없음, fixture는 사람이 확정한 합성 fixture, ai는 호출 1회 (결과는 저장하지 않는다)."""
    mode = args.task_links
    if mode == "none":
        return []
    if mode == "fixture":
        entry = {k: v for k, v in json.loads(TASK_LINKS_JSON.read_text(encoding="utf-8")).items() if not k.startswith("_")}.get(args.select)
        if entry is None:
            raise ValueError(f"고정된 연결 후보가 없다: {args.select}")
        return build_links(profile, entry["tasks"], {"links": entry["links"]})
    return link_tasks(profile, info.main_tasks, args.writer_client, allow_external_profile=True,
                      is_synthetic=selected.profile.is_synthetic, job_id=args.select)


def _draft(run: PipelineRun, profile, args) -> int:
    """고른 공고의 초안을 mock으로 만들어 보인다. 입력(WriterInput)은 메모리에만 있고 저장하지 않는다."""
    try:
        selected = select_job(run, args.select, include_profile_evidence=True)
        info = _job_info(args.select, args.gate_cases)
        wi = build_writer_input(selected, profile, info, task_links=_task_links(args, profile, selected, info))
        client = getattr(args, "writer_client", None)
        if client is None:  # mock: fixture의 초안을 돌려준다
            answers = {k: v for k, v in json.loads(WRITER_MOCK_JSON.read_text(encoding="utf-8")).items() if not k.startswith("_")}
            if args.select not in answers:
                print(f"초안을 만들 수 없다: mock 초안이 {sorted(answers)}에만 있다", file=sys.stderr)
                return EXIT_REFUSED
            client = make_writer_mock(answers)
        draft = write_application(wi, client, args.draft, allow_external_profile=getattr(args, "writer_client", None) is not None, mode=getattr(args, "writer_mode", "mock"))
        path = save_draft(draft, RESULTS_DIR / "drafts") if args.save_draft else None
        md = save_draft_md(draft, RESULTS_DIR / "drafts") if args.save_draft or draft.meta.mode == "ai" else None  # 실제 AI 초안은 플래그 없이도 (정책 5장 D13)
    except (SelectionRefused, WriterInputError, DraftError, LLMStageError, LLMQuotaExceeded, PermissionError, ValueError, FileExistsError) as e:
        print(f"초안을 만들지 못했다: {type(e).__name__}: {e}", file=sys.stderr)  # 메시지에는 초안·프로필 내용이 없다
        return EXIT_REFUSED
    print("\n".join(render_draft(draft, path, md)))
    return 0


def _finish(run: PipelineRun, args, posting_ref=lambda job_id: None) -> int:
    print("\n".join(render(run, args.quiet)))
    try:
        path, saved = save_run(run, RESULTS_DIR / "pipeline", args.save_profile_evidence)
    except ValueError as e:
        print(f"저장하지 않았다: {e}", file=sys.stderr)
        return EXIT_REFUSED
    kind = "프로필 인용 포함" if saved.profile_evidence_included else "프로필 인용 제외"
    print(f"저장: {path.relative_to(REPO) if path.is_relative_to(REPO) else path} ({kind}, 커밋하지 않는 파일)")
    counts = saved.counts
    if args.select and _select(saved, args, posting_ref(args.select)):
        return EXIT_REFUSED
    if args.draft and (draft_code := _draft(run, args.draft_profile, args)):
        return draft_code
    if counts["not_run"]:
        return 3
    return 0 if counts["failed"] == 0 else 1


def run_mock(args) -> int:
    try:
        jobs, profile, desired, _ = build_scenario(args.gate, args.gate_cases)
    except (ScenarioError, FileNotFoundError) as e:
        print(f"실행할 수 없다: {e}", file=sys.stderr)
        return EXIT_REFUSED
    if problem := _check_select(args, jobs):
        print(f"실행할 수 없다: {problem}", file=sys.stderr)
        return EXIT_REFUSED
    samples, _, evidence, _ = ev.load_fixtures()
    run = run_pipeline(jobs, profile, PROFILE_REF, make_extraction_mock(samples), make_evidence_mock(samples, evidence),
                       desired_family=desired)
    args.draft_profile = profile  # 초안 입력에만 쓴다(저장하지 않는다)
    return _finish(run, args, lambda job_id: f"synthetic-posting:{job_id} (fixture에서 만든 합성 공고, 실제 원문이 아니다)")


def run_ai(args) -> int:
    """자격요건 단계만 실제 Gemini로 돌린다. 게이트의 AI 자리는 mock이다."""
    try:
        samples, profile, _, _ = ev.load_fixtures()
        session = ev.prepare_ai(args, [s["job_id"] for s in samples], RESULTS_DIR)
        jobs, profile, desired, _ = build_scenario(args.gate, args.gate_cases, session.postings)
    except ev.AiSetupError as e:
        print(e, file=sys.stderr)
        return e.code
    except (ScenarioError, FileNotFoundError) as e:
        print(f"실행할 수 없다: {e}", file=sys.stderr)
        return EXIT_REFUSED
    if problem := _check_select(args, jobs):
        print(f"실행할 수 없다: {problem}", file=sys.stderr)  # 호출을 쓰기 전에 거른다
        return EXIT_REFUSED
    cache_mode = "fresh" if args.fresh_cache else ("disk" if args.cache_profile else "memory")
    run = run_pipeline(jobs, profile, PROFILE_REF, session.client, session.client, desired_family=desired,
                       allow_external_profile=True, usage=session.usage, cache_mode=cache_mode)
    postings_file = Path(args.postings)
    postings_file = postings_file.relative_to(REPO) if postings_file.is_relative_to(REPO) else postings_file
    code = _finish(run, args, lambda job_id: f"{postings_file.as_posix()}#{job_id}")
    t, after = session.usage.run_totals(), session.usage.calls_today(session.settings.model)
    print(f"이번 실행({session.usage.run_id}) 호출 {t['calls']}회(실패 {t['failed']}), 캐시 적중 {t['cache_hits']}, 토큰 입력 {t['input_tokens']} / 출력 {t['output_tokens']}. "
          f"오늘 누적 {after}/{session.settings.rpd} (모델 {session.settings.model}). 결제 연결 여부는 코드가 확인할 수 없다: AI Studio에서 확인하라.")
    if code == 3:
        print("한도 소진으로 중간에 멈췄다. 태평양 시간 자정 이후에 이어서 하라.", file=sys.stderr)
    return code


def run_draft_ai(args) -> int:
    """초안 작성 1회만 실제 Gemini로 보낸다. 자격요건과 게이트는 mock이다(가상 프로필과 샘플 공고만 나간다, 정책 5장).

    동의, 설정, 하루 예산(재시도 포함 최대 시도)을 점검한 뒤에만 보낸다. 하나라도 안 되면 아무것도 보내지 않는다.
    """
    try:
        build_scenario(args.gate, args.gate_cases)
        session = prepare_session(args, notice=ev.FREE_TIER_NOTICE, needed_calls=(1 + MAX_RETRIES) * (2 if args.task_links == "ai" else 1), results_dir=RESULTS_DIR,
                                  cache_dir_name="extract", env_loader=load_env, client_cls=GeminiClient)
    except AiSetupError as e:
        print(e, file=sys.stderr)
        return e.code
    except (ScenarioError, FileNotFoundError) as e:
        print(f"실행할 수 없다: {e}", file=sys.stderr)
        return EXIT_REFUSED
    args.writer_client, args.writer_mode = session.client, "ai"
    print(f"=== 초안 작성 실제 AI: {session.settings.model}, 실행 {session.usage.run_id}, 오늘 이미 사용 {session.calls_before}/{session.settings.rpd} "
          f"(자격요건과 게이트는 mock) ===")
    code = run_mock(args)
    t, after = session.usage.run_totals(), session.usage.calls_today(session.settings.model)
    print(f"이번 실행({session.usage.run_id}) 호출 {t['calls']}회(실패 {t['failed']}), 캐시 적중 {t['cache_hits']}, 토큰 입력 {t['input_tokens']} / 출력 {t['output_tokens']}. "
          f"오늘 누적 {after}/{session.settings.rpd} (모델 {session.settings.model}). 결제 연결 여부는 코드가 확인할 수 없다: AI Studio에서 확인하라.")
    return code


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ai", action="store_true", help="자격요건 단계를 실제 Gemini로 실행 (가상 프로필과 샘플 공고만)")
    p.add_argument("--accept-free-tier-terms", action="store_true", help="무료 티어 입력이 제품 개선에 쓰일 수 있음에 동의")
    p.add_argument("--gate", choices=("auto", "injected", "mock"), default="auto", help="게이트 결과를 만드는 방식")
    p.add_argument("--gate-cases", default=str(GATE_CASES_JSON), help="게이트 입력 JSON 경로")
    p.add_argument("--select", metavar="JOB_ID", help="고른 공고를 자소서·포트폴리오 Agent에 넘길 파일(초안 형식)로 저장한다")
    p.add_argument("--draft", choices=KINDS, help="고른 공고(--select)의 자소서 또는 포트폴리오 초안을 만든다 (mock만)")
    p.add_argument("--task-links", choices=("none", "fixture", "ai"), default="none",
                   help="초안에 주요 업무 연결 후보를 더한다. none=없음(기본), fixture=사람이 확정한 합성 fixture, ai=연결 후보 호출 1회(--ai일 때만, 결과는 저장하지 않는다)")
    p.add_argument("--save-draft", action="store_true", help="초안을 data/results/drafts/에 저장한다 (가상 프로필 한정)")
    p.add_argument("--save-profile-evidence", action="store_true", help="저장 파일에 프로필 원문 인용을 넣는다 (가상 프로필 한정)")
    p.add_argument("--cache-profile", action="store_true", help="근거 찾기 결과를 디스크에 캐시한다(가상 프로필 개발용)")
    p.add_argument("--fresh-cache", action="store_true", help="이번 실행만의 공고 추출 캐시를 쓴다(모델 비교용)")
    p.add_argument("--real-profile", action="store_true", help="구현하지 않음")
    p.add_argument("--model", help=".env의 GEMINI_MODEL을 덮어쓴다")
    p.add_argument("--rpm", type=int, help=".env의 GEMINI_RPM을 덮어쓴다")
    p.add_argument("--rpd", type=int, help=".env의 GEMINI_RPD를 덮어쓴다")
    p.add_argument("--postings", default=str(ev.POSTINGS_JSON), help="공고 원문 JSON 경로 (--ai)")
    p.add_argument("-q", "--quiet", action="store_true", help="공고 id·순위·코드만 출력")
    args = p.parse_args(argv)
    args.draft_profile = None
    if args.draft and not args.select:
        p.error("--draft는 --select와 함께 쓴다")
    if args.save_draft and not args.draft:
        p.error("--save-draft는 --draft와 함께 쓴다")
    if args.task_links != "none" and not args.draft:
        p.error("--task-links는 --draft와 함께 쓴다")
    if args.task_links == "ai" and not args.ai:
        p.error("--task-links ai는 --ai와 함께 쓴다")
    if args.draft and args.real_profile:
        p.error("--real-profile은 구현하지 않았다")
    if args.draft and args.ai:
        sys.exit(run_draft_ai(args))
    if args.ai or args.real_profile:
        sys.exit(run_ai(args))
    sys.exit(run_mock(args))


if __name__ == "__main__":
    main()
