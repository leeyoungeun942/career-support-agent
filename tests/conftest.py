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
