"""
Konfigurační modul pro Bazos Scraper & AI Evaluator.
Načítá a validuje nastavení z proměnných prostředí a souboru .env.
"""

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any
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

    # 7. Časové okno aktivity (šetření Groq API tokenů)
    active_start_hour: Optional[int] = _parse_optional_int("ACTIVE_START_HOUR")
    active_end_hour: Optional[int] = _parse_optional_int("ACTIVE_END_HOUR")
    run_on_startup: bool = os.getenv("RUN_ON_STARTUP", "false").lower() in ("true", "1", "yes")

    # 8. Cesty a perzistence
    bazos_base_url: str = os.getenv("BAZOS_BASE_URL", "https://www.bazos.cz").rstrip("/")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # 9. Lokální Webové rozhraní (Dashboard)
    web_enabled: bool = os.getenv("WEB_ENABLED", "true").lower() in ("true", "1", "yes")
    web_host: str = os.getenv("WEB_HOST", "0.0.0.0").strip()
    web_port: int = _parse_optional_int("WEB_PORT") or 5050

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
        self.targets_file = self.data_dir / "targets.json"

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

    def reload(self) -> None:
        """Znovu načte proměnné prostředí ze souboru .env a obnoví atributy konfigurace."""
        env_path = Path(__file__).resolve().parent / ".env"
        load_dotenv(dotenv_path=env_path, override=True)
        self.groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
        self.groq_model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.user_query = os.getenv("USER_QUERY", self.user_query).strip()
        self.min_price = _parse_optional_int("MIN_PRICE")
        self.max_price = _parse_optional_int("MAX_PRICE")
        self.check_interval_minutes = _parse_optional_int("CHECK_INTERVAL_MINUTES") or 30
        self.score_threshold = _parse_optional_int("SCORE_THRESHOLD") or 8
        self.max_pages_to_scrape = _parse_optional_int("MAX_PAGES_TO_SCRAPE") or 1
        self.mock_mode = os.getenv("MOCK_MODE", "false").lower() in ("true", "1", "yes")
        self.active_start_hour = _parse_optional_int("ACTIVE_START_HOUR")
        self.active_end_hour = _parse_optional_int("ACTIVE_END_HOUR")
        self.run_on_startup = os.getenv("RUN_ON_STARTUP", "false").lower() in ("true", "1", "yes")
        self.web_enabled = os.getenv("WEB_ENABLED", "true").lower() in ("true", "1", "yes")
        self.web_host = os.getenv("WEB_HOST", "0.0.0.0").strip()
        self.web_port = _parse_optional_int("WEB_PORT") or 5050


def update_env_file(updates: Dict[str, Any], env_file_path: Optional[Path] = None) -> None:
    """
    Aktualizuje vybrané klíče v souboru .env se zachováním stávajících komentářů a struktury.
    Po aktualizaci automaticky přenačte globální konfiguraci.
    """
    env_path = env_file_path or (Path(__file__).resolve().parent / ".env")
    if not env_path.exists():
        lines = []
    else:
        lines = env_path.read_text(encoding="utf-8").splitlines()

    updated_keys = set()
    new_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped or "=" not in stripped:
            new_lines.append(line)
            continue

        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            val = updates[key]
            updated_keys.add(key)
            if val is None or val == "":
                new_lines.append(f"{key}=")
            elif isinstance(val, bool):
                new_lines.append(f"{key}={'true' if val else 'false'}")
            elif isinstance(val, (int, float)):
                new_lines.append(f"{key}={val}")
            else:
                str_val = str(val).strip()
                # Zabalit do uvozovek pokud obsahuje mezery nebo speciální znaky
                safe_val = str_val.replace('"', '\\"')
                new_lines.append(f'{key}="{safe_val}"')
        else:
            new_lines.append(line)

    # Přidat klíče, které v souboru ještě nebyly
    for key, val in updates.items():
        if key not in updated_keys:
            if val is None or val == "":
                new_lines.append(f"{key}=")
            elif isinstance(val, bool):
                new_lines.append(f"{key}={'true' if val else 'false'}")
            elif isinstance(val, (int, float)):
                new_lines.append(f"{key}={val}")
            else:
                str_val = str(val).strip()
                safe_val = str_val.replace('"', '\\"')
                new_lines.append(f'{key}="{safe_val}"')

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    config.reload()


# Globální instance konfigurace
config = Config()
