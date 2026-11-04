"""경력 기준을 원문 표현으로 바로잡는다 (정책 eligibility.md 5-4, 13-1).

LLM이 뽑은 경력 요건의 min_value·max_value가 원문의 정해진 표현과 다르면 코드 값으로 고친다. 경력 기준은 종합 판정을 바로 바꾸므로
(예: "신입 / 경력 1년 이상 ~ 3년 이하"를 min_value=1로 읽으면 6개월 인턴이 거짓 미충족이 된다) 모델의 값을 믿기만 하지 않는다.
- LLM과 네트워크를 쓰지 않는다. 판정은 core/rules.py가 한다.
- 애매하면 고치지 않는다. 정해진 표현이 없거나 서로 충돌하면 LLM의 값을 그대로 둔다.
"""
import re

NUM = r"(\d+(?:\.\d+)?)"
TILDE = r"[~∼～]"
_NOT_FRESH = re.compile(r"신입\s*(?:불가|제외)")
_NO_LIMIT = re.compile(r"연차\s*무관")


def _number(text: str) -> int | float:
    value = float(text)
    return int(value) if value == int(value) else value


def career_basis(text: str) -> dict | None:
    """원문에서 확정할 수 있는 경력 기준을 돌려준다. 없거나 충돌하면 None.

    키: min_value(없음=null 포함), max_value(원문이 상한을 말할 때만), force_numeric(유형을 numeric으로 바로잡아야 하는 표현), reason.
    """
    t = re.sub(r"\s+", " ", text)
    has_fresh = "신입" in t
    if has_fresh and _NOT_FRESH.search(t):
        return None  # 신입 경로가 닫힌 공고는 일반 "경력 N년 이상"과 같아서 아래 표를 그대로 쓰기 어렵다
    if "경력" in t and _NO_LIMIT.search(t):
        if has_fresh:
            return None
        return {"min_value": None, "max_value": None, "force_numeric": True, "reason": "연차 무관이라 기준 없음"}

    if has_fresh:
        upper = None
        for pattern, group in ((rf"{NUM}\s*년(?:\s*이상)?\s*{TILDE}\s*{NUM}\s*년", 2), (rf"{NUM}\s*년\s*이하", 1), (rf"{TILDE}\s*{NUM}\s*년", 1)):
            if m := re.search(pattern, t):
                upper = _number(m.group(group))
                break
        found = {"min_value": 0, "reason": "신입 지원 경로가 열려 있어 기준 0"}
        if upper is not None:
            found["max_value"] = upper
        return found

    candidates = []
    for pattern, build in (
        (rf"경력\s*{NUM}\s*년(?:\s*이상)?\s*{TILDE}\s*{NUM}\s*년(?:\s*이하)?",
         lambda m: {"min_value": _number(m.group(1)), "max_value": _number(m.group(2)), "reason": "경력 N~M년"}),
        (rf"경력\s*{NUM}\s*{TILDE}\s*{NUM}\s*년",
         lambda m: {"min_value": _number(m.group(1)), "max_value": _number(m.group(2)), "reason": "경력 N~M년"}),
        (rf"경력\s*{NUM}\s*년\s*(?:이상|초과)(?!\s*{TILDE})", lambda m: {"min_value": _number(m.group(1)), "reason": "경력 N년 이상"}),
        (rf"경력\s*{NUM}\s*년\s*내외", lambda m: {"min_value": max(_number(m.group(1)) - 1, 0), "reason": "경력 N년 내외는 N-1"}),
        (r"경력\s*\(?\s*무관|경력무관", lambda m: {"min_value": 0, "reason": "경력 무관"}),
    ):
        candidates.extend(build(m) for m in re.finditer(pattern, t))  # 같은 표현이 여러 번 나오면 충돌을 가려낸다
    unique = {tuple(sorted(c.items())) for c in candidates}
    return candidates[0] if len(unique) == 1 else None


def normalize_career(raw: dict) -> tuple[dict, list[str]]:
    """요건 하나(ExtractedRequirement 딕셔너리)의 경력 기준을 원문 표현으로 바로잡는다. (새 딕셔너리, 고친 내용)을 돌려준다.

    numeric이면서 career_years인 요건에만 쓴다. 단 "경력 … 연차 무관"은 유형을 잘못 뽑았어도 numeric으로 바로잡는다.
    입력은 바꾸지 않는다. 고친 것이 있으면 interpretation_note 뒤에 ` / `로 이어 적는다.
    """
    if raw.get("combine", "all") != "all":
        return raw, []
    basis = career_basis(raw["text"])
    if basis is None:
        return raw, []
    is_career = raw.get("type") == "numeric" and raw.get("numeric_kind") == "career_years"
    if not is_career and not basis.get("force_numeric"):
        return raw, []

    out, changes = dict(raw), []
    if not is_career:
        out.update(type="numeric", numeric_kind="career_years", min_degree=None, allows_graduating=None, major_names=[])
        if (out.get("career_family") is None) != (out.get("career_family_basis") is None):
            out.update(career_family=None, career_family_basis=None)  # 한쪽만 있으면 JobRequirement 검증을 통과하지 못한다
        changes.append(f"유형 {raw.get('type')}→numeric")
    for key in ("min_value", "max_value"):
        if key in basis:
            if out.get(key) != basis[key]:
                changes.append(f"{key} {raw.get(key)}→{basis[key]}")
            out[key] = basis[key]  # 모델이 키를 보내지 않았어도 값을 채운다
    if not changes:
        return raw, []
    note = f"코드가 원문 표현으로 보정({basis['reason']}: {', '.join(changes)})"
    out["interpretation_note"] = f"{raw['interpretation_note']} / {note}" if raw.get("interpretation_note") else note
    return out, changes
