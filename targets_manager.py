"""
Správce vyhledávacích cílů (Multi-Target Search Manager).
Umožňuje definovat, ukládat, upravovat a spravovat vícero aktivních hledání
(např. 3D tiskárny, Herní notebooky, Grafické karty) běžících současně.
Data jsou perzistentně ukládána v data/targets.json.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger("TargetManager")


class TargetManager:
    """Správce perzistentních sledovaných témat a vyhledávacích profilů."""

    def __init__(self, file_path: Path, default_query: str = "", default_min_price: Optional[int] = None, default_max_price: Optional[int] = None, default_score_threshold: int = 8):
        self.file_path = file_path
        self.lock = threading.Lock()
        self.targets: Dict[str, Dict[str, Any]] = {}

        self.default_query = default_query
        self.default_min_price = default_min_price
        self.default_max_price = default_max_price
        self.default_score_threshold = default_score_threshold

        self.load()

    def load(self) -> None:
        """Načte vyhledávací cíle z disku. Pokud soubor neexistuje, vytvoří výchozí cíl."""
        with self.lock:
            if self.file_path.exists():
                try:
                    content = self.file_path.read_text(encoding="utf-8").strip()
                    if content:
                        raw_data = json.loads(content)
                        if isinstance(raw_data, list):
                            self.targets = {t["id"]: t for t in raw_data if "id" in t}
                        elif isinstance(raw_data, dict):
                            self.targets = raw_data
                        else:
                            self.targets = {}
                    else:
                        self.targets = {}
                except Exception as e:
                    logger.error(f"Chyba při čtení {self.file_path}: {e}. Inicializuji výchozí.")
                    self.targets = {}

            # Pokud neexistuje žádný cíl, vytvoříme výchozí z konfigurace (.env)
            if not self.targets and self.default_query:
                logger.info("Inicializuji výchozí hledaný cíl z konfigurace...")
                default_target = {
                    "id": "target_default",
                    "name": "3D tiskárny (Výchozí)" if "3d" in self.default_query.lower() else "Výchozí hledání",
                    "query": self.default_query,
                    "min_price": self.default_min_price,
                    "max_price": self.default_max_price,
                    "score_threshold": self.default_score_threshold,
                    "is_active": True,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "last_scraped_at": None,
                    "strategy": None,
                }
                self.targets[default_target["id"]] = default_target
                self._save_unlocked()

    def _save_unlocked(self) -> None:
        """Interní uložení bez dodatečného zamykání."""
        temp_file = self.file_path.with_suffix(".tmp")
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            data_list = list(self.targets.values())
            temp_file.write_text(json.dumps(data_list, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_file.replace(self.file_path)
        except Exception as e:
            logger.error(f"Chyba při ukládání {self.file_path}: {e}")

    def save(self) -> None:
        """Atomické uložení cílů na disk chráněné zámkem."""
        with self.lock:
            self._save_unlocked()

    def get_all_targets(self) -> List[Dict[str, Any]]:
        """Vrátí všechny cíle seřazené podle data vytvoření."""
        with self.lock:
            # Vrátit kopie
            return [dict(t) for t in self.targets.values()]

    def get_active_targets(self) -> List[Dict[str, Any]]:
        """Vrátí pouze zapnuté (aktivní) cíle."""
        with self.lock:
            return [dict(t) for t in self.targets.values() if t.get("is_active", True)]

    def get_target(self, target_id: str) -> Optional[Dict[str, Any]]:
        """Vrátí konkrétní cíl podle ID."""
        with self.lock:
            t = self.targets.get(str(target_id))
            return dict(t) if t else None

    def add_target(
        self,
        name: str,
        query: str,
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
        score_threshold: Optional[int] = None,
        is_active: bool = True,
    ) -> Dict[str, Any]:
        """Vytvoří nový vyhledávací cíl."""
        with self.lock:
            target_id = f"target_{int(time.time() * 1000)}"
            target = {
                "id": target_id,
                "name": name.strip() or "Nové hledání",
                "query": query.strip(),
                "min_price": min_price,
                "max_price": max_price,
                "score_threshold": score_threshold,
                "is_active": is_active,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "last_scraped_at": None,
                "strategy": None,
            }
            self.targets[target_id] = target
            self._save_unlocked()
            logger.info(f"Vytvořen nový vyhledávací cíl: '{target['name']}' [{target_id}]")
            return dict(target)

    def update_target(self, target_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Aktualizuje existující cíl."""
        with self.lock:
            tid = str(target_id)
            if tid not in self.targets:
                return None

            t = self.targets[tid]
            if "name" in updates and updates["name"]:
                t["name"] = str(updates["name"]).strip()

            if "query" in updates and updates["query"]:
                new_query = str(updates["query"]).strip()
                if new_query != t.get("query"):
                    t["query"] = new_query
                    # Při změně zadání vymažeme cached strategii, aby se vygenerovala znovu
                    t["strategy"] = None

            if "min_price" in updates:
                t["min_price"] = updates["min_price"]

            if "max_price" in updates:
                t["max_price"] = updates["max_price"]

            if "score_threshold" in updates:
                t["score_threshold"] = updates["score_threshold"]

            if "is_active" in updates:
                t["is_active"] = bool(updates["is_active"])

            self._save_unlocked()
            logger.info(f"Aktualizován vyhledávací cíl: '{t['name']}' [{tid}]")
            return dict(t)

    def toggle_target(self, target_id: str) -> Optional[Dict[str, Any]]:
        """Přepne stav aktivní/pozastaveno."""
        with self.lock:
            tid = str(target_id)
            if tid not in self.targets:
                return None
            t = self.targets[tid]
            t["is_active"] = not t.get("is_active", True)
            self._save_unlocked()
            status_str = "aktivován" if t["is_active"] else "pozastaven"
            logger.info(f"Vyhledávací cíl '{t['name']}' [{tid}] byl {status_str}.")
            return dict(t)

    def delete_target(self, target_id: str) -> bool:
        """Smaže vyhledávací cíl."""
        with self.lock:
            tid = str(target_id)
            if tid in self.targets:
                name = self.targets[tid].get("name")
                del self.targets[tid]
                self._save_unlocked()
                logger.info(f"Smazán vyhledávací cíl: '{name}' [{tid}]")
                return True
            return False

    def update_strategy(self, target_id: str, strategy: Dict[str, Any]) -> None:
        """Uloží vygenerovanou AI strategii pro konkrétní cíl."""
        with self.lock:
            tid = str(target_id)
            if tid in self.targets:
                self.targets[tid]["strategy"] = strategy
                self._save_unlocked()

    def update_last_scraped(self, target_id: str) -> None:
        """Zaznamená čas posledního scrapování."""
        with self.lock:
            tid = str(target_id)
            if tid in self.targets:
                self.targets[tid]["last_scraped_at"] = datetime.now(timezone.utc).isoformat()
                self._save_unlocked()
