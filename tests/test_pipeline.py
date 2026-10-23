"""
Unit test suite for Bazos Scraper & AI Evaluator.
"""

import json
from pathlib import Path
import pytest
from config import Config
from scraper import BazosScraper
from groq_client import GroqEvaluator
from notifier import TelegramNotifier
from main import SeenAdsManager


def test_scraper_parse_price():
    scraper = BazosScraper()
    assert scraper._parse_price("15 000 Kč") == (15000, "15 000 Kč")
    assert scraper._parse_price("12999")[0] == 12999
    assert scraper._parse_price("Dohodou") == (None, "Dohodou")
    assert scraper._parse_price("Zdarma") == (0, "Zdarma")
    assert scraper._parse_price("V textu") == (None, "V textu")


def test_scraper_extract_id():
    scraper = BazosScraper()
    url = "https://pc.bazos.cz/inzerat/224577904/pc-set.php"
    assert scraper._extract_id_from_url(url) == "224577904"


def test_scraper_coarse_filter():
    scraper = BazosScraper()
    seen = {"123456"}

    # 1. Already seen ID
    ad1 = {"id": "123456", "title": "Notebook", "description": "OK", "price": 10000}
    passed, reason = scraper.coarse_filter(ad1, seen, ["koupím"], min_price=5000, max_price=20000)
    assert not passed
    assert "Již viděno" in reason

    # 2. Excluded negative keyword (single word)
    ad2 = {"id": "999999", "title": "Koupím herní notebook", "description": "Nabídněte", "price": 10000}
    passed, reason = scraper.coarse_filter(ad2, seen, ["koupím"], min_price=5000, max_price=20000)
    assert not passed
    assert "Obsahuje vyloučené slovo" in reason

    # 3. Excluded phrase (e.g. 'na díly')
    ad3 = {"id": "999999", "title": "Notebook Asus", "description": "Prodám na díly, nejde zapnout", "price": 2000}
    passed, reason = scraper.coarse_filter(ad3, seen, ["na díly"], min_price=1000, max_price=20000)
    assert not passed
    assert "Obsahuje vyloučenou frázi" in reason

    # 4. Price out of bounds (too low)
    ad4 = {"id": "999999", "title": "Notebook Asus", "description": "Super stav", "price": 2000}
    passed, reason = scraper.coarse_filter(ad4, seen, [], min_price=5000, max_price=20000)
    assert not passed
    assert "pod minimem" in reason

    # 5. Price out of bounds (too high)
    ad5 = {"id": "999999", "title": "Notebook Asus", "description": "Super stav", "price": 25000}
    passed, reason = scraper.coarse_filter(ad5, seen, [], min_price=5000, max_price=20000)
    assert not passed
    assert "nad maximem" in reason

    # 6. Valid ad passing all filters
    ad6 = {"id": "999999", "title": "Herní notebook RTX 3060", "description": "Skvělý stav, 16 GB RAM", "price": 15000}
    passed, reason = scraper.coarse_filter(ad6, seen, ["koupím", "na díly"], min_price=5000, max_price=20000)
    assert passed


def test_groq_json_extractor():
    groq = GroqEvaluator(api_key="mock", mock_mode=True)

    # Markdown JSON block
    raw_md = "Zde je výsledek:\n```json\n{\"score\": 9, \"is_deal\": true}\n```"
    parsed = groq._extract_json(raw_md)
    assert parsed["score"] == 9
    assert parsed["is_deal"] is True

    # Raw JSON
    raw_plain = "{\"score\": 8, \"summary\": \"Super\"}"
    parsed = groq._extract_json(raw_plain)
    assert parsed["score"] == 8


def test_groq_mock_mode():
    groq = GroqEvaluator(api_key="mock", mock_mode=True)
    strategy = groq.generate_search_strategy("Herní notebook s RTX 3060")
    assert "search_queries" in strategy
    assert len(strategy["search_queries"]) > 0
    assert "koupím" in strategy["excluded_keywords"]

    ad = {
        "id": "111222",
        "title": "Herní notebook Lenovo RTX 3060",
        "price": 15000,
        "price_str": "15 000 Kč",
        "description": "Záruka ještě rok, top stav",
    }
    eval_res = groq.evaluate_ad(ad, "Herní notebook RTX 3060")
    assert eval_res["score"] >= 8
    assert eval_res["is_deal"] is True


def test_seen_ads_manager(tmp_path: Path):
    seen_file = tmp_path / "seen_ids.json"
    manager = SeenAdsManager(seen_file)

    assert len(manager.get_seen_ids()) == 0
    assert not manager.is_seen("123")

    manager.mark_seen("123", "Notebook", 15000, score=9, notified=True)
    assert manager.is_seen("123")
    assert "123" in manager.get_seen_ids()

    # Re-instantiate from file
    manager2 = SeenAdsManager(seen_file)
    assert manager2.is_seen("123")
    assert manager2.data["123"]["score"] == 9
    assert manager2.data["123"]["notified"] is True


def test_notifier_simulation():
    notifier = TelegramNotifier("", "")
    assert not notifier.is_configured

    # Test that deal alert executes cleanly in simulated mode
    ad = {
        "id": "12345",
        "title": "Lenovo Legion 5 RTX 3060",
        "url": "https://pc.bazos.cz/inzerat/12345/test.php",
        "price_str": "16 500 Kč",
        "location": "Praha",
        "date": "12.10.2026",
    }
    eval_res = {
        "score": 9,
        "summary": "Skvělý stav a výborná cena.",
        "pros": ["RTX 3060", "Záruka", "16 GB RAM"],
        "cons": ["Osobní předání"],
        "estimated_fair_price": "18 000 Kč",
    }
    success = notifier.send_deal_alert(ad, eval_res)
    assert success is True


def test_active_hours_logic(monkeypatch):
    from main import BazosHunterPipeline
    import datetime

    pipeline = BazosHunterPipeline()

    # Case 1: Unrestricted hours (None)
    monkeypatch.setattr("config.config.active_start_hour", None)
    monkeypatch.setattr("config.config.active_end_hour", None)
    assert pipeline.is_within_active_hours() is True

    # Case 2: Restricted hours (8 to 10)
    monkeypatch.setattr("config.config.active_start_hour", 8)
    monkeypatch.setattr("config.config.active_end_hour", 10)

    class MockDateTime8:
        @classmethod
        def now(cls):
            return datetime.datetime(2026, 10, 16, 8, 30)

    class MockDateTime12:
        @classmethod
        def now(cls):
            return datetime.datetime(2026, 10, 16, 12, 30)

    monkeypatch.setattr("main.datetime", MockDateTime8)
    assert pipeline.is_within_active_hours() is True

    monkeypatch.setattr("main.datetime", MockDateTime12)
    assert pipeline.is_within_active_hours() is False
