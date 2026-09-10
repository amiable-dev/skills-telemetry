import pytest

@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", "skills")
    monkeypatch.setenv("STDTEL_BRANCH", "feature/PLAT-123-structured-logging")
    monkeypatch.setenv("STDTEL_REPO", "git@github.com:org/payments-api.git")
    monkeypatch.setenv("STDTEL_TEAM", "payments")
    yield
