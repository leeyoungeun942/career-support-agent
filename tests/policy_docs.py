"""정책 문서와 데이터 파일을 읽는 도우미 (직무 게이트 설정값·매핑표 검증용).

원본은 이전 작업 폴더의 validate_job_family_map.py, validate_job_gate_config.py다. 검증 규칙은 job-gate.md 3-3 "자동 검증 규칙"이다.
테스트가 아닌 파일이라 pytest가 직접 실행하지 않는다. test_job_family_map.py와 test_job_gate_config.py가 가져다 쓴다.
"""
import csv
import re
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
POLICY_MD = REPO / "docs" / "policies" / "job-gate.md"
FAMILY_MD = REPO / "docs" / "policies" / "job-family-definition.md"
MAP_CSV = REPO / "data" / "job_family_map.csv"
CODE_CSV = REPO / "data" / "saramin_job_codes.csv"

COLUMNS = [
    "job_mid_cd", "job_mid_name", "job_cd", "job_name", "item_type",
    "mapping_status", "job_family_id", "context_rule", "match_terms",
    "source", "mapping_reason", "review_status",
]
MID_ORDER = ["16", "2", "15", "8", "12"]  # job-gate.md 2-3 지원 대분류, 3-3 정렬 순서
SUPPORTED_MIDS = set(MID_ORDER)
DEFERRED_MIDS = {"12"}  # 2-3: 상품기획·MD 매핑 보류 (2026-10-27). 완결 검사에서도 누락으로 보지 않는다
ITEM_TYPES = {"직무", "직급", "방법론", "산출물", "주제", "기술", "근무형태"}
STATUSES = {"mapped", "context", "common", "out_of_scope", "non_job"}
REVIEW_STATUSES = {"draft", "approved"}
RULE_EXTRA_CANDIDATES = {"범위외", "공통"}  # 3-4 후보 열에 직무군 ID 대신 올 수 있는 값


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def section(text, heading):
    """heading으로 시작하는 절부터 같은 수준 이상의 다음 제목 전까지 잘라낸다."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(heading):
            out = []
            for nxt in lines[i + 1:]:
                m = re.match(r"^(#+) ", nxt)
                if m and len(m.group(1)) <= level:
                    break
                out.append(nxt)
            return out
    raise ValueError(f"제목을 찾지 못함: {heading}")


def split_kw(s):
    return [k.strip() for k in s.split(";") if k.strip()]


def table_cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def load_family_ids():
    """직무군 ID -> 지원 상태 (job-family-definition.md 1장 표의 마지막 열)."""
    lines = section(FAMILY_MD.read_text(encoding="utf-8"), "## 1. 직무군 목록")
    ids = {}
    for line in lines:
        m = re.match(r"^\|\s*`(JF_[A-Z_]+)`\s*\|", line)
        if m:
            ids[m.group(1)] = table_cells(line)[-1]
    return ids


def load_context_rules():
    """규칙 ID -> [(후보, 판별 키워드 목록)] (job-gate.md 3-4 표)."""
    lines = section(POLICY_MD.read_text(encoding="utf-8"), "### 3-4.")
    rules = defaultdict(list)
    for line in lines:
        m = re.match(r"^\|\s*`(CTX_[A-Z_]+)`\s*\|\s*`([^`]+)`\s*\|([^|]*)\|", line)
        if m:
            rules[m.group(1)].append((m.group(2), split_kw(m.group(3))))
    return rules


def doc_families():
    """직무군 ID -> {name, status, definition, key_results, keywords} (job-family-definition.md 1~2장)."""
    family_md = FAMILY_MD.read_text(encoding="utf-8")
    status = load_family_ids()
    names = {}
    for line in section(family_md, "## 1. 직무군 목록"):
        m = re.match(r"^\|\s*`(JF_[A-Z_]+)`\s*\|\s*([^|]+?)\s*\|", line)
        if m:
            names[m.group(1)] = m.group(2)
    fams, cur = {}, None
    for line in section(family_md, "## 2. 직무군별 정의"):
        h = re.match(r"^### 2-\d+\. `(JF_[A-Z_]+)`", line)
        if h:
            cur = h.group(1)
            fams[cur] = {}
            continue
        if line.startswith("### "):
            cur = None
        if cur:
            m = re.match(r"^\|\s*(정의|핵심 결과물|대표 키워드)\s*\|\s*(.*?)\s*\|\s*$", line)
            if m:
                fams[cur][m.group(1)] = m.group(2)
    return {
        fid: {
            "name": names.get(fid),
            "status": status.get(fid),
            "definition": f.get("정의"),
            "key_results": f.get("핵심 결과물"),
            "keywords": split_kw(f.get("대표 키워드", "")),
        }
        for fid, f in fams.items()
    }


def doc_common_cues():
    """공통 업무 이름 -> 단서 목록 (job-gate.md 3-2 "공통 단서" 표)."""
    cues, started = {}, False
    for line in section(POLICY_MD.read_text(encoding="utf-8"), "### 3-2."):
        if line.startswith("**공통 단서**"):
            started = True
            continue
        if started:
            if line.startswith("|"):
                cells = table_cells(line)
                if cells[0] == "공통 업무" or set(cells[0]) <= {"-"}:
                    continue
                cues[cells[0]] = split_kw(cells[1])
            elif cues:
                break
    return cues


def doc_adjacent_pairs():
    """[(직무군 A, 직무군 B, 겹치는 업무, 상태)] (job-gate.md 4-2 표)."""
    pairs = []
    for line in section(POLICY_MD.read_text(encoding="utf-8"), "### 4-2."):
        if line.startswith("|"):
            cells = table_cells(line)
            if len(cells) == 5 and cells[3] in ("사용", "보류", "제외"):
                pairs.append((cells[0], cells[1], cells[2], cells[3]))
    return pairs
