"""
Konfigurační modul pro Bazos Scraper & AI Evaluator.
Načítá a validuje nastavení z proměnných prostředí a souboru .env.
"""

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# Načtení souboru .env z kořenového adresáře projektu
load_dotenv()


def _parse_optional_int(var_name: str) -> Optional[int]:
    """Bezpečně parsuje celočíselnou proměnnou prostředí nebo vrací None."""
    val = os.getenv(var_name, "").strip()
    if val and (val.isdigit() or (val.startswith("-") and val[1:].isdigit())):
        try:
            return int(val)
        except ValueError:
            return None
    return None


@dataclass
class Config:
    # 1. Groq API konfigurace
    groq_api_key: str = os.getenv("GROQ_API_KEY", "").strip()
    groq_model: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()

    # 2. Telegram Bot konfigurace
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    # 3. Volné zadání uživatele (Natural Language prompt)
    user_query: str = os.getenv(
        "USER_QUERY",
        "Herní notebook s grafickou kartou RTX 3060 nebo lepší, minimálně 16 GB RAM, procesor AMD Ryzen nebo Intel i5/i7 (11. gen a novější), bez poškození a vad, cena do 18000 Kč"
    ).strip()

    # 4. Hrubý filtr cen (Kč)
    min_price: Optional[int] = _parse_optional_int("MIN_PRICE")
    max_price: Optional[int] = _parse_optional_int("MAX_PRICE")

    # 5. Plánovač a logika vyhodnocení
    check_interval_minutes: int = _parse_optional_int("CHECK_INTERVAL_MINUTES") or 30
    score_threshold: int = _parse_optional_int("SCORE_THRESHOLD") or 8
    max_pages_to_scrape: int = _parse_optional_int("MAX_PAGES_TO_SCRAPE") or 1

    # 6. Režim simulace / Mock (užitečné pro rychlý test bez Groq klíče)
    mock_mode: bool = os.getenv("MOCK_MODE", "false").lower() in ("true", "1", "yes")

    # 7. Cesty a perzistence
    bazos_base_url: str = os.getenv("BAZOS_BASE_URL", "https://www.bazos.cz").rstrip("/")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    def __post_init__(self):
        # Nastavení adresáře dat - relativní k projektu nebo absolutní (např. /app/data v Dockeru)
        raw_data_dir = os.getenv("DATA_DIR", "data").strip()
        data_path = Path(raw_data_dir)
        if not data_path.is_absolute():
            base_dir = Path(__file__).resolve().parent
            self.data_dir = base_dir / raw_data_dir
        else:
            self.data_dir = data_path

        self.seen_ids_file = self.data_dir / "seen_ids.json"

        # Vytvoření adresáře pokud neexistuje
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Inicializace seen_ids.json jako prázdného JSON objektu pokud neexistuje
        if not self.seen_ids_file.exists():
            self.seen_ids_file.write_text("{}", encoding="utf-8")

    def validate(self) -> None:
        """Validuje konfiguraci a vypisuje přehledná varování nebo chyby."""
        errors = []
        warnings = []

        # Kontrola Groq API klíče
        if not self.mock_mode:
            if not self.groq_api_key or "your_groq_api_key" in self.groq_api_key.lower():
                errors.append(
                    "GROQ_API_KEY není nastaven nebo obsahuje výchozí zástupný text!\n"
                    "-> Získejte bezplatný klíč na: https://console.groq.com/keys a vložte jej do .env souboru.\n"
                    "-> Pro spuštění v testovacím režimu bez klíče nastavte MOCK_MODE=true v .env."
                )

        # Kontrola Telegramu
        telegram_missing = (
            not self.telegram_bot_token
            or "your_telegram_bot_token" in self.telegram_bot_token.lower()
            or not self.telegram_chat_id
            or "your_telegram_chat_id" in self.telegram_chat_id.lower()
        )
        if telegram_missing:
            warnings.append(
                "TELEGRAM_BOT_TOKEN nebo TELEGRAM_CHAT_ID není nastaven.\n"
                "-> Nalezené nabídky se budou vypisovat do systémové konzole / logů Dockeru."
            )

        # Kontrola zadání
        if not self.user_query:
            errors.append("USER_QUERY je prázdný! Nastavte text hledání v .env.")

        if warnings:
            for warn in warnings:
                logging.warning(f"[CONFIG VAROVÁNÍ] {warn}")

        if errors:
            for err in errors:
                logging.error(f"[CONFIG CHYBA] {err}")
            raise ValueError("\n".join(errors))


# Globální instance konfigurace
config = Config()
