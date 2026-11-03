"""직무 게이트 판정 기록을 job_gate_result.v2로 바꾼다 (docs/data-contract.md 1장, docs/policies/job-gate.md 5-4).

core/job_gate.py의 judge()는 판정 기록(딕셔너리)을 낸다. 이 파일이 그것을 JobGateResult로 만든다.
- summary는 코드가 템플릿으로 만든다 (data-contract 1-6). LLM이 쓰지 않는다.
- detail.evidence는 정책 5-4 "핵심 근거를 만드는 규칙"을 따른다: 인용은 하위 업무 원문 전체이고 한 항목에 1개다.
- proceed_to_eligibility는 없다. 모든 공고가 자격요건 평가를 받고, 게이트는 정렬 그룹(ranking_group)만 정한다.
"""
from collections import Counter
from datetime import date
from fractions import Fraction

from core import job_gate as gate
from core import job_gate_config as cfg
from core.schemas import (
    DISPLAY_LABELS,
    EvidenceQuote,
    GateAiLogEntry,
    GateDetail,
    GateEvidence,
    GateItem,
    GateSubtask,
    GateVersions,
    JobGateResult,
    Ratio,
)

CORE_RESULTS = ("match", "adjacent", "mismatch")  # 핵심 직무군이 정해진 판정


def _ratio(text):
    """판정 기록의 "2/3" 문자열을 Ratio로. 없으면 None."""
    if text is None:
        return None
    count, total = text.split("/")
    return Ratio(count=int(count), total=int(total))


def _name(family_id):
    return cfg.JOB_FAMILIES[family_id]["name"]


def make_summary(raw):
    """data-contract 1-6 템플릿. 판정 값만 넣는다."""
    result, desired, core = raw["result"], raw["desired_family"], raw["core_family"]
    if result == "match":
        ratio = _ratio(raw["core_ratio"])
        return f"공통을 뺀 업무 {ratio.total}개 중 {ratio.count}개가 {_name(desired)}"
    if result == "adjacent":
        ratio = _ratio(raw["core_ratio"])
        return f"핵심 직무는 {_name(core)}({ratio.count}/{ratio.total})이며 {_name(desired)} 업무도 있음"
    if result == "mismatch":
        ratio = _ratio(raw["core_ratio"])
        return f"공통을 뺀 업무 {ratio.total}개 중 {ratio.count}개가 {_name(core)}"
    if result == "review_needed":
        return f"직무 확인 필요: {DISPLAY_LABELS[raw['reason_code']]}"
    oos = _ratio(raw["oos_items"])
    return f"지원 범위 밖 업무가 중심 (범위외 근거 {oos.count}/{oos.total})"


def _quote(item, label):
    """항목에서 인용할 하위 업무: label이 후보로 잡힌 첫 하위 업무, 없으면 첫 하위 업무 (정책 5-4). 하위 업무가 없으면 None."""
    subtasks = item["subtasks"]
    return next((s for s in subtasks if label in s["candidates"]), subtasks[0] if subtasks else None)


def _entry(item, subtask):
    return EvidenceQuote(item_index=item["index"], quote=subtask["text"], source="job_posting", decided_by=subtask["source"])


def _missing(raw):
    """검토 필요 사유별 고정 문구 (정책 5-4). 다른 판정은 비어 있다."""
    reason = raw["reason_code"]
    th = gate.GATE_THRESHOLDS
    if reason == "insufficient_info":
        return [f"업무 항목 {raw['item_count']}개 < 최소 {th['min_items']}개"]
    if reason == "no_core_tasks":
        return ["공통을 뺀 업무 항목 0개"]
    if reason == "mixed_job":
        counted = [it["label"] for it in raw["items"] if it["label"] != gate.COMMON]
        top, top_n = Counter(counted).most_common(1)[0]
        return ["가장 많은 라벨이 혼합"] if top == gate.MIXED else [f"가장 많은 라벨 {top_n}/{len(counted)}이 과반이 아님"]
    ratio = _ratio(raw["core_ratio"])  # title_task_conflict
    clear = Fraction(th["clear_ratio"]).limit_denominator(100)
    return [f"직무명은 {_name(raw['title_family'])}, 업무 핵심은 {_name(raw['core_family'])}"
            f"({ratio.count}/{ratio.total}), 명확성 기준 {clear.numerator}/{clear.denominator} 미만"]


def make_evidence(raw):
    """정책 5-4 "핵심 근거를 만드는 규칙"."""
    supporting, conflicting, missing = [], [], []
    if raw["result"] in CORE_RESULTS:
        core = raw["core_family"]
        for item in raw["items"]:
            label = item["label"]
            if label == core:
                target = supporting
            elif label.startswith("JF_"):  # 공통·혼합·범위외 라벨은 인용하지 않는다
                target = conflicting
            else:
                continue
            if (sub := _quote(item, label)) is not None:
                target.append(_entry(item, sub))
    elif raw["result"] == "out_of_scope":
        supporting = [_entry(item, sub) for item in raw["items"]
                      if item["label"] == gate.OUT_OF_SCOPE and (sub := _quote(item, item["label"])) is not None]
    else:
        missing = _missing(raw)
    return GateEvidence(supporting=supporting, conflicting=conflicting, missing=missing)


def _items(raw):
    return [
        GateItem(
            index=it["index"], heading=it["heading"], text=it["text"], label=it["label"], why=it["why"],
            subtasks=[
                GateSubtask(
                    text=s["text"], label=s["label"], source=s["source"], candidates=s["candidates"], cues=s["evidence"],
                    common_cues=s["common_cues"], context_rules=s["context"], warnings=s["warnings"], ai_reason=s.get("ai_reason"),
                )
                for s in it["subtasks"]
            ],
        )
        for it in raw["items"]
    ]


def build_gate_result(job_id, raw, with_detail=True, ai_model=None, prompt_version=None):
    """judge()의 판정 기록을 JobGateResult로 만든다. detail은 근거 확인·테스트용이라 끌 수 있다 (data-contract 1장)."""
    detail = None
    if with_detail:
        detail = GateDetail(
            ended_at_step=raw["ended_at"],
            core_ratio=_ratio(raw.get("core_ratio")),
            oos_items=_ratio(raw.get("oos_items")),
            evidence=make_evidence(raw),
            items=_items(raw),
            ai_log=[GateAiLogEntry(**entry) for entry in raw["ai_log"]],
            versions=GateVersions(
                config_version=raw["config_version"],
                policy_revised=date.fromisoformat(cfg.POLICY_REVISED),
                map_modified=date.fromisoformat(cfg.MAP_MODIFIED),
                ai_model=ai_model,
                prompt_version=prompt_version,
            ),
        )
    return JobGateResult(
        job_id=job_id,
        desired_family=raw["desired_family"],
        result=raw["result"],
        core_family=raw["core_family"],
        reason_code=raw["reason_code"],
        warnings=raw["warnings"],
        summary=make_summary(raw),
        detail=detail,
    )


def judge_posting(job_id, posting, desired_family, ai_judge, dic=None, **options):
    """공고 1건을 판정해 JobGateResult로 돌려준다. options는 build_gate_result의 with_detail, ai_model, prompt_version."""
    return build_gate_result(job_id, gate.judge(posting, desired_family, ai_judge, dic), **options)
