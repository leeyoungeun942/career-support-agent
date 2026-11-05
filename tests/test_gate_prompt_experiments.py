"""게이트 프롬프트 실험 도구 검증 (정책 job-gate.md 6-5 "프롬프트 실험", 이슈 #15). 네트워크를 쓰지 않는다."""
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

import pytest
from gate_data import RAW_CASES, needs_raw

from core import prompts
from core.job_gate_ai import make_llm_judge
from core.llm_client import FakeClient
from core.prompts import PROMPT_DIR, load_prompt, load_prompt_file, prompt_hash
from core.schemas import ITEM_LABEL_COMMON

POLICY = (Path(__file__).resolve().parents[1] / "docs" / "policies" / "job-gate.md").read_text(encoding="utf-8")
SHARED_LIMIT = 12  # 정책 6-5: 샘플 원문 항목과 공백을 뺀 이 길이 이상 이어서 같으면 샘플 문장이 들어간 것이다


# ---------------------------------------------------------------- 프롬프트 파일 읽기: prompts/ 아래 .md만

def make_prompt_dir(tmp_path):
    root = tmp_path / "prompts"
    (root / "experiments").mkdir(parents=True)
    return root


def test_experiment_file_under_the_prompt_dir_is_read_with_its_own_version(tmp_path):
    root = make_prompt_dir(tmp_path)
    f = root / "experiments" / "gate_label_b1.md"
    f.write_text("version: 4-b1\n본문 {{labels}}\n", encoding="utf-8")
    assert load_prompt_file(f, root=root) == ("4-b1", "본문 {{labels}}\n")


@pytest.mark.parametrize("where", ["data/sample-jobs/gate_sample_cases.json", ".env", "prompts/../data/x.md", "outside.txt", "outside.md"])
def test_files_outside_the_prompt_dir_or_not_markdown_are_refused_without_reading_them(tmp_path, where):
    root = make_prompt_dir(tmp_path)
    target = tmp_path / where.replace("prompts/../", "")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("version: 9\nUNIQUE_SECRET_CONTENT", encoding="utf-8")
    path = root / ".." / "data" / "x.md" if where.startswith("prompts/../") else target
    with pytest.raises(ValueError, match="아래의 .md 파일") as e:
        load_prompt_file(path, root=root)
    assert "UNIQUE_SECRET_CONTENT" not in str(e.value)  # 오류 메시지에 파일 내용을 옮기지 않는다


def test_non_markdown_file_inside_the_prompt_dir_is_refused_too(tmp_path):
    root = make_prompt_dir(tmp_path)
    (root / "experiments" / "note.txt").write_text("version: 1\n본문", encoding="utf-8")
    with pytest.raises(ValueError, match="아래의 .md 파일"):
        load_prompt_file(root / "experiments" / "note.txt", root=root)


def test_missing_file_and_missing_version_line_are_errors(tmp_path):
    root = make_prompt_dir(tmp_path)
    with pytest.raises(ValueError, match="없다"):
        load_prompt_file(root / "experiments" / "nope.md", root=root)
    bad = root / "experiments" / "gate_label_x.md"
    bad.write_text("본문만 있다", encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        load_prompt_file(bad, root=root)


def test_the_default_prompt_is_read_the_same_way_by_path_and_by_name():
    assert load_prompt_file(PROMPT_DIR / "gate_label.md") == load_prompt("gate_label")


def test_prompt_hash_is_eight_hex_chars_and_follows_the_body():
    h = prompt_hash("본문")
    assert re.fullmatch(r"[0-9a-f]{8}", h) and h == prompt_hash("본문") and h != prompt_hash("본문 ")


# ---------------------------------------------------------------- 어댑터가 넘겨받은 프롬프트를 쓴다

def test_adapter_uses_the_given_prompt_version_and_body_instead_of_the_default():
    version, body = load_prompt("gate_label")
    marker = "UNIQUE_VARIANT_RULE 이 규칙은 실험 변형에만 있다"
    variant = ("4-test", body.replace("## 허용 라벨", f"{marker}\n\n## 허용 라벨", 1))
    client = FakeClient([{"label": ITEM_LABEL_COMMON, "reason": "r"}, {"label": ITEM_LABEL_COMMON, "reason": "r"}])
    request = {"level": "subtask", "item_index": 0, "sub_index": 0, "text": "t", "item_text": "t", "other_items": [], "context_items": [],
               "candidates": [], "allowed": [ITEM_LABEL_COMMON, "mixed", "out_of_scope"], "families": ["JF_SERVICE_PLANNING"]}
    make_llm_judge(client, "p", variant)(request)
    make_llm_judge(client, "p")(request)  # 기본
    custom, default = client.requests
    assert custom.prompt_version == "4-test" and marker in custom.system
    assert default.prompt_version == version and marker not in default.system


# ---------------------------------------------------------------- 프롬프트에 샘플 문장이 없다 (정책 6-5, 이슈 #15 완료 조건)

def squash(text):
    return re.sub(r"\s+", "", text)


def longest_shared(prompt_body, texts):
    """프롬프트 본문과 텍스트들이 공백 없이 이어서 같은 가장 긴 부분의 길이."""
    body = squash(prompt_body)
    best = 0
    for text in texts:
        t = squash(text)
        best = max(best, SequenceMatcher(None, body, t, autojunk=False).find_longest_match(0, len(body), 0, len(t)).size)
    return best


def test_longest_shared_detects_a_copied_fragment_and_ignores_whitespace():
    sentence = "에이전트의 목적과 행동 범위 판단 기준을 기획 문서로 정의"
    assert longest_shared("앞 규칙 " + sentence + " 뒤 규칙", [sentence]) >= len(squash(sentence))
    assert longest_shared("앞 규칙 에이전트의   목적과\n행동 범위 판단", [sentence]) >= SHARED_LIMIT
    assert longest_shared("공통 업무의 예: 문서 작성, 데이터 분석", [sentence]) < SHARED_LIMIT  # 우연히 겹치는 용어


PROMPT_FILES = sorted([PROMPT_DIR / "gate_label.md", *PROMPT_DIR.glob("experiments/gate_label*.md")])


@needs_raw
@pytest.mark.parametrize("path", PROMPT_FILES, ids=lambda p: str(p.relative_to(PROMPT_DIR)))
def test_no_sample_sentence_is_in_the_gate_prompts(path):
    """로컬 전용: 샘플 원문 파일(커밋하지 않음)이 있을 때 본 프롬프트와 모든 실험 변형을 검사한다."""
    cases = json.loads(RAW_CASES.read_text(encoding="utf-8"))["cases"]
    texts = [(it.get("heading", "") + " " + it["text"]) for c in cases for it in c["items"]]
    assert len(texts) >= 20
    _, body = prompts.load_prompt_file(path)
    shared = longest_shared(body, texts)
    assert shared < SHARED_LIMIT, f"{path.name}: 샘플 문장과 공백 제외 {shared}자가 이어서 같다"


@needs_raw
def test_the_check_would_catch_a_sample_sentence_put_into_a_prompt():
    cases = json.loads(RAW_CASES.read_text(encoding="utf-8"))["cases"]
    texts = [(it.get("heading", "") + " " + it["text"]) for c in cases for it in c["items"]]
    _, body = load_prompt("gate_label")
    leaked = body + "\n예시: " + texts[0]
    assert longest_shared(leaked, texts) >= len(squash(texts[0]))


def test_experiment_variants_in_the_repo_follow_the_naming_rule():
    for path in (PROMPT_DIR / "experiments").glob("*.md") if (PROMPT_DIR / "experiments").exists() else []:
        assert path.name.startswith("gate_label"), f"{path.name}: 변형 파일 이름은 gate_label로 시작한다"
        version, _ = prompts.load_prompt_file(path)
        assert version != load_prompt("gate_label")[0], f"{path.name}: 변형은 기본 프롬프트와 다른 버전 문자열이어야 한다"


@pytest.mark.policy_contract
def test_policy_states_the_experiment_rules_the_tool_enforces():
    for phrase in ("prompts/experiments/", "--prompt", "아래의 `.md` 파일만", "해시의 앞 8자리", "12자 이상", "한 번에 하나만", "같은 방향의 2회 이상"):
        assert phrase in POLICY, phrase
    assert str(SHARED_LIMIT) in POLICY  # 테스트의 기준 길이와 정책의 숫자가 같다
