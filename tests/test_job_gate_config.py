"""직무 게이트 설정값(core/job_gate_config.py)이 정책 문서 표와 같은지 검사 (이슈 #5).

비교 대상 (원본은 문서 표):
    JOB_FAMILIES    <- job-family-definition.md 1장 지원 상태, 2장 정의·핵심 결과물·대표 키워드
    COMMON_CUES     <- job-gate.md 3-2 공통 단서 표
    CONTEXT_RULES   <- job-gate.md 3-4 문맥 판별 규칙 표
    ADJACENT_PAIRS  <- job-gate.md 4-2 인접 직무군 쌍 표

값을 바꿀 때는 문서 표를 먼저 고치고 설정값에 옮긴다 (job-gate.md 3-1). 문서를 읽는 계약 테스트라 정책 문서 형식이 바뀌면 깨진다.
원본은 이전 작업 폴더의 validate_job_gate_config.py다.
"""
import re

import pytest

import policy_docs as pd
from core import job_gate_config as cfg

pytestmark = pytest.mark.policy_contract


def diff_keys(label, doc, code):
    errors = [f"{label} '{key}': 문서에만 있음 (코드에 옮기지 않음)" for key in sorted(doc.keys() - code.keys())]
    errors += [f"{label} '{key}': 코드에만 있음 (문서에 없음)" for key in sorted(code.keys() - doc.keys())]
    return errors


def family_errors():
    errors = []
    doc, code = pd.doc_families(), cfg.JOB_FAMILIES
    errors += diff_keys("직무군", doc, code)
    for fid in sorted(doc.keys() & code.keys()):
        for field, doc_value in doc[fid].items():
            if code[fid].get(field) != doc_value:
                errors.append(f"직무군 {fid} {field}: 문서와 다름\n    문서: {doc_value}\n    코드: {code[fid].get(field)}")
        keywords = code[fid]["keywords"]
        if len(keywords) != len(set(keywords)):
            errors.append(f"직무군 {fid} keywords: 중복 키워드가 있음")
        if not keywords:
            errors.append(f"직무군 {fid} keywords: 비어 있음")
    return errors


def keyword_owner_errors():
    """대표 키워드는 그 직무군에만 나와야 한다 (job-gate.md 3-2). 공백을 지우고 비교한다."""
    owner = {}
    for fid, family in cfg.JOB_FAMILIES.items():
        for keyword in family["keywords"]:
            owner.setdefault(keyword.replace(" ", ""), []).append(fid)
    return [f"대표 키워드 '{k}'가 둘 이상의 직무군에 있음: {owners}" for k, owners in owner.items() if len(owners) > 1]


def common_cue_errors():
    doc = pd.doc_common_cues()
    errors = diff_keys("공통 단서", doc, cfg.COMMON_CUES)
    for name in sorted(doc.keys() & cfg.COMMON_CUES.keys()):
        if doc[name] != cfg.COMMON_CUES[name]:
            errors.append(f"공통 단서 '{name}': 문서와 다름\n    문서: {doc[name]}\n    코드: {cfg.COMMON_CUES[name]}")
    return errors


def context_rule_errors():
    errors = []
    doc = {rule_id: [(cand, kws) for cand, kws in lines] for rule_id, lines in pd.load_context_rules().items()}
    errors += diff_keys("문맥 판별 규칙", doc, cfg.CONTEXT_RULES)
    for rule_id in sorted(doc.keys() & cfg.CONTEXT_RULES.keys()):
        code_lines = [(cand, list(kws)) for cand, kws in cfg.CONTEXT_RULES[rule_id]]
        if doc[rule_id] != code_lines:
            errors.append(f"문맥 판별 규칙 {rule_id}: 문서와 다름\n    문서: {doc[rule_id]}\n    코드: {code_lines}")
    for rule_id, lines in cfg.CONTEXT_RULES.items():
        for cand, _ in lines:
            if cand not in cfg.JOB_FAMILIES and cand not in pd.RULE_EXTRA_CANDIDATES:
                errors.append(f"문맥 판별 규칙 {rule_id}: 알 수 없는 후보 '{cand}'")
    return errors


def adjacent_pair_errors():
    errors = []
    doc_pairs = pd.doc_adjacent_pairs()
    code_pairs = [tuple(p) for p in cfg.ADJACENT_PAIRS]
    if doc_pairs != code_pairs:
        errors += [f"인접 쌍: 문서에만 있거나 값이 다름 {p}" for p in doc_pairs if p not in code_pairs]
        errors += [f"인접 쌍: 코드에만 있거나 값이 다름 {p}" for p in code_pairs if p not in doc_pairs]
        if not errors:
            errors.append("인접 쌍: 순서가 문서와 다름")
    for a, b, _, status in code_pairs:
        for name in (a, b):
            if name not in cfg.PAIR_NAME_TO_ID:
                errors.append(f"인접 쌍 '{name}': PAIR_NAME_TO_ID에 없음")
        if status == "사용":
            for fid in (cfg.PAIR_NAME_TO_ID.get(a), cfg.PAIR_NAME_TO_ID.get(b)):
                if fid is None or fid not in cfg.JOB_FAMILIES:
                    errors.append(f"인접 쌍 {a}↔{b}: '사용'인데 직무군 ID가 없음 ({fid})")
    seen = set()
    for a, b, _, _ in code_pairs:
        key = frozenset((a, b))
        if key in seen:
            errors.append(f"인접 쌍 {a}↔{b}: 방향만 바꿔 중복됨 (4-1: 방향 없음)")
        seen.add(key)
    for name, fid in cfg.PAIR_NAME_TO_ID.items():
        if fid is not None and fid not in cfg.JOB_FAMILIES:
            errors.append(f"PAIR_NAME_TO_ID '{name}': job-family-definition.md에 없는 직무군 ID '{fid}'")
    return errors


def assert_no_errors(errors):
    assert not errors, "\n" + "\n".join(f"  - {e}" for e in errors)


def test_job_families_match_policy_doc():
    assert_no_errors(family_errors())


def test_representative_keywords_belong_to_one_family():
    assert_no_errors(keyword_owner_errors())


def test_common_cues_match_policy_doc():
    assert_no_errors(common_cue_errors())


def test_context_rules_match_policy_doc():
    assert_no_errors(context_rule_errors())


def test_adjacent_pairs_match_policy_doc():
    assert_no_errors(adjacent_pair_errors())


def test_config_version_is_a_dated_version_string():
    """CONFIG_VERSION은 판정 기록에 남는다. 값을 바꾸면 올린다 (config 맨 위 주석). 값 자체는 고정하지 않고 형식만 본다."""
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", cfg.CONFIG_VERSION)


def test_policy_revised_is_the_last_date_in_the_policy_revision_history():
    """판정 기록의 정책 개정일(D7). 문서를 고치면 개정 이력 날짜가 바뀌고, 이 상수도 함께 올려야 한다."""
    assert cfg.POLICY_REVISED == pd.doc_last_revision_date()


def test_map_modified_and_config_version_match_the_latest_validation_row():
    """판정 기록의 매핑표 수정일(D7)과 설정값 버전이 6-4 검증 결과 표의 마지막 행과 같다."""
    row = pd.doc_latest_validation_row()
    assert cfg.MAP_MODIFIED == row["매핑표 수정일"]
    assert cfg.CONFIG_VERSION == row["설정값"]


def test_active_families_are_exactly_the_supported_ones_in_the_family_doc():
    supported = {fid for fid, status in pd.load_family_ids().items() if status == "지원"}
    assert set(cfg.active_families()) == supported


def test_adjacent_id_pairs_are_the_active_pairs_of_the_table():
    expected = {frozenset((cfg.PAIR_NAME_TO_ID[a], cfg.PAIR_NAME_TO_ID[b])) for a, b, _, status in pd.doc_adjacent_pairs() if status == "사용"}
    assert set(cfg.adjacent_id_pairs()) == expected
    assert cfg.is_adjacent("JF_DEV", "JF_QA") and cfg.is_adjacent("JF_QA", "JF_DEV")  # 방향이 없다
    assert not cfg.is_adjacent("JF_DEV", "JF_DEV")  # 같은 직무군은 인접이 아니라 일치다
    assert not cfg.is_adjacent("JF_SERVICE_PLANNING", "JF_DEV")  # '제외' 상태
