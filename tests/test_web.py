"""
Unit test suite pro webové rozhraní a nové funkce Bazos Scraper AI.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest

from config import Config, update_env_file
from main import SeenAdsManager
from notifier import TelegramNotifier
from web import app


@pytest.fixture
def test_client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_web_index(test_client):
    res = test_client.get("/")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Bazoš Scraper & AI Evaluator" in html
    assert "TOP 3 nejlepší nabídky za poslední měsíc" in html
    assert "Co scrapovat?" in html
    assert "Kolik scrapovat?" in html
    assert "Kdy scrapovat?" in html


def test_web_status_endpoint(test_client):
    res = test_client.get("/api/status")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "online"
    assert "stats" in data
    assert "server_ips" in data
    assert "web_port" in data


def test_web_top_deals_endpoint(test_client):
    res = test_client.get("/api/top_deals?limit=3&days=30")
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert isinstance(data["deals"], list)
    assert len(data["deals"]) <= 3


def test_web_all_deals_endpoint(test_client):
    res = test_client.get("/api/all_deals?limit=10")
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert isinstance(data["deals"], list)


def test_seen_ads_manager_top_deals(tmp_path: Path):
    seen_file = tmp_path / "seen_ids.json"
    manager = SeenAdsManager(seen_file)

    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=2)).isoformat()
    old = (now - timedelta(days=40)).isoformat()

    manager.mark_seen("1", "Tiskárna A", 3000, score=7, notified=False)
    manager.data["1"]["seen_at"] = recent

    manager.mark_seen("2", "Tiskárna B", 4000, score=9, notified=True)
    manager.data["2"]["seen_at"] = recent

    manager.mark_seen("3", "Tiskárna C", 2500, score=8, notified=True)
    manager.data["3"]["seen_at"] = recent

    manager.mark_seen("4", "Tiskárna D (stará)", 2000, score=10, notified=True)
    manager.data["4"]["seen_at"] = old

    manager.save()

    # Top deals za 30 dní by mělo ignorovat položku 4 (je starší než 30 dní)
    top = manager.get_top_deals(days=30, limit=3)
    assert len(top) == 3
    # První by měla být Tiskárna B (skóre 9)
    assert top[0]["id"] == "2"
    assert top[0]["score"] == 9
    # Druhá by měla být Tiskárna C (skóre 8)
    assert top[1]["id"] == "3"
    assert top[1]["score"] == 8
    # Třetí by měla být Tiskárna A (skóre 7)
    assert top[2]["id"] == "1"
    assert top[2]["score"] == 7


def test_update_env_file(tmp_path: Path):
    dummy_env = tmp_path / ".env"
    dummy_env.write_text(
        "# Komentář\n"
        "USER_QUERY=\"Původní dotaz\"\n"
        "MIN_PRICE=1000\n"
        "CHECK_INTERVAL_MINUTES=30\n"
        "ACTIVE_START_HOUR=8\n",
        encoding="utf-8"
    )

    updates = {
        "USER_QUERY": "Nový dotaz s diakritikou",
        "MIN_PRICE": 500,
        "MAX_PRICE": 8000,
        "RUN_ON_STARTUP": True,
        "ACTIVE_START_HOUR": "",
    }

    update_env_file(updates, env_file_path=dummy_env)

    content = dummy_env.read_text(encoding="utf-8")
    assert "# Komentář" in content
    assert 'USER_QUERY="Nový dotaz s diakritikou"' in content
    assert "MIN_PRICE=500" in content
    assert "MAX_PRICE=8000" in content
    assert "RUN_ON_STARTUP=true" in content
    assert "ACTIVE_START_HOUR=" in content
    assert "CHECK_INTERVAL_MINUTES=30" in content


def test_notifier_send_top_deals():
    notifier = TelegramNotifier("", "")
    deals = [
        {"id": "100", "title": "Prusa MK3S", "price": 4500, "score": 9, "seen_at": "2026-10-23T10:00:00Z"},
        {"id": "200", "title": "Ender 3", "price": 2500, "score": 8, "seen_at": "2026-10-22T10:00:00Z"}
    ]
    # Mělo by proběhnout v simulovaném režimu bez chyb
    res = notifier.send_top_deals(deals)
    assert res is True
