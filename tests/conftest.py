import pytest


@pytest.fixture(autouse=True)
def narrow_ambiguous_symbols(monkeypatch):
    """Rendered output must not depend on the machine's console code page."""
    monkeypatch.setenv('VASP_AGENT_AMBIGUOUS_WIDE', '0')


@pytest.fixture(autouse=True)
def subprocess_ssh_transport(monkeypatch):
    """Tests stub the ssh subprocess; never let a pooled Paramiko connection reach a host."""
    monkeypatch.setenv('VASP_AGENT_SSH_TRANSPORT', 'subprocess')
