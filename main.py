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
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Set, List, Optional

import schedule

from config import config
from groq_client import GroqEvaluator
from notifier import TelegramNotifier
from scraper import BazosScraper
from targets_manager import TargetManager

# Kruhový buffer logů pro webové rozhraní
class LogBufferHandler(logging.Handler):
    """Uchovává posledních N logovacích záznamů v paměti pro webový dashboard."""
    def __init__(self, maxlen: int = 250):
        super().__init__()
        self.buffer = deque(maxlen=maxlen)

    def emit(self, record):
        try:
            msg = self.format(record)
            self.buffer.append({
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "name": record.name,
                "message": msg,
            })
        except Exception:
            self.handleError(record)


log_buffer_handler = LogBufferHandler()
log_buffer_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

# Konfigurace kořenového loggeru
logging.basicConfig(
    level=getattr(logging, config.log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger().addHandler(log_buffer_handler)
logger = logging.getLogger("BazosHunter")


class SeenAdsManager:
    """Správce perzistentní paměti pro deduplikaci inzerátů v seen_ids.json s podporou vláken."""

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.data: Dict[str, Any] = {}
        self.lock = threading.Lock()
        self.load()

    def load(self) -> None:
        """Načte dříve viděná ID inzerátů z disku."""
        with self.lock:
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
        with self.lock:
            return str(ad_id) in self.data

    def get_seen_ids(self) -> Set[str]:
        """Vrátí množinu všech zaznamenaných ID."""
        with self.lock:
            return set(self.data.keys())

    def mark_seen(
        self,
        ad_id: str,
        title: str,
        price: Any,
        score: int = 0,
        notified: bool = False,
        reject_reason: str = "",
        url: str = "",
        image_url: str = "",
        summary: str = "",
        estimated_fair_price: str = "",
        pros: Optional[List[str]] = None,
        cons: Optional[List[str]] = None,
        target_id: str = "",
        target_name: str = "",
    ) -> None:
        """Zaznamená inzerát s rozšířenými metadaty a uloží změny na disk."""
        with self.lock:
            self.data[str(ad_id)] = {
                "title": title,
                "price": price,
                "score": score,
                "notified": notified,
                "reject_reason": reject_reason,
                "seen_at": datetime.now(timezone.utc).isoformat(),
                "url": url or f"https://www.bazos.cz/inzerat/{ad_id}/",
                "image_url": image_url or "",
                "summary": summary or "",
                "estimated_fair_price": estimated_fair_price or "",
                "pros": pros or [],
                "cons": cons or [],
                "target_id": target_id or "",
                "target_name": target_name or "",
            }
            self.save()

    def get_top_deals(self, days: int = 30, limit: int = 3, target_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Vrátí N nejlépe hodnocených inzerátů za posledních X dní.
        Filtruje z databáze inzeráty s kladným skóre a řadí primárně podle skóre (sestupně)
        a sekundárně podle data nalezení (nejnovější). Možnost filtrovat podle target_id.
        """
        with self.lock:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(days=days)
            candidates = []

            for ad_id, info in self.data.items():
                score = info.get("score", 0)
                if score <= 0:
                    continue

                if target_id and info.get("target_id") and info.get("target_id") != target_id:
                    continue

                seen_at_str = info.get("seen_at", "")
                seen_dt = None
                if seen_at_str:
                    try:
                        seen_dt = datetime.fromisoformat(seen_at_str.replace("Z", "+00:00"))
                    except Exception:
                        pass

                # Kontrola časového okna posledních X dní
                if seen_dt is None or seen_dt >= cutoff:
                    item = dict(info)
                    item["id"] = str(ad_id)
                    if not item.get("url"):
                        item["url"] = f"https://www.bazos.cz/inzerat/{ad_id}/"
                    item["_seen_dt"] = seen_dt or datetime.min.replace(tzinfo=timezone.utc)
                    candidates.append(item)

            # Řazení: nejvyšší skóre, pak nejnovější datum
            candidates.sort(key=lambda x: (x.get("score", 0), x["_seen_dt"]), reverse=True)

            results = []
            for item in candidates[:limit]:
                cleaned = dict(item)
                cleaned.pop("_seen_dt", None)
                results.append(cleaned)

            return results

    def get_all_deals(self, min_score: int = 0, limit: int = 100, search: str = "", target_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Vrátí zpracované inzeráty seřazené od nejnovějších s možností filtrace."""
        search_lower = search.strip().lower()
        with self.lock:
            items = []
            for ad_id, info in self.data.items():
                score = info.get("score", 0)
                if score < min_score:
                    continue

                if target_id and info.get("target_id") and info.get("target_id") != target_id:
                    continue

                title = str(info.get("title", ""))
                if search_lower and search_lower not in title.lower():
                    continue

                item = dict(info)
                item["id"] = str(ad_id)
                if not item.get("url"):
                    item["url"] = f"https://www.bazos.cz/inzerat/{ad_id}/"
                items.append(item)

            items.sort(key=lambda x: x.get("seen_at", ""), reverse=True)
            return items[:limit]

    def get_stats(self) -> Dict[str, Any]:
        """Vrátí přehledné statistiky o databázi inzerátů."""
        with self.lock:
            total = len(self.data)
            scored = sum(1 for v in self.data.values() if v.get("score", 0) > 0)
            threshold = config.score_threshold
            deals = sum(1 for v in self.data.values() if v.get("score", 0) >= threshold)
            notified = sum(1 for v in self.data.values() if v.get("notified", False))
            rejected_coarse = sum(1 for v in self.data.values() if v.get("reject_reason"))
            return {
                "total_seen": total,
                "total_evaluated": scored,
                "total_deals": deals,
                "total_notified": notified,
                "rejected_coarse": rejected_coarse,
            }


_global_pipeline: Optional["BazosHunterPipeline"] = None


def get_pipeline() -> Optional["BazosHunterPipeline"]:
    """Vrátí běžící instanci orchestrátoru pro webové rozhraní."""
    global _global_pipeline
    return _global_pipeline


class BazosHunterPipeline:
    """Hlavní koordinační pipeline scraperu a AI evaluátoru."""

    def __init__(self):
        global _global_pipeline
        _global_pipeline = self
        self.seen_manager = SeenAdsManager(config.seen_ids_file)
        self.targets_manager = TargetManager(
            config.targets_file,
            default_query=config.user_query,
            default_min_price=config.min_price,
            default_max_price=config.max_price,
            default_score_threshold=config.score_threshold,
        )
        self.scraper = BazosScraper(base_url=config.bazos_base_url, request_delay=1.0)
        self.notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
        self.groq: GroqEvaluator = None
        self.is_running = True
        self.is_scraping = False
        self.run_lock = threading.Lock()
        self.last_run_time: Optional[datetime] = None
        self.last_run_stats: Dict[str, Any] = {}
        self.web_server_thread: Optional[threading.Thread] = None

    def trigger_manual_run(self, force: bool = True) -> Dict[str, Any]:
        """Spustí okamžitý scrapovací cyklus v samostatném vlákně na pozadí."""
        if self.is_scraping:
            return {"status": "busy", "message": "Vyhledávací cyklus již právě probíhá."}

        def _worker():
            try:
                self.run_cycle(force=force)
            except Exception as e:
                logger.error(f"Chyba při manuálním spuštění cyklu: {e}", exc_info=True)

        thread = threading.Thread(target=_worker, daemon=True, name="ManualCycleThread")
        thread.start()
        return {"status": "started", "message": "Vyhledávací cyklus byl úspěšně spuštěn na pozadí."}

    def reload_config(self) -> None:
        """Přenačte konfiguraci ze souboru .env a cíle z targets.json."""
        logger.info("Přenačítám konfiguraci a aktualizuji strategii vyhledávání...")
        config.reload()
        config.validate()
        self.targets_manager.load()

        self.groq = GroqEvaluator(
            api_key=config.groq_api_key,
            model=config.groq_model,
            mock_mode=config.mock_mode,
        )

        # Aktualizovat strategie pro aktivní cíle
        active_targets = self.targets_manager.get_active_targets()
        for target in active_targets:
            if not target.get("strategy") or not target["strategy"].get("search_queries"):
                try:
                    strategy = self.groq.generate_search_strategy(target["query"])
                    if target.get("min_price") is not None:
                        strategy["min_price"] = target["min_price"]
                    if target.get("max_price") is not None:
                        strategy["max_price"] = target["max_price"]
                    self.targets_manager.update_strategy(target["id"], strategy)
                except Exception as e:
                    logger.warning(f"Nelze vygenerovat strategii pro cíl {target.get('name')}: {e}")

        # Přeplánování úloh v schedule
        schedule.clear()
        interval = config.check_interval_minutes
        schedule.every(interval).minutes.do(self.run_cycle)
        logger.info(f"Konfigurace aktualizována: {len(active_targets)} aktivních cílů, interval={interval}m")

    def start_web_server(self) -> None:
        """Spustí Flask webový server v samostatném démonickém vlákně."""
        if not config.web_enabled:
            return
        try:
            from web import run_web_server
            self.web_server_thread = threading.Thread(
                target=run_web_server,
                kwargs={"host": config.web_host, "port": config.web_port},
                daemon=True,
                name="WebDashboardThread",
            )
            self.web_server_thread.start()
            logger.info(f"🌐 Webový dashboard spuštěn na http://{config.web_host}:{config.web_port}")
        except Exception as e:
            logger.error(f"Nepodařilo se spustit webový server: {e}", exc_info=True)

    def initialize(self) -> None:
        """Inicializuje konfiguraci, spojení s API a strategie pro všechny aktivní vyhledávací cíle."""
        logger.info("=" * 65)
        logger.info("🚀 Inicializuji Bazos Scraper & AI Evaluator (Multi-Search)...")
        logger.info(f"Použitý AI model: {config.groq_model} (Mock: {config.mock_mode})")
        logger.info("=" * 65)

        config.validate()

        self.groq = GroqEvaluator(
            api_key=config.groq_api_key,
            model=config.groq_model,
            mock_mode=config.mock_mode,
        )

        self.notifier.test_connection()

        # Příprava strategií pro aktivní cíle
        active_targets = self.targets_manager.get_active_targets()
        logger.info(f"Načteno {len(active_targets)} aktivních vyhledávacích profilů.")

        for target in active_targets:
            tid = target["id"]
            tname = target.get("name", "Hledání")
            tquery = target.get("query", "")
            if not target.get("strategy") or not target["strategy"].get("search_queries"):
                logger.info(f"Generuji strategii pro cíl: '{tname}'...")
                strategy = self.groq.generate_search_strategy(tquery)
                if target.get("min_price") is not None:
                    strategy["min_price"] = target["min_price"]
                if target.get("max_price") is not None:
                    strategy["max_price"] = target["max_price"]
                self.targets_manager.update_strategy(tid, strategy)
                target["strategy"] = strategy

            logger.info(f" - [{tname}]: dotazy {target['strategy'].get('search_queries')}")

        logger.info(f"Načteno {len(self.seen_manager.get_seen_ids())} dříve viděných inzerátů z disku.")

        # Odeslání potvrzení o startu na Telegram
        self.notifier.send_startup_notification(
            user_query=config.user_query,
            active_start_hour=config.active_start_hour,
            active_end_hour=config.active_end_hour,
            targets=active_targets,
        )

    def is_within_active_hours(self) -> bool:
        """Zjistí, zda je aktuální čas v povoleném časovém okně (např. 8:00 - 10:00)."""
        if config.active_start_hour is None or config.active_end_hour is None:
            return True

        current_hour = datetime.now().hour
        if config.active_start_hour <= config.active_end_hour:
            return config.active_start_hour <= current_hour < config.active_end_hour
        else:
            return current_hour >= config.active_start_hour or current_hour < config.active_end_hour

    def run_cycle(self, force: bool = False) -> None:
        """Provede jeden kompletní cyklus přes všechny aktivní vyhledávací profily."""
        if not force and not self.is_within_active_hours():
            now_str = datetime.now().strftime("%H:%M")
            logger.info(
                f"Aktuální čas ({now_str}) je mimo aktivní okno ({config.active_start_hour:02d}:00 - {config.active_end_hour:02d}:00). "
                "Cyklus přeskočen pro úsporu Groq API tokenů."
            )
            return

        with self.run_lock:
            if self.is_scraping:
                logger.warning("Scrapovací cyklus byl přeskočen, protože již jeden právě probíhá.")
                return
            self.is_scraping = True

            try:
                active_targets = self.targets_manager.get_active_targets()
                if not active_targets:
                    logger.info("ℹ️ Žádné aktivní vyhledávací profily k provedení.")
                    return

                logger.info(f"--- Spouštím cyklus pro {len(active_targets)} aktivních hledání ---")

                new_candidates_total = 0
                evaluated_total = 0
                deals_total = 0

                for target in active_targets:
                    if not self.is_running:
                        break

                    target_id = target["id"]
                    target_name = target.get("name", "Hledání")
                    target_query = target.get("query", config.user_query)
                    target_threshold = target.get("score_threshold") or config.score_threshold

                    # Načtení nebo generování strategie
                    strategy = target.get("strategy")
                    if not strategy or not strategy.get("search_queries"):
                        try:
                            strategy = self.groq.generate_search_strategy(target_query)
                            if target.get("min_price") is not None:
                                strategy["min_price"] = target["min_price"]
                            if target.get("max_price") is not None:
                                strategy["max_price"] = target["max_price"]
                            self.targets_manager.update_strategy(target_id, strategy)
                        except Exception as e:
                            logger.error(f"Chyba při přípravě strategie pro '{target_name}': {e}")
                            continue

                    queries = strategy.get("search_queries") or [target_query]
                    excluded_keywords = strategy.get("excluded_keywords", [])
                    min_price = target.get("min_price") if target.get("min_price") is not None else strategy.get("min_price")
                    max_price = target.get("max_price") if target.get("max_price") is not None else strategy.get("max_price")

                    logger.info(f"🎯 Zpracovávám cíl: [{target_name}] (dotazy: {queries}, rozsah {min_price}-{max_price} Kč)")

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
                                    if ad_id in seen_ids:
                                        continue
                                    logger.debug(f"[{target_name}] Inzerát {ad_id} vyřazen: {reason}")
                                    self.seen_manager.mark_seen(
                                        ad_id=ad_id,
                                        title=ad.get("title", ""),
                                        price=ad.get("price"),
                                        score=0,
                                        notified=False,
                                        reject_reason=reason,
                                        url=ad.get("url", ""),
                                        target_id=target_id,
                                        target_name=target_name,
                                    )
                                    continue

                                # 2. Nalezen relevantní kandidát
                                new_candidates_total += 1
                                logger.info(f"[{target_name}] Nový kandidát: [{ad_id}] {ad.get('title')} ({ad.get('price_str')})")

                                if len(ad.get("description", "")) < 200:
                                    full_desc = self.scraper.fetch_ad_detail(ad["url"])
                                    if full_desc:
                                        ad["description"] = full_desc

                                # 3. AI vyhodnocení
                                evaluation = self.groq.evaluate_ad(ad=ad, user_query=target_query)
                                evaluated_total += 1
                                score = evaluation.get("score", 0)
                                summary = evaluation.get("summary", "")

                                logger.info(f"[{target_name}] Inzerát [{ad_id}] AI Skóre: {score}/10 | {summary}")

                                # 4. Odeslání notifikace při splnění prahu
                                notified = False
                                if score >= target_threshold:
                                    deals_total += 1
                                    logger.info(
                                        f"🔥 [{target_name}] SKVĚLÁ NABÍDKA ({score}/10)! Odesílám notifikaci pro [{ad_id}]..."
                                    )
                                    notified = self.notifier.send_deal_alert(
                                        ad=ad,
                                        evaluation=evaluation,
                                        target_name=target_name,
                                    )

                                # 5. Uložení inzerátu
                                self.seen_manager.mark_seen(
                                    ad_id=ad_id,
                                    title=ad.get("title", ""),
                                    price=ad.get("price"),
                                    score=score,
                                    notified=notified,
                                    reject_reason="",
                                    url=ad.get("url", ""),
                                    image_url=ad.get("image_url", ""),
                                    summary=summary,
                                    estimated_fair_price=str(evaluation.get("estimated_fair_price", "")),
                                    pros=evaluation.get("pros", []),
                                    cons=evaluation.get("cons", []),
                                    target_id=target_id,
                                    target_name=target_name,
                                )

                                time.sleep(1.0)

                    self.targets_manager.update_last_scraped(target_id)

                self.last_run_time = datetime.now()
                self.last_run_stats = {
                    "time": self.last_run_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "candidates": new_candidates_total,
                    "evaluated": evaluated_total,
                    "deals_found": deals_total,
                    "active_targets_count": len(active_targets),
                }

                logger.info(
                    f"--- Cyklus dokončen ({len(active_targets)} profilů): {new_candidates_total} kandidátů, "
                    f"{evaluated_total} AI hodnocení, {deals_total} TOP nabídek odesláno. ---"
                )
            finally:
                self.is_scraping = False

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
            # Spuštění webového serveru
            if config.web_enabled:
                self.start_web_server()
        except Exception as e:
            logger.critical(f"Inicializace selhala: {e}", exc_info=True)
            sys.exit(1)

        # Okamžitý první běh po spuštění (pouze pokud jsme v aktivním okně nebo je vynuceno RUN_ON_STARTUP)
        if self.is_within_active_hours() or config.run_on_startup:
            logger.info("Spouštím okamžitý první cyklus po startu aplikace...")
            try:
                self.run_cycle(force=config.run_on_startup)
            except Exception as e:
                logger.error(f"Chyba při úvodním cyklu: {e}", exc_info=True)
        else:
            now_str = datetime.now().strftime("%H:%M")
            logger.info(
                f"Aplikace byla spuštěna v {now_str}, což je mimo aktivní okno ({config.active_start_hour:02d}:00 - {config.active_end_hour:02d}:00). "
                "Úvodní vyhledávací cyklus přeskočen pro úsporu tokenů. Plánovač čeká na aktivní čas."
            )

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
