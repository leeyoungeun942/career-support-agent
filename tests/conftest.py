"""테스트 공통 설정. 테스트는 네트워크를 쓰지 않는다 (플랜 이슈 #6: 실제 호출은 사용자가 승인한 실행에서만)."""
import socket

import pytest

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class NetworkBlocked(RuntimeError):
    """테스트가 외부로 소켓 연결을 시도했다."""


def _host_of(address) -> str:
    return str(address[0]) if isinstance(address, tuple) and address else str(address)


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    """외부 주소로의 연결과 이름 조회를 막는다. 루프백은 허용한다(일부 라이브러리가 내부에서 쓴다)."""
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, address):
        if _host_of(address) not in _LOOPBACK:
            raise NetworkBlocked(f"테스트에서 네트워크 연결 금지: {_host_of(address)}")
        return real_connect(self, address)

    def connect_ex(self, address):
        if _host_of(address) not in _LOOPBACK:
            raise NetworkBlocked(f"테스트에서 네트워크 연결 금지: {_host_of(address)}")
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if str(host) not in _LOOPBACK:
            raise NetworkBlocked(f"테스트에서 이름 조회 금지: {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def results(tmp_path, monkeypatch):
    """tools.run_demo의 결과 폴더(data/results 대신 임시 폴더)."""
    from tools import run_demo as demo

    monkeypatch.setattr(demo, "RESULTS_DIR", tmp_path / "results")
    return tmp_path / "results"


@pytest.fixture
def ai_postings(results, tmp_path, monkeypatch):
    """--ai 실행 준비: 합성 공고 원문 파일, 가짜 설정, 가짜 GeminiClient(Routing). 공고 원문 파일 경로를 돌려준다."""
    import json

    from core.llm_mock import synthetic_posting
    from pipeline_helpers import SAMPLES, Routing
    from tools import eval_eligibility as ev

    postings = tmp_path / "postings.json"
    postings.write_text(json.dumps({"postings": {s["job_id"]: {"text": synthetic_posting(s)} for s in SAMPLES}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ev, "load_env", lambda: {"GEMINI_API_KEY": "k", "GEMINI_MODEL": "m-test", "GEMINI_RPM": "5", "GEMINI_RPD": "20"})
    monkeypatch.setattr(ev, "GeminiClient", Routing)
    return postings


class Typed:
    """run_presentation의 공고 선택 질문에 답을 넣어 준다 (이슈 #28, 정책 presentation.md 2-1).

    기본은 매번 "3"이다(이전 기본 실행의 sample-3). answers에 목록을 주면 그 순서대로 답하고 목록이 떨어지면 입력 끝(None)이다.
    prompts에는 질문 문구가 쌓인다.
    """

    def __init__(self):
        self.answers = None
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if self.answers is None:
            return "3"
        return self.answers.pop(0) if self.answers else None


@pytest.fixture
def typed(monkeypatch):
    """run_presentation의 선택 질문에 답을 넣어 준다. 이 fixture를 쓰는 테스트(run_presentation을 실행하는 두 모듈은 pytestmark로 쓴다)만 영향을 받는다."""
    from tools import run_presentation

    box = Typed()
    monkeypatch.setattr(run_presentation, "read_line", box)
    return box
