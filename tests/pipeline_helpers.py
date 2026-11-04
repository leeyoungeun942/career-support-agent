"""pipeline·run_demo·selection 테스트가 함께 쓰는 헬퍼와 가짜 클라이언트 (이슈 #7). 픽스처(results, ai_postings)는 conftest.py에 있다."""
from datetime import datetime, timezone

import pytest

from core.llm_client import BaseClient, FakeClient, LLMRequest, LLMResponse
from core.llm_mock import evidence_answer, extraction_answer, make_evidence_mock, make_extraction_mock, synthetic_posting
from core.pipeline import ScenarioJob, run_pipeline
from core.pipeline_run import ProfileRef
from tools import eval_eligibility as ev
from tools import run_demo as demo

SAMPLES, PROFILE, EVIDENCE, GATES = ev.load_fixtures()
REF = ProfileRef(profile_id="kim-haneul", is_synthetic=True)
NOW = datetime(2026, 11, 4, 6, 15, tzinfo=timezone.utc)
POLICY_14_ORDER = ["sample-3", "sample-1", "sample-4", "sample-5", "sample-2", "sample-6"]
IDS = [s["job_id"] for s in SAMPLES]
BY_ID = {s["job_id"]: s for s in SAMPLES}


def scenario(samples=SAMPLES):
    return [ScenarioJob(job_id=s["job_id"], posting_text=synthetic_posting(s), job_label=f"샘플 {s['number']}", gate=GATES[s["job_id"]])
            for s in samples]


def clients(samples=SAMPLES):
    return make_extraction_mock(samples), make_evidence_mock(samples, EVIDENCE)


def run(jobs=None, samples=SAMPLES, extract=None, evidence=None, **kw):
    mock_e, mock_j = clients(samples)
    return run_pipeline(jobs if jobs is not None else scenario(samples), PROFILE, REF, extract or mock_e, evidence or mock_j, now=NOW, **kw)


class Wrap(BaseClient):
    """mock을 감싸 (단계, job_id)마다 예외를 던지거나 응답을 바꿔 끼운다. 호출 순서는 calls에 쌓는다."""

    external = False

    def __init__(self, inner, errors=None, answers=None):
        self.inner, self.errors, self.answers, self.model = inner, errors or {}, answers or {}, inner.model
        self.calls = []

    def complete_json(self, request: LLMRequest):
        key = (request.stage, request.job_id)
        self.calls.append(key)
        if key in self.errors:
            raise self.errors[key]
        if key in self.answers:
            return FakeClient([self.answers[key]], model=self.model).complete_json(request)
        return self.inner.complete_json(request)


class Routing(BaseClient):
    """GeminiClient 대신 쓰는 가짜: 단계 이름으로 mock 정답을 골라 돌려주고, 외부 클라이언트인 척한다."""

    external = True

    def __init__(self, settings, usage=None):
        self.settings, self.model, self.calls = settings, settings.model, []
        self.answers = {}
        for s in SAMPLES:
            self.answers[("extract_requirements", s["job_id"])] = extraction_answer(s)
            self.answers[("find_evidence", s["job_id"])] = evidence_answer(s, EVIDENCE)

    def ensure_budget(self, needed):
        Routing.needed = needed

    def _complete(self, request):
        self.calls.append(request.stage)
        return LLMResponse(data=self.answers[(request.stage, request.job_id)])


def main(*args):
    """tools.run_demo를 인자로 실행하고 종료 코드를 돌려준다."""
    with pytest.raises(SystemExit) as e:
        demo.main(list(args))
    return e.value.code


def saved_runs(results):
    return sorted((results / "pipeline").glob("*.json"))


def ai_args(postings, *extra):
    return ["--ai", "--accept-free-tier-terms", "--gate", "injected", "--postings", str(postings), *extra]
