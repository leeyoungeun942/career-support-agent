"""발표용 실행(tools.run_presentation)과 시연 도구(tools.run_demo)가 함께 쓰는 함수 (이슈 #24, 정책 presentation.md 9장 D11).

tools.run_demo에서 **순수하게 옮겨 온** 것이다: 시나리오 만들기, 결과 화면, 결과 저장·재로딩, 초안 입력의 공고 정보, 초안 확인용 md와 화면,
게이트 AI 호출 예산. 동작은 옮기기 전과 같고, tools.run_demo는 같은 이름으로 다시 가져와 쓴다(기존 테스트가 그 이름을 쓴다).
이 모듈은 tools.run_demo를 가져오지 않는다.
"""
import json
from pathlib import Path

from agents.application_writer import SECTIONS, ApplicationDraft
from core.job_gate_ai import make_llm_judge
from core.job_gate_mock import make_label_mock
from core.llm_client import MAX_RETRIES
from core.llm_mock import synthetic_posting
from core.pipeline import ScenarioJob, make_run_id
from core.pipeline_run import PipelineRun, ProfileRef
from core.schemas import DISPLAY_LABELS
from core.writer_input import JobInfo
from tools import eval_eligibility as ev
from tools import eval_gate as eg

REPO = Path(__file__).resolve().parents[1]


GATE_CASES_JSON = REPO / "data" / "sample-jobs" / "gate_sample_cases.json"


PROFILE_REF = ProfileRef(profile_id="kim-haneul", is_synthetic=True)  # fixture의 가상 프로필 (김하늘)


class ScenarioError(Exception):
    """시나리오 입력이 틀려 실행을 시작할 수 없다. 메시지에는 공고 원문을 넣지 않는다."""


def _label(code):
    return DISPLAY_LABELS.get(code, code) if code else "-"


def build_scenario(gate_source: str = "auto", gate_cases_path: Path | None = None, posting_texts: dict[str, str] | None = None, gate_client=None):
    """(공고 목록, 프로필, 희망 직무군, 실제 게이트 방식). 입력이 어긋나면 ScenarioError.

    gate_source: injected(fixture의 게이트 결과), mock(게이트 규칙 + mock AI 자리), ai(게이트 규칙 + 실제 AI 자리, gate_client 필요),
    auto(입력 파일이 있으면 mock, 없으면 injected).
    posting_texts를 주면(--ai) 게이트 입력의 직무명이 그 공고 원문에 들어 있는지도 확인해 두 파일이 같은 공고인지 본다.
    """
    gate_cases_path = Path(gate_cases_path or GATE_CASES_JSON)
    samples, profile, _, gates = ev.load_fixtures()
    fixture = ev._read_json(ev.GATE_FIXTURE)
    desired, expected = fixture["desired_family"], {c["no"]: c for c in fixture["cases"]}
    if gate_source == "auto":
        gate_source = "mock" if gate_cases_path.exists() else "injected"
    texts = posting_texts or {s["job_id"]: synthetic_posting(s) for s in samples}

    if gate_source == "ai" and gate_client is None:
        raise ScenarioError("--gate ai에는 게이트 AI 클라이언트가 필요하다")
    raw = {}
    if gate_source in ("mock", "ai"):
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
        judge = make_llm_judge(gate_client, job_id) if gate_source == "ai" else make_label_mock(exp["item_labels"])
        jobs.append(ScenarioJob(**base, gate_posting=posting, gate_judge=judge))
    return jobs, profile, desired, gate_source


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
    gate = {"injected": "fixture 결과 사용", "mock": "규칙 + mock AI 자리", "ai": "실제 AI"}[info.gate_mode]
    if info.gate_mode == "ai":  # 두 단계 모두 실제 AI다. 같은 모델을 쓴다 (data-contract 3-1)
        lines.append(f"=== 시연: {run.profile.profile_id}({who}) / 공고 {len(run.jobs)}건 / 게이트: 실제 AI / 자격요건: 실제 AI / 모델 {info.gate_model_id} / 실행 {run.run_id} ===")
    else:
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


def load_run(path: Path) -> PipelineRun:
    """저장한 실행 결과를 읽고 다시 검증한다."""
    return PipelineRun.model_validate_json(Path(path).read_text(encoding="utf-8"))


def job_info(job_id: str, gate_cases_path: Path | None = None) -> JobInfo:
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


KIND_LABEL = {"cover_letter": "자기소개서", "portfolio": "포트폴리오"}


def _basis(item) -> str:
    """문단·불릿의 근거 표기. 업무 연결 후보는 사실이 아니라서 따로 보인다 (정책 4장)."""
    parts = []
    if item.fact_ids:
        parts.append("자격요건 근거 " + ", ".join(item.fact_ids))
    if item.task_ids:
        parts.append("업무 연결 후보 " + ", ".join(item.task_ids))
    return "(" + ", ".join(parts) + ")"


def draft_markdown(draft: ApplicationDraft) -> str:
    """사람이 전체를 읽는 확인용 md. 화면에 내는 초안과 같은 내용이다(소제목, 문단, 불릿, 근거 사실 id, 확인 필요, 경고, 검증 한계)."""
    ids = lambda item: f"*{_basis(item)}*"  # noqa: E731
    lines = [f"# {KIND_LABEL[draft.kind]} 초안: 공고 {draft.job_id}", "",
             f"> writer: {draft.meta.mode} ({draft.meta.model}) · 프롬프트 v{draft.meta.prompt_version} · 실행 {draft.run_id} · 가상 프로필 {draft.profile_id}",
             f"> {draft.banner}", f"> 검증 한계: {draft.verification}", ""]
    for sec in draft.sections:
        lines += [f"## {sec.heading}", "", f"*섹션: {SECTIONS[draft.kind][sec.section]} (`{sec.section}`)*", ""]
        for para in sec.paragraphs:
            lines += [para.text, "", ids(para), ""]
        lines += [f"- {b.text} {ids(b)}" for b in sec.bullets]
        if sec.bullets:
            lines.append("")
    if draft.linked_tasks:
        lines += ["## 사용한 업무 연결 후보", "", "> 업무 연결 후보는 판정이 아니다. 프로필 경험을 이 업무와 연결해 설명할 수 있다는 후보일 뿐이며, 업무를 할 수 있다거나 요건을 충족한다는 근거가 아니다.", ""]
        lines += [f"- {t.task_id} {t.text}" for t in draft.linked_tasks] + [""]
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
        lines += [f"    {p.text} {_basis(p)}" for p in s.paragraphs]
        lines += [f"    - {b.text} {_basis(b)}" for b in s.bullets]
    lines += [f"업무 연결 후보 {t.task_id}: {t.text}" for t in draft.linked_tasks]
    lines.append(f"--- 확인 필요 {len(draft.to_confirm)}개 (초안에서 충족한 것처럼 쓰지 않았다) ---")
    lines += [f"{c.confirm_id} {c.text} -> {c.question}" for c in draft.to_confirm]
    lines += [f"경고: {n}" for n in draft.notices]
    rel = lambda path: path.relative_to(REPO) if path.is_relative_to(REPO) else path  # noqa: E731
    lines.append(f"저장: {rel(saved)}" if saved else "저장하지 않았다 (--save-draft로 저장)")
    if md:
        lines.append(f"확인용 md: {rel(md)} (커밋하지 않는 파일)")
    return lines


def gate_ai_budget(args, job_count: int) -> tuple[int, int, int, int]:
    """--gate ai의 호출 예산: (게이트 최대 논리 호출, 게이트 최대 시도, 자격요건 최대 시도, 합계 최대 시도).

    두 단계는 같은 키·같은 모델의 일일 한도를 나눠 쓰므로 최대 시도 수를 합쳐 비교한다 (정책 job-gate.md 5-2). 게이트 최대 논리 호출은
    tools.eval_gate와 같은 계산이다. 입력 파일이 없거나 어긋나면 ScenarioError, 샘플 6건 밖이면 AiSetupError.
    """
    try:
        desired, cases = eg.load_sample(args.gate_cases)
    except (FileNotFoundError, ValueError) as e:
        raise ScenarioError(str(e)) from None
    eg.check_ai_scope(cases)
    _, logical = eg.ai_call_bounds(cases, desired)
    gate_attempts = logical * (1 + MAX_RETRIES)
    elig_attempts = 2 * job_count * (1 + MAX_RETRIES)  # 공고당 요건 추출 1 + 근거 찾기 1, 각각 재시도 포함
    return logical, gate_attempts, elig_attempts, gate_attempts + elig_attempts
