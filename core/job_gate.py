"""직무 게이트 판정 (docs/policies/job-gate.md 5장).

입력은 직무명, 모집부문, 주요 업무 항목뿐이다. 자격요건·우대사항·경력은 받지 않는다 (5-1).
그래서 자격요건이 미충족이어도 게이트 결과는 달라지지 않는다 (1-2).

AI 판별(5-2의 4단계)은 `ai_judge` 함수로 바꿔 끼운다.
    ai_judge(request: dict) -> {"label": str, "reason": str}
    request 키: level("하위업무"/"항목"), item_index, sub_index, text, item_text,
                other_items, candidates, allowed, families
라벨은 request["allowed"] 안에서만 고른다.

값의 원본: 설정값은 core/job_gate_config.py, 소분류 이름은 data/job_family_map.csv (approved 행만).
"""

import csv
import re
from collections import Counter
from pathlib import Path
from typing import Protocol

from core import job_gate_config as cfg

REPO = Path(__file__).resolve().parents[1]
MAP_CSV = REPO / "data" / "job_family_map.csv"

class AiJudge(Protocol):
    """AI 자리의 형태 (5-2의 4단계). 실제 AI 연결은 보류라 지금은 mock과 스텁만 쓴다 (job-gate.md 8장).

    request 키: level("하위업무"/"항목"), item_index, sub_index, text, item_text, other_items,
                candidates, allowed, families
    반환: {"label": str, "reason": str}. 라벨은 request["allowed"] 안에서만 고른다.
    request에는 직무명·모집부문·사람인 대분류·자격요건·우대사항·다른 항목의 라벨을 넣지 않는다 (5-2).
    """

    def __call__(self, request: dict) -> dict: ...


COMMON = "공통"
OUT_OF_SCOPE = "범위외"
MIXED = "혼합"

# 5-3 기준값 (초기값). 검증(6장) 결과로 조정한다
GATE_THRESHOLDS = {
    "min_items": 2,         # 정리된 업무 항목이 이보다 적으면 검토 필요 (정보 부족)
    "clear_ratio": 2 / 3,   # 핵심 직무군 비율이 이 이상이면 업무 판정이 명확하다
}


def norm(s):
    """공백을 지우고 소문자로 만든다 (5-2: 공백 제거 비교)."""
    return re.sub(r"\s+", "", s).lower()


def split_terms(s):
    return [t.strip() for t in s.split(";") if t.strip()]


def word_starts(text):
    """norm(text)의 각 위치가 원문 단어의 시작인지 (5-2 후보 탐색의 단어 시작 경계).
    공백을 지우면 '운영 업무'가 '운영업무'가 되어 '영업'이 걸리므로, 일치가 공백·문장부호 바로 뒤나
    문장 처음에서 시작해야 한다. 단어 끝은 조사·어미가 붙으므로 따지지 않는다."""
    flags, boundary = [], True
    for ch in text:
        if ch.isspace():
            boundary = True
            continue
        flags.append(boundary)
        boundary = not ch.isalnum()
    return flags


def find_term(term, norm_text, starts):
    """norm_text 안에서 term이 단어 시작에서 시작하는 위치가 있는가."""
    i = norm_text.find(term)
    while i != -1:
        if starts[i]:
            return True
        i = norm_text.find(term, i + 1)
    return False


class Dictionary:
    """후보 탐색에 쓰는 사전. 매핑표 approved 행 + 직무군 대표 키워드 + 공통 단서."""

    def __init__(self, map_csv=MAP_CSV):
        self.family_terms = []   # (정규화 단서, 직무군 ID, 출처)
        self.context_terms = []  # (정규화 단서, 규칙 ID, 출처)
        self.common_terms = []   # (정규화 단서, 출처)
        self.draft_terms = []    # (정규화 단서, 출처): 미확정 행은 판정에 쓰지 않고 AI로 보낸다 (3-1)
        self.oos_terms = []      # 범위외 근거 (5-1 1단계 ④): out_of_scope 소분류 이름 + 3-4 범위외 후보 키워드

        for cand_rules in cfg.CONTEXT_RULES.values():
            for cand, kws in cand_rules:
                if cand == OUT_OF_SCOPE:
                    self.oos_terms += [norm(k) for k in kws]
        for fid, f in cfg.active_families().items():
            for kw in f["keywords"]:
                self.family_terms.append((norm(kw), fid, f"대표 키워드 '{kw}'"))
        for group, kws in cfg.COMMON_CUES.items():
            for kw in kws:
                self.common_terms.append((norm(kw), f"공통 단서 '{kw}'"))

        with open(map_csv, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                names = [r["job_name"]] + split_terms(r["match_terms"])
                status = r["mapping_status"]
                if status == "out_of_scope" and r["review_status"] == "approved":
                    self.oos_terms += [norm(n) for n in names]
                if status not in ("mapped", "context", "common"):
                    continue  # non_job·out_of_scope는 후보 단서가 아니다
                for name in names:
                    src = f"소분류 '{name}'({r['job_cd']})"
                    if r["review_status"] != "approved":
                        self.draft_terms.append((norm(name), src))
                    elif status == "mapped":
                        if r["job_family_id"] in cfg.active_families():
                            self.family_terms.append((norm(name), r["job_family_id"], src))
                    elif status == "context":
                        self.context_terms.append((norm(name), r["context_rule"], src))
                    else:
                        self.common_terms.append((norm(name), src))


def analyze(text, item_text, dic):
    """하위 업무 하나에서 단서를 찾는다 (5-2의 1~3단계)."""
    t, item_t = norm(text), norm(item_text)
    st, st_item = word_starts(text), word_starts(item_text)
    out = {"families": {}, "common": [], "draft": [], "context": [], "to_ai": []}

    for term, fid, src in dic.family_terms:
        if term and find_term(term, t, st):
            out["families"].setdefault(fid, []).append(src)
    for term, src in dic.common_terms:
        if term and find_term(term, t, st):
            out["common"].append(src)
    for term, src in dic.draft_terms:
        if term and find_term(term, t, st):
            out["draft"].append(src)

    # 문맥 판별 (3-4 적용 방법): 같은 항목 안에서 규칙의 후보별 판별 키워드를 찾는다
    seen_rules = set()
    for term, rule_id, src in dic.context_terms:
        if not term or not find_term(term, t, st) or rule_id in seen_rules:
            continue
        seen_rules.add(rule_id)
        hits = {}
        for cand, kws in cfg.CONTEXT_RULES[rule_id]:
            found = [k for k in kws if find_term(norm(k), item_t, st_item)]
            if found:
                hits[cand] = found
        record = {"rule": rule_id, "source": src, "hits": hits}
        out["context"].append(record)
        if len(hits) == 1:
            cand = next(iter(hits))
            if cand in cfg.active_families():
                out["families"].setdefault(cand, []).append(f"문맥 {rule_id}: {hits[cand]}")
            else:
                out["to_ai"].append(f"{rule_id} 후보가 '{cand}'")  # 범위외·공통 후보는 AI가 확인
        else:
            out["to_ai"].append(f"{rule_id} 후보 {len(hits)}개")
    return out


def split_subtasks(text):
    """항목을 하위 업무로 나눈다: ' / '로 이어진 업무, 문장 하나 (5-2 판단 단위).
    'A/B 테스트', '추적/취합/분석'처럼 붙은 '/'는 나누지 않는다."""
    parts = []
    for chunk in re.split(r"\s+/\s+", text):
        parts += re.split(r"(?<=[다요함음])\.\s+", chunk.strip())
    return [p.strip().rstrip(".") for p in parts if p.strip()]


def allowed_labels(candidates):
    base = list(candidates) if candidates else list(cfg.active_families())
    return base + [COMMON, OUT_OF_SCOPE, MIXED]


def label_subtask(text, item_text, item_index, sub_index, other_items, dic, ai_judge, log):
    info = analyze(text, item_text, dic)
    fams = list(info["families"])
    reasons = []
    if info["draft"]:
        reasons.append("미확정 분류 관여")
    if info["common"]:
        reasons.append("공통 단서")
    if info["to_ai"]:
        reasons.append("문맥 판별 미결")
    if len(fams) != 1:
        reasons.append(f"후보 {len(fams)}개")

    record = {
        "text": text,
        "candidates": fams,
        "evidence": info["families"],
        "common_cues": info["common"],
        "context": info["context"],
        "warnings": ["미확정 분류 사용"] if info["draft"] else [],
    }
    if not reasons:
        record.update(label=fams[0], source="규칙")
        return record

    request = {
        "level": "하위업무", "item_index": item_index, "sub_index": sub_index,
        "text": text, "item_text": item_text, "other_items": other_items,
        "candidates": fams, "allowed": allowed_labels(fams), "families": cfg.active_families(),
    }
    answer = ai_judge(request)
    if answer["label"] not in request["allowed"]:
        raise ValueError(f"AI가 허용되지 않은 라벨을 골랐다: {answer['label']} (허용: {request['allowed']})")
    record.update(label=answer["label"], source="AI", ai_reasons=reasons, ai_reason=answer.get("reason", ""))
    log.append({"item": item_index, "sub": sub_index, "text": text, "label": answer["label"],
                "reason": answer.get("reason", ""), "why_ai": reasons})
    return record


def item_label(subs, item_text, item_index, other_items, ai_judge, log):
    """하위 업무 라벨을 항목 라벨로 올린다 (5-2 항목 라벨 규칙)."""
    labels = [s["label"] for s in subs]
    n = len(labels)
    non_common = [l for l in labels if l != COMMON]
    if not non_common:
        return COMMON, "하위 업무가 모두 공통"
    if len(non_common) * 2 < n:
        return COMMON, f"공통이 아닌 하위 업무 {len(non_common)}/{n}로 절반 미만"
    if len(non_common) * 2 == n:
        request = {
            "level": "항목", "item_index": item_index, "sub_index": None,
            "text": item_text, "item_text": item_text, "other_items": other_items,
            "candidates": sorted(set(non_common) - {OUT_OF_SCOPE, MIXED}),
            "allowed": allowed_labels(sorted(set(non_common) - {OUT_OF_SCOPE, MIXED})),
            "families": cfg.active_families(),
        }
        answer = ai_judge(request)
        if answer["label"] not in request["allowed"]:
            raise ValueError(f"AI가 허용되지 않은 라벨을 골랐다: {answer['label']}")
        log.append({"item": item_index, "sub": None, "text": item_text, "label": answer["label"],
                    "reason": answer.get("reason", ""), "why_ai": ["공통이 아닌 하위 업무가 정확히 절반"]})
        return answer["label"], "공통이 아닌 하위 업무가 정확히 절반이라 항목 전체를 AI로 판별"
    top, top_n = Counter(non_common).most_common(1)[0]
    if top_n * 2 > len(non_common):
        return top, f"공통이 아닌 하위 업무 {len(non_common)}/{n} 중 {top_n}개가 같은 라벨 (과반)"
    return MIXED, "공통이 아닌 하위 업무가 절반 초과인데 어느 라벨도 과반이 아님 (분리 불가)"


def judge(posting, desired_family, ai_judge, dic=None):
    """공고 1건을 판정한다 (5-1). 반환값은 5-4의 기록 필드를 따른다.

    posting: {"job_title", "position", "items": [{"heading"?, "text", "exclude"?}]}
    """
    dic = dic or Dictionary()
    th = GATE_THRESHOLDS
    log = []
    result = {
        "desired_family": desired_family, "result": None, "core_family": None,
        "ended_at": None, "items": [], "reason_code": None, "warnings": [],
        "evidence": [], "ai_log": log, "config_version": cfg.CONFIG_VERSION,
    }

    def finish(step, outcome, reason_code=None):
        result.update(result=outcome, ended_at=step, reason_code=reason_code)
        return result

    # 0. 업무 항목 정리: 회사 소개 문장은 뺀다
    items = [it for it in posting["items"] if not it.get("exclude")]
    texts = [(it.get("heading", "") + " " + it["text"]).strip() for it in items]

    # 대분류 정보는 샘플에 없다 (5-3): 1단계 ①은 항상 참
    title_text = posting["job_title"] + " " + posting.get("position", "")
    title_info = analyze(title_text, title_text, dic)
    title_family = next(iter(title_info["families"])) if len(title_info["families"]) == 1 else None

    # 항목별 하위 업무 단서 (1단계 ③과 3단계가 함께 쓴다)
    analyzed = []
    for i, it in enumerate(items):
        subs = split_subtasks(it["text"])
        others = [t for j, t in enumerate(texts) if j != i]
        analyzed.append((subs, others))

    # 1. 범위 외 확인 (네 조건 모두, 5-1). 대분류 정보가 없어 ①은 항상 참이다
    any_cue = bool(title_info["families"]) or any(
        analyze(s, texts[i], dic)["families"] for i, (subs, _) in enumerate(analyzed) for s in subs
    )
    oos_items = sum(
        1 for t in texts if any(find_term(term, norm(t), word_starts(t)) for term in dic.oos_terms if term)
    )
    result["oos_items"] = f"{oos_items}/{len(items)}"
    if not any_cue and items and oos_items * 2 >= len(items):  # ④ 범위외 근거가 항목의 절반 이상
        return finish(1, "범위 외")

    # 2. 정보 충분성
    if len(items) < th["min_items"]:
        return finish(2, "검토 필요", "정보 부족")

    # 3. 업무 항목 라벨링
    for i, it in enumerate(items):
        subs, others = analyzed[i]
        sub_records = [
            label_subtask(s, texts[i], i, k, others, dic, ai_judge, log) for k, s in enumerate(subs)
        ]
        label, why = item_label(sub_records, texts[i], i, others, ai_judge, log)
        result["items"].append({"index": i, "heading": it.get("heading", ""), "text": it["text"],
                                "label": label, "why": why, "subtasks": sub_records})
        for s in sub_records:
            result["warnings"] += [w for w in s["warnings"] if w not in result["warnings"]]

    # 4. 핵심 직무군 결정
    labels = [it["label"] for it in result["items"]]
    counted = [l for l in labels if l != COMMON]
    if not counted:
        return finish(4, "검토 필요", "핵심 업무 없음")
    top, top_n = Counter(counted).most_common(1)[0]
    families_only = Counter(l for l in counted if l in cfg.JOB_FAMILIES)
    if families_only:
        result["core_family"] = families_only.most_common(1)[0][0]  # 검토 필요여도 추정값은 보존 (5-4)
    if top_n * 2 <= len(counted) or top == MIXED:
        return finish(4, "검토 필요", "혼합 직무")
    if top == OUT_OF_SCOPE:
        result["core_family"] = None
        return finish(4, "범위 외")
    result["core_family"] = top
    ratio = top_n / len(counted)
    result["core_ratio"] = f"{top_n}/{len(counted)}"

    # 5. 직무명 대조 (대분류는 정보가 없어 건너뜀)
    if title_family and title_family != top:
        if ratio >= th["clear_ratio"]:
            result["warnings"].append("직무명 차이")
        else:
            return finish(5, "검토 필요", "직무명-업무 충돌")

    # 6. 희망 직무와 비교
    if top == desired_family:
        return finish(6, "일치")
    if cfg.is_adjacent(top, desired_family) and desired_family in labels:  # 4-1 인접 조건
        return finish(6, "인접")
    return finish(6, "불일치")
