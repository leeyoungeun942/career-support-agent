"""직무 게이트 매핑표(data/job_family_map.csv) 자동 검증 (이슈 #5).

검증 규칙의 원본: job-gate.md 3-3 "자동 검증 규칙". 직무군 ID는 job-family-definition.md 1장, 문맥 판별 규칙은 job-gate.md 3-4 표에서 읽는다.
매핑표와 사람인 직무 코드표(data/saramin_job_codes.csv)는 한 쌍이다. 소분류 이름과 누락을 코드표 기준으로 검사한다.
원본은 이전 작업 폴더의 validate_job_family_map.py다. 오류가 있으면 테스트가 실패하고, 경고는 검토용이라 실패시키지 않는다.
"""
from collections import Counter, defaultdict

import pytest

import policy_docs as pd

pytestmark = pytest.mark.policy_contract


def validate(complete):
    """(오류 목록, 경고 목록). complete이면 지원 대분류가 모두 있어야 한다(보류 대분류 제외)."""
    errors, warnings = [], []
    if not pd.MAP_CSV.read_bytes().startswith(b"\xef\xbb\xbf"):
        errors.append("UTF-8 BOM이 없음 (엑셀에서 한글이 깨짐, 3-3 인코딩)")
    fieldnames, rows = pd.read_csv(pd.MAP_CSV)
    _, codes = pd.read_csv(pd.CODE_CSV)
    families = pd.load_family_ids()
    rules = pd.load_context_rules()

    if fieldnames != pd.COLUMNS:
        errors.append(f"열 순서가 3-3과 다름: {fieldnames}")
        return errors, warnings

    origin = {(c["job_mid_cd"], c["job_cd"]): c for c in codes}

    # 규칙표 자체 검사
    for rule_id, lines in rules.items():
        if len(lines) < 2:
            errors.append(f"3-4 {rule_id}: 후보 줄이 {len(lines)}개 (2개 이상 필요)")
        for cand, keywords in lines:
            if cand not in families and cand not in pd.RULE_EXTRA_CANDIDATES:
                errors.append(f"3-4 {rule_id}: 알 수 없는 후보 '{cand}'")
            if not keywords:
                errors.append(f"3-4 {rule_id} / {cand}: 판별 키워드가 비어 있음")

    seen, used_rules = set(), set()
    by_name = defaultdict(list)
    for n, r in enumerate(rows, start=2):  # 1행은 헤더
        key = (r["job_mid_cd"], r["job_cd"])
        where = f"{n}행 ({r['job_cd']} {r['job_name']})"

        if key in seen:
            errors.append(f"{where}: 키 (job_mid_cd, job_cd) 중복")
        seen.add(key)

        src = origin.get(key)
        if src is None:
            errors.append(f"{where}: 원본 코드표에 없는 키")
        elif (r["job_mid_name"], r["job_name"]) != (src["job_mid_name"], src["job_name"]):
            errors.append(f"{where}: 이름이 원본과 다름 (원본: {src['job_mid_name']} / {src['job_name']})")
        if r["job_mid_cd"] not in pd.SUPPORTED_MIDS:
            errors.append(f"{where}: 지원 대분류가 아님 ({r['job_mid_cd']})")

        status, fam, rule = r["mapping_status"], r["job_family_id"], r["context_rule"]
        if r["item_type"] not in pd.ITEM_TYPES:
            errors.append(f"{where}: item_type 허용값 아님 '{r['item_type']}'")
        if status not in pd.STATUSES:
            errors.append(f"{where}: mapping_status 허용값 아님 '{status}'")
        if r["review_status"] not in pd.REVIEW_STATUSES:
            errors.append(f"{where}: review_status 허용값 아님 '{r['review_status']}'")

        if r["item_type"] != "직무" and status != "non_job":
            errors.append(f"{where}: item_type이 '{r['item_type']}'이면 non_job이어야 함")
        if status == "non_job" and r["item_type"] == "직무":
            errors.append(f"{where}: non_job인데 item_type이 '직무'")

        if status == "mapped":
            if fam not in families:
                errors.append(f"{where}: mapped인데 job_family_id가 job-family-definition.md에 없음 '{fam}'")
            elif families[fam] != "지원":
                errors.append(f"{where}: {fam}의 지원 상태가 '{families[fam]}'")
        elif fam:
            errors.append(f"{where}: {status}인데 job_family_id가 채워짐")

        if status == "context":
            if rule not in rules:
                errors.append(f"{where}: context_rule이 3-4에 없음 '{rule}'")
            used_rules.add(rule)
        elif rule:
            errors.append(f"{where}: {status}인데 context_rule이 채워짐")

        if r["match_terms"]:
            terms = [t.strip() for t in r["match_terms"].split(";")]
            if any(not t for t in terms):
                errors.append(f"{where}: match_terms에 빈 표기가 있음")
            if len(terms) != len(set(terms)):
                errors.append(f"{where}: match_terms에 중복 표기가 있음")

        for col in ("source", "mapping_reason"):
            if not r[col].strip():
                errors.append(f"{where}: {col} 비어 있음")

        by_name[r["job_name"]].append((r["job_mid_name"], status, fam or rule))

    # 정렬 검사: 대분류는 MID_ORDER 순, 대분류 안에서는 job_cd 숫자 오름차순
    def sort_key(r):
        mid = r["job_mid_cd"]
        mid_rank = pd.MID_ORDER.index(mid) if mid in pd.MID_ORDER else len(pd.MID_ORDER)
        cd = int(r["job_cd"]) if r["job_cd"].isdigit() else 0
        return (mid_rank, cd)

    for n, (prev, cur) in enumerate(zip(rows, rows[1:]), start=3):
        if sort_key(cur) < sort_key(prev):
            errors.append(f"{n}행 ({cur['job_cd']} {cur['job_name']}): 정렬 순서가 3-3과 다름 (앞 행 {prev['job_cd']})")
            break  # 첫 위치만 알려 준다

    # 누락 검사
    mids_present = {r["job_mid_cd"] for r in rows}
    target_mids = (pd.SUPPORTED_MIDS - pd.DEFERRED_MIDS) if complete else mids_present & pd.SUPPORTED_MIDS
    for key, c in origin.items():
        if key[0] in target_mids and key not in seen:
            errors.append(f"누락: {c['job_mid_name']} {c['job_cd']} {c['job_name']}")
    for mid in [m for m in pd.MID_ORDER if m not in mids_present]:
        if mid in pd.DEFERRED_MIDS:
            warnings.append(f"대분류 {mid}: 보류 중 (job-gate.md 2-3)")
        elif not complete:
            warnings.append(f"대분류 {mid}: 아직 작성되지 않음 (완결 검사에서는 오류)")

    # 같은 이름, 다른 매핑: 오류가 아니라 3-3에 따라 의미를 확인할 대상
    for name, items in by_name.items():
        if len({(s, v) for _, s, v in items}) > 1:
            desc = ", ".join(f"{m}={s}:{v}" for m, s, v in items)
            warnings.append(f"같은 이름 다른 매핑 '{name}': {desc} (의미가 다른지 확인)")

    for rule_id in sorted(set(rules) - used_rules):
        warnings.append(f"3-4 {rule_id}: 매핑표에서 쓰지 않는 규칙")

    drafts = sum(r["review_status"] == "draft" for r in rows)
    if drafts:
        warnings.append(f"draft {drafts}행 / 전체 {len(rows)}행: 판정에 쓰이지 않음")

    return errors, warnings


def test_map_passes_all_validation_rules():
    """3-3 자동 검증 규칙 전체. 지원 대분류 5개가 모두 있어야 한다(상품기획·MD는 보류라 제외)."""
    errors, _ = validate(complete=True)
    assert not errors, "\n" + "\n".join(f"  - {e}" for e in errors[:30]) + (f"\n  ... 외 {len(errors) - 30}건" if len(errors) > 30 else "")


# --- 검증 규칙과 별개로, 정책 8장에 적힌 숫자를 테스트에 직접 적어 둔 독립 확인 -----------------
EXPECTED_ROWS_PER_MID = {"기획·전략": 72, "IT개발·데이터": 260, "디자인": 148, "영업·판매·무역": 113}


def test_map_has_the_rows_policy_8_describes():
    _, rows = pd.read_csv(pd.MAP_CSV)
    assert dict(Counter(r["job_mid_name"] for r in rows)) == EXPECTED_ROWS_PER_MID
    assert len(rows) == 593


def test_all_map_rows_are_approved():
    """draft 행은 판정에 쓰이지 않는다. 지금 매핑표는 593행 모두 approved다 (정책 8장)."""
    _, rows = pd.read_csv(pd.MAP_CSV)
    assert Counter(r["review_status"] for r in rows) == {"approved": 593}


def test_map_status_counts_match_the_known_snapshot():
    """mapping_status 분포. 행을 추가·수정하면 이 숫자도 함께 고친다(조용한 변경을 막는다)."""
    _, rows = pd.read_csv(pd.MAP_CSV)
    assert Counter(r["mapping_status"] for r in rows) == {"non_job": 329, "out_of_scope": 144, "mapped": 84, "context": 21, "common": 15}


def test_the_deferred_mid_category_has_no_rows():
    """상품기획·MD(대분류 12)는 보류 중이라 행이 없어야 한다 (job-gate.md 2-3)."""
    _, rows = pd.read_csv(pd.MAP_CSV)
    assert not [r for r in rows if r["job_mid_cd"] in pd.DEFERRED_MIDS]
