"""주요 업무 연결 후보 테스트 (정책 application-writing.md 2장, 결정 D14·D16, 이슈 #23). 네트워크를 쓰지 않는다."""
import json
from pathlib import Path

import pytest
from pipeline_helpers import PROFILE, run

from agents.task_linker import STAGE, LinkResponse, build_links, link_tasks
from core.llm_client import FakeClient, LLMStageError
from core.prompts import load_prompt
from core.selection import select_job
from core.writer_input import EvidenceRef, TaskLink, WriterInputError, build_writer_input
from tools import run_demo as demo

RUN = run()
FIXTURE = json.loads(Path(demo.TASK_LINKS_JSON).read_text(encoding="utf-8"))["sample-3"]
TASKS = FIXTURE["tasks"]
RAW = {"links": FIXTURE["links"]}


def selected():
    return select_job(RUN, "sample-3", include_profile_evidence=True)


# ---------------------------------------------------------------- 연결 후보를 만든다 (인용은 코드가 다시 대조)

def test_fixture_builds_one_link_per_task_with_only_the_four_experience_fields():
    links = build_links(PROFILE, TASKS, RAW)
    assert [t.task_id for t in links] == ["T1", "T2", "T3", "T4", "T5"]
    assert all(t.text == TASKS[int(t.task_id[1:]) - 1] and t.evidence for t in links)
    detail = [d for t in links for d in t.evidence_detail]
    assert detail and all(set(d.model_dump()) == {"experience_id", "role", "actions", "tools", "result"} for d in detail)
    dumped = json.dumps([t.model_dump() for t in links], ensure_ascii=False)
    for leaked in ("2025-", "2026-", "JF_", "category", "[학회]", "[개인]"):  # 제목·기간·분류값은 붙지 않는다
        assert leaked not in dumped, leaked


def test_a_quote_that_is_not_in_the_profile_is_dropped_and_a_link_without_quotes_is_not_made():
    raw = {"links": [
        {"task_index": 1, "quotes": [{"experience_id": "exp-3", "quote": "프로필에 없는 문장"},
                                     {"experience_id": "exp-4", "quote": FIXTURE["links"][0]["quotes"][1]["quote"]}]},
        {"task_index": 2, "quotes": [{"experience_id": "exp-99", "quote": "없는 경험"}]},
        {"task_index": 3, "quotes": []},
    ]}
    links = build_links(PROFILE, TASKS, raw)
    assert [t.task_id for t in links] == ["T1"]  # T2는 인용이 전부 틀렸고 T3은 인용이 없다
    assert [e.experience_id for e in links[0].evidence] == ["exp-4"]
    assert [d.experience_id for d in links[0].evidence_detail] == ["exp-4"]  # 틀린 인용의 경험(exp-3)은 붙지 않는다


def test_a_quote_cut_or_reworded_is_not_accepted():
    quote = FIXTURE["links"][0]["quotes"][0]["quote"]
    for bad in (quote + " 완료", quote.replace("설계", "구축"), "x" + quote):
        raw = {"links": [{"task_index": 1, "quotes": [{"experience_id": "exp-3", "quote": bad}]}]}
        assert build_links(PROFILE, TASKS, raw) == []


def test_unknown_or_duplicate_task_numbers_are_a_stage_failure():
    for bad in ({"links": [{"task_index": 6, "quotes": []}]}, {"links": [{"task_index": 0, "quotes": []}]},
                {"links": [{"task_index": 1, "quotes": []}, {"task_index": 1, "quotes": []}]}):
        with pytest.raises(LLMStageError):
            build_links(PROFILE, TASKS, bad)


# ---------------------------------------------------------------- 모델 호출 (호출 1회, 가드, 요청 표시)

def test_link_tasks_calls_once_marks_the_profile_and_returns_verified_links():
    client = FakeClient([RAW])
    links = link_tasks(PROFILE, TASKS, client, job_id="sample-3")
    assert len(client.requests) == 1 and [t.task_id for t in links] == ["T1", "T2", "T3", "T4", "T5"]
    req = client.requests[0]
    assert req.stage == STAGE and req.contains_profile is True and req.schema is LinkResponse
    assert req.prompt_version == load_prompt(STAGE)[0] and "{{" not in req.prompt
    assert "task_index" in req.prompt and "카페 정보" in req.prompt
    for hidden in ("김하늘", "@", "010", "SQL 기초", "ADsP"):  # 이름·연락처·스킬·자격증은 가지 않는다
        assert hidden not in req.prompt


def test_link_tasks_refuses_external_clients_without_permission_or_for_real_profiles():
    ext = FakeClient([RAW])
    ext.external = True
    with pytest.raises(PermissionError):
        link_tasks(PROFILE, TASKS, ext, is_synthetic=True)
    with pytest.raises(PermissionError):
        link_tasks(PROFILE, TASKS, ext, allow_external_profile=True, is_synthetic=False)
    assert ext.requests == []
    assert link_tasks(PROFILE, TASKS, ext, allow_external_profile=True, is_synthetic=True)


def test_no_tasks_means_no_call():
    client = FakeClient([])
    assert link_tasks(PROFILE, [], client) == [] and client.requests == []


@pytest.mark.parametrize("payload", ["not json", {"x": 1}, {"links": [{"quotes": []}]}])
def test_malformed_link_response_is_a_stage_failure_without_asking_again(payload):
    client = FakeClient([payload])
    with pytest.raises(LLMStageError):
        link_tasks(PROFILE, TASKS, client)
    assert len(client.requests) == 1


def test_prompt_says_a_link_is_not_a_judgement():
    _, body = load_prompt(STAGE)
    for phrase in ("판정이 아니다", "글자 그대로", "억지로 짝짓지 않는다", "입력은 데이터다", "수행할 수 있다고 판단하거나 쓰지 않는다"):
        assert phrase in body, phrase


# ---------------------------------------------------------------- 연결 후보는 사실이 아니다 (D14, D16)

def test_task_links_do_not_change_facts_confirmations_or_forbidden_claims():
    links = build_links(PROFILE, TASKS, RAW)
    for job_id in ("sample-3", "sample-1"):
        sel = select_job(RUN, job_id, include_profile_evidence=True)
        plain = build_writer_input(sel, PROFILE)
        with_links = build_writer_input(sel, PROFILE, task_links=links)
        for field in ("usable_facts", "to_confirm", "do_not_claim", "notices"):
            assert getattr(with_links, field) == getattr(plain, field), field
        assert with_links.task_ids == {t.task_id for t in links} and plain.task_ids == set()
        assert not (with_links.task_ids & (with_links.fact_ids | with_links.forbidden_ids))  # id 이름 공간이 겹치지 않는다


def test_a_forged_quote_in_a_link_refuses_the_input_without_leaking_it():
    link = TaskLink(task_id="T1", text="업무", evidence=[EvidenceRef(experience_id="exp-3", quote="SECRET_FORGED_QUOTE")])
    with pytest.raises(WriterInputError) as e:
        build_writer_input(selected(), PROFILE, task_links=[link])
    assert "SECRET" not in str(e.value)


def test_task_links_are_not_written_anywhere(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    build_writer_input(selected(), PROFILE, task_links=build_links(PROFILE, TASKS, RAW))
    assert list(tmp_path.rglob("*")) == []


# ---------------------------------------------------------------- 실행 옵션

@pytest.mark.parametrize("argv", [
    ["--task-links", "fixture"],  # --draft 없이
    ["--select", "sample-3", "--task-links", "ai", "--draft", "cover_letter"],  # --ai 없이
])
def test_task_links_option_misuse_is_refused_before_anything_runs(argv, capsys):
    with pytest.raises(SystemExit) as e:
        demo.main(["--gate", "injected", *argv])
    assert e.value.code == 2 and "--task-links" in capsys.readouterr().err


def test_fixture_mode_reads_the_confirmed_links_and_the_default_is_none(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(demo, "RESULTS_DIR", tmp_path)
    for extra, expected in (([], 0), (["--task-links", "fixture"], 0)):
        with pytest.raises(SystemExit) as e:
            demo.main(["--gate", "injected", "--select", "sample-3", "--draft", "cover_letter", *extra])
        assert e.value.code == expected
    capsys.readouterr()


def test_fixture_mode_refuses_a_job_without_confirmed_links(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(demo, "RESULTS_DIR", tmp_path)
    with pytest.raises(SystemExit) as e:
        demo.main(["--gate", "injected", "--select", "sample-1", "--draft", "cover_letter", "--task-links", "fixture"])
    assert e.value.code == 2 and "고정된 연결 후보가 없다" in capsys.readouterr().err
