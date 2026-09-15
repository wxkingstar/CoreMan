import base64
import subprocess

import pytest

from coreman.core.knowledge.catalog_sync import fetch_catalog
from coreman.core.knowledge.git_auth import https_repository, token_git_env


def test_token_auth_is_ephemeral_scoped_and_has_no_redirects():
    url = https_repository("git@git.example.com:group/repo.git")
    assert url == "https://git.example.com/group/repo.git"
    env = token_git_env(url, "test-token")
    config = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert (
        config[f"http.{url}.extraHeader"]
        == "Authorization: Basic " + base64.b64encode(b"oauth2:test-token").decode()
    )
    assert config["http.followRedirects"] == "false"
    assert config["http.sslVerify"] == "true"
    assert config["credential.helper"] == ""


def test_catalog_fetch_uses_https_auth_without_token_in_arguments(monkeypatch):
    calls = []
    monkeypatch.setenv("GIT_TRACE", "1")
    monkeypatch.setattr(
        "coreman.core.knowledge.catalog_sync.subprocess.run",
        lambda cmd, **kwargs: calls.append((cmd, kwargs)),
    )
    monkeypatch.setattr("coreman.core.knowledge.catalog_sync.read_catalog", lambda root: [])
    assert fetch_catalog("git@git.example.com:group/repo.git", "test-token") == []
    cmd, kwargs = calls[0]
    assert "https://git.example.com/group/repo.git" in cmd
    assert "test-token" not in str(cmd)
    assert "GIT_TRACE" not in kwargs["env"]
    assert kwargs["stderr"].closed
    assert kwargs["env"]["GIT_CONFIG_COUNT"] == "4"


@pytest.mark.parametrize(
    "url", ["http://git.example.com/a.git", "https://user:password@git.example.com/a.git"]
)
def test_token_url_rejects_insecure_or_embedded_credentials(url):
    with pytest.raises(ValueError):
        https_repository(url)


def test_catalog_reports_access_denied_without_echoing_git_output(monkeypatch):
    from coreman.core.knowledge.catalog_sync import CatalogAccessError

    def denied(cmd, **kwargs):
        kwargs["stderr"].write(
            b"remote: You are not allowed to download code from this project. SECRET 403"
        )
        kwargs["stderr"].flush()
        raise subprocess.CalledProcessError(128, cmd)

    monkeypatch.setattr("coreman.core.knowledge.catalog_sync.subprocess.run", denied)
    with pytest.raises(CatalogAccessError) as caught:
        fetch_catalog("https://git.example.com/group/repo.git", "test-token")
    assert "read_repository" in str(caught.value)
    assert "Reporter" in str(caught.value)
    assert "SECRET" not in str(caught.value)
