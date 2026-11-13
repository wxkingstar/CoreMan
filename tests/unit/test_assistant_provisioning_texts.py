"""开通卡片与欢迎语里审批系统的叫法：部署配了名称与网页入口就用它们。"""

from types import SimpleNamespace
from typing import Any

import pytest

from coreman.core.assistants import provisioning


def configure(monkeypatch: pytest.MonkeyPatch, name: str | None, url: str | None) -> None:
    settings = SimpleNamespace(tower_display_name=name, tower_web_url=url)
    monkeypatch.setattr(provisioning, "get_settings", lambda: settings)


def card_text(card: dict[str, Any]) -> str:
    return str(card["body"]["elements"][0]["content"])


def test_without_configuration_the_texts_say_approval_system(monkeypatch: pytest.MonkeyPatch):
    configure(monkeypatch, None, None)
    assert "在审批系统里批准后才执行" in card_text(provisioning.offer_card("t"))
    assert "在审批系统里批准后才执行" in provisioning.welcome_text()


def test_configured_name_and_link_replace_the_generic_wording(monkeypatch: pytest.MonkeyPatch):
    configure(monkeypatch, " Example Tower ", "https://tower.example.com/")
    card = card_text(provisioning.offer_card("t"))
    assert "在[Example Tower](https://tower.example.com/)里批准后才执行" in card
    assert "先登录一次Example Tower" in card and "审批系统" not in card
    welcome = provisioning.welcome_text()
    assert "在Example Tower（https://tower.example.com/）里批准后才执行" in welcome
    assert "审批系统" not in welcome


def test_a_link_that_is_not_http_is_left_out(monkeypatch: pytest.MonkeyPatch):
    configure(monkeypatch, "Example Tower", "javascript:alert(1)")
    assert "javascript" not in card_text(provisioning.offer_card("t"))
    assert "javascript" not in provisioning.welcome_text()
