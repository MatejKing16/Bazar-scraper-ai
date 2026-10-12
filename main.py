"""
Hlavní řídicí orchestrátor pro Bazos Scraper & AI Evaluator.
Zajišťuje periodické scrapování, deduplikaci v JSON souboru,
sémantické AI vyhodnocování nabídek a odesílání Telegram notifikací.
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Set, List

import schedule

from config import config
from groq_client import GroqEvaluator
from notifier import TelegramNotifier
from scraper import BazosScraper

# Konfigurace kořenového loggeru
logging.basicConfig(
    level=getattr(logging, config.log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("BazosHunter")


class SeenAdsManager:
    """Správce perzistentní paměti pro deduplikaci inzerátů v seen_ids.json."""

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        """Načte dříve viděná ID inzerátů z disku."""
        if self.file_path.exists():
            try:
                content = self.file_path.read_text(encoding="utf-8").strip()
                if content:
                    self.data = json.loads(content)
                else:
                    self.data = {}
            except Exception as e:
                logger.error(f"Chyba při čtení {self.file_path}: {e}. Inicializuji prázdnou paměť.")
                self.data = {}
        else:
            self.data = {}
            self.save()

    def save(self) -> None:
        """Atomický zápis na disk chránící soubor před poškozením při neočekávaném vypnutí."""
        temp_file = self.file_path.with_suffix(".tmp")
        try:
            temp_file.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_file.replace(self.file_path)
        except Exception as e:
            logger.error(f"Chyba při ukládání {self.file_path}: {e}")

    def is_seen(self, ad_id: str) -> bool:
        """Zjistí, zda už bylo dané ID inzerátu zpracováno."""
        return str(ad_id) in self.data

    def get_seen_ids(self) -> Set[str]:
        """Vrátí množinu všech zaznamenaných ID."""
        return set(self.data.keys())

    def mark_seen(
        self,
        ad_id: str,
        title: str,
        price: Any,
        score: int = 0,
        notified: bool = False,
        reject_reason: str = "",
    ) -> None:
        """Zaznamená inzerát s metadaty a uloží změny na disk."""
        self.data[str(ad_id)] = {
            "title": title,
            "price": price,
            "score": score,
            "notified": notified,
            "reject_reason": reject_reason,
            "seen_at": datetime.now(timezone.utc).isoformat(),
        }
        self.save()


class BazosHunterPipeline:
    """Hlavní koordinační pipeline scraperu a AI evaluátoru."""

    def __init__(self):
        self.seen_manager = SeenAdsManager(config.seen_ids_file)
        self.scraper = BazosScraper(base_url=config.bazos_base_url, request_delay=1.0)
        self.notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
        self.groq: GroqEvaluator = None
        self.search_strategy: Dict[str, Any] = {}
        self.is_running = True

    def initialize(self) -> None:
        """Inicializuje konfiguraci, spojení s API a generuje strategii vyhledávání."""
        logger.info("=" * 65)
        logger.info("🚀 Inicializuji Bazos Scraper & AI Evaluator...")
        logger.info(f"Hledané zadání: '{config.user_query}'")
        logger.info(f"Použitý AI model: {config.groq_model} (Mock: {config.mock_mode})")
        logger.info("=" * 65)

        # Validace nastavení
        config.validate()

        # Inicializace Groq klienta
        self.groq = GroqEvaluator(
            api_key=config.groq_api_key,
            model=config.groq_model,
            mock_mode=config.mock_mode,
        )

        # Test Telegram spojení
        self.notifier.test_connection()

        # Generování optimalizovaných dotazů přes Groq
        self.search_strategy = self.groq.generate_search_strategy(config.user_query)

        # Přepsání cenových filtrů, pokud jsou explicitně definovány v .env
        if config.min_price is not None:
            self.search_strategy["min_price"] = config.min_price
        if config.max_price is not None:
            self.search_strategy["max_price"] = config.max_price

        logger.info(f"Aktivní vyhledávací dotazy pro Bazoš: {self.search_strategy['search_queries']}")
        logger.info(
            f"Cenový rozsah: {self.search_strategy.get('min_price')} - {self.search_strategy.get('max_price')} Kč"
        )
        logger.info(f"Načteno {len(self.seen_manager.get_seen_ids())} již viděných inzerátů z disku.")

        # Odeslání potvrzení o startu na Telegram
        self.notifier.send_startup_notification(
            search_queries=self.search_strategy.get("search_queries", []),
            excluded_keywords=self.search_strategy.get("excluded_keywords", []),
            user_query=config.user_query,
        )

    def run_cycle(self) -> None:
        """Provede jeden kompletní cyklus vyhledání, filtrace, AI hodnocení a notifikace."""
        logger.info("--- Spouštím nový vyhledávací a vyhodnocovací cyklus ---")
        queries = self.search_strategy.get("search_queries") or [config.user_query]
        excluded_keywords = self.search_strategy.get("excluded_keywords", [])
        min_price = self.search_strategy.get("min_price")
        max_price = self.search_strategy.get("max_price")

        new_candidates = 0
        evaluated_count = 0
        deals_found = 0

        for query in queries:
            if not self.is_running:
                break

            for page in range(config.max_pages_to_scrape):
                if not self.is_running:
                    break

                ads = self.scraper.search(
                    query=query,
                    min_price=min_price,
                    max_price=max_price,
                    page=page,
                )

                if not ads:
                    logger.info(f"Pro dotaz '{query}' na stránce {page + 1} nebyly nalezeny žádné inzeráty.")
                    break

                for ad in ads:
                    if not self.is_running:
                        break

                    ad_id = str(ad.get("id"))
                    seen_ids = self.seen_manager.get_seen_ids()

                    # 1. Hrubá filtrace
                    passed, reason = self.scraper.coarse_filter(
                        ad=ad,
                        seen_ids=seen_ids,
                        excluded_keywords=excluded_keywords,
                        min_price=min_price,
                        max_price=max_price,
                    )

                    if not passed:
                        # Pokud již byl inzerát dříve viděn, jen pokračujeme
                        if ad_id in seen_ids:
                            continue
                        # Pokud inzerát neprošel hrubým filtrem (cena, vyloučené slovo),
                        # uložíme ho do paměti, abychom ho nezkoumali v příštích cyklech
                        logger.debug(f"Inzerát {ad_id} vyřazen hrubým filtrem: {reason}")
                        self.seen_manager.mark_seen(
                            ad_id=ad_id,
                            title=ad.get("title", ""),
                            price=ad.get("price"),
                            score=0,
                            notified=False,
                            reject_reason=reason,
                        )
                        continue

                    # 2. Nalezen relevantní kandidát
                    new_candidates += 1
                    logger.info(f"Nalezen nový kandidát: [{ad_id}] {ad.get('title')} ({ad.get('price_str')})")

                    # Pokud je popis ze stránky výsledků příliš krátký, stáhneme plný detail
                    if len(ad.get("description", "")) < 200:
                        full_desc = self.scraper.fetch_ad_detail(ad["url"])
                        if full_desc:
                            ad["description"] = full_desc

                    # 3. Sémantické hodnocení přes Groq AI
                    evaluation = self.groq.evaluate_ad(ad=ad, user_query=config.user_query)
                    evaluated_count += 1
                    score = evaluation.get("score", 0)
                    summary = evaluation.get("summary", "")

                    logger.info(f"Inzerát [{ad_id}] Výsledek AI: Skóre = {score}/10 | Shrnutí: {summary}")

                    # 4. Kontrola prahu skóre a odeslání notifikace
                    notified = False
                    if score >= config.score_threshold:
                        deals_found += 1
                        logger.info(
                            f"🔥 SKVĚLÁ NABÍDKA ({score}/10)! Odesílám okamžité upozornění pro [{ad_id}]..."
                        )
                        notified = self.notifier.send_deal_alert(ad=ad, evaluation=evaluation)

                    # 5. Uložení inzerátu do seen_ids.json
                    self.seen_manager.mark_seen(
                        ad_id=ad_id,
                        title=ad.get("title", ""),
                        price=ad.get("price"),
                        score=score,
                        notified=notified,
                        reject_reason="",
                    )

                    # Krátká pauza mezi AI vyhodnoceními
                    time.sleep(1.0)

        logger.info(
            f"--- Cyklus dokončen: {new_candidates} nových kandidátů, "
            f"{evaluated_count} AI vyhodnocení, {deals_found} top nabídek odesláno. ---"
        )

    def stop(self, *args) -> None:
        """Zpracování signálu pro čisté ukončení běhu (SIGINT, SIGTERM)."""
        logger.info("Přijat signál k ukončení. Zastavuji pipeline...")
        self.is_running = False

    def start(self) -> None:
        """Spustí celou pipeline a přepne do nekonečné smyčky plánovače."""
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        try:
            self.initialize()
        except Exception as e:
            logger.critical(f"Inicializace selhala: {e}", exc_info=True)
            sys.exit(1)

        # Okamžitý první běh hned po spuštění
        logger.info("Spouštím okamžitý první cyklus po startu aplikace...")
        try:
            self.run_cycle()
        except Exception as e:
            logger.error(f"Chyba při úvodním cyklu: {e}", exc_info=True)

        # Nastavení plánovače úloh
        interval = config.check_interval_minutes
        logger.info(f"Plánuji periodické spouštění každých {interval} minut.")
        schedule.every(interval).minutes.do(self.run_cycle)

        # Hlavní smyčka 24/7
        logger.info("Pipeline běží v režimu 24/7. Pro ukončení stiskněte Ctrl+C.")
        while self.is_running:
            try:
                schedule.run_pending()
                time.sleep(1)
            except Exception as e:
                logger.error(f"Chyba v běhové smyčce plánovače: {e}", exc_info=True)
                time.sleep(10)

        logger.info("Aplikace byla korektně ukončena. Nashledanou!")


if __name__ == "__main__":
    pipeline = BazosHunterPipeline()
    pipeline.start()
