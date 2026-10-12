"""
Bazos.cz scraper modul.
Zajišťuje stahování, spolehlivé dekódování českého textu, parsování inzerátů a hrubou filtraci.
"""

import logging
import re
import time
import urllib.parse
from typing import Dict, Any, List, Optional, Tuple, Set
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class BazosScraper:
    """Stahuje a parsuje inzeráty z Bazoš.cz s ošetřením kódování a předběžnou filtrací."""

    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "cs,sk,en;q=0.9",
        "Connection": "keep-alive",
    }

    def __init__(self, base_url: str = "https://www.bazos.cz", request_delay: float = 1.0):
        self.base_url = base_url.rstrip("/")
        self.request_delay = request_delay
        self.session = requests.Session()
        self.session.headers.update(self.DEFAULT_HEADERS)

    def _decode_response(self, response: requests.Response) -> str:
        """
        Bazoš.cz servíruje data v UTF-8 nebo Windows-1250 (CP1250).
        Tato metoda spolehlivě zachovává českou diakritiku (háčky a čárky).
        """
        # 1. Zkusit striktní UTF-8
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError:
            pass

        # 2. Zkusit Windows-1250 (tradiční české kódování starších stránek)
        try:
            return response.content.decode("windows-1250")
        except UnicodeDecodeError:
            pass

        # 3. Zkusit ISO-8859-2
        try:
            return response.content.decode("iso-8859-2")
        except UnicodeDecodeError:
            pass

        # 4. Záložní řešení přes zjištěné kódování
        fallback = response.apparent_encoding or "windows-1250"
        return response.content.decode(fallback, errors="replace")

    def _extract_id_from_url(self, url: str) -> Optional[str]:
        """Extrahuje číselné ID inzerátu z URL adresy (např. /inzerat/224577904/...)."""
        match = re.search(r"/inzerat/(\d+)/", url)
        if match:
            return match.group(1)
        fallback_match = re.search(r"\b(\d{7,10})\b", url)
        return fallback_match.group(1) if fallback_match else None

    def _parse_price(self, price_raw: str) -> Tuple[Optional[int], str]:
        """Převede text ceny na celočíselnou částku a hezky formátovaný text."""
        cleaned = price_raw.strip().replace("\xa0", " ")
        if not cleaned:
            return None, "Neuvedeno"

        lowered = cleaned.lower()
        if "dohodou" in lowered:
            return None, "Dohodou"
        if "zdarma" in lowered:
            return 0, "Zdarma"
        if "v textu" in lowered:
            return None, "V textu"

        # Extrakce číslic
        digits_only = re.sub(r"[^\d]", "", cleaned)
        if digits_only:
            try:
                price_val = int(digits_only)
                return price_val, f"{price_val:,} Kč".replace(",", " ")
            except ValueError:
                pass

        return None, cleaned

    def fetch_ad_detail(self, ad_url: str) -> Optional[str]:
        """Stáhne kompletní detailní text inzerátu z jeho cílové stránky."""
        try:
            time.sleep(self.request_delay)
            resp = self.session.get(ad_url, timeout=12)
            if resp.status_code != 200:
                logger.warning(f"Nepodařilo se načíst detail inzerátu {ad_url}: HTTP {resp.status_code}")
                return None

            html = self._decode_response(resp)
            soup = BeautifulSoup(html, "html.parser")
            popis_div = soup.find("div", class_="popisdetail") or soup.find("div", class_="popis")
            if popis_div:
                return popis_div.get_text(separator="\n", strip=True)
            return None
        except Exception as e:
            logger.warning(f"Chyba při stahování detailu inzerátu {ad_url}: {e}")
            return None

    def search(
        self,
        query: str,
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
        page: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Vyhledá zadaný dotaz na Bazoš.cz a vrátí seznam naparsovaných inzerátů.
        page: číslo stránky (0 = 1. stránka, 1 = 2. stránka crp=20, atd.)
        """
        params = {
            "hledat": query,
            "hlokalita": "",
            "humkreis": "25",
            "cenaod": str(min_price) if min_price is not None else "",
            "cenado": str(max_price) if max_price is not None else "",
            "Submit": "Hledat",
        }
        if page > 0:
            params["crp"] = str(page * 20)

        url = f"{self.base_url}/search.php"
        logger.info(f"Stahuji Bazoš stránku {page + 1} pro dotaz '{query}'...")

        try:
            time.sleep(self.request_delay)
            response = self.session.get(url, params=params, timeout=15)
            if response.status_code != 200:
                logger.error(f"Chyba při stahování vyhledávání: HTTP {response.status_code}")
                return []

            html = self._decode_response(response)
            soup = BeautifulSoup(html, "html.parser")

            items = soup.find_all("div", class_="inzeraty")
            parsed_ads: List[Dict[str, Any]] = []

            for item in items:
                # 1. Titulek a odkaz
                title_elem = item.find("h2", class_="nadpis")
                if not title_elem:
                    continue
                link_elem = title_elem.find("a")
                if not link_elem:
                    continue

                title = link_elem.get_text(strip=True)
                raw_url = link_elem.get("href", "")
                ad_url = urllib.parse.urljoin(self.base_url, raw_url)
                ad_id = self._extract_id_from_url(ad_url)
                if not ad_id:
                    continue

                # 2. URL náhledu obrázku
                img_elem = item.find("img", class_="obrazek")
                image_url = img_elem.get("src") if img_elem else None
                if image_url and not image_url.startswith("http"):
                    image_url = urllib.parse.urljoin(self.base_url, image_url)

                # 3. Zkrácený popis ze stránky vyhledávání
                popis_elem = item.find("div", class_="popis")
                description = popis_elem.get_text(separator=" ", strip=True) if popis_elem else ""

                # 4. Cena
                cena_elem = item.find("div", class_="inzeratycena")
                price_text = cena_elem.get_text(strip=True) if cena_elem else ""
                price_val, price_str = self._parse_price(price_text)

                # 5. Lokalita
                lok_elem = item.find("div", class_="inzeratylok")
                location = lok_elem.get_text(separator=" ", strip=True) if lok_elem else ""

                # 6. Datum / TOP označení
                date_elem = item.find("span", class_="velikost10")
                date_str = date_elem.get_text(strip=True) if date_elem else ""

                parsed_ads.append({
                    "id": ad_id,
                    "title": title,
                    "url": ad_url,
                    "price": price_val,
                    "price_str": price_str,
                    "description": description,
                    "location": location,
                    "date": date_str,
                    "image_url": image_url,
                })

            logger.info(f"Nalezeno {len(parsed_ads)} inzerátů na straně {page + 1} pro dotaz '{query}'.")
            return parsed_ads

        except Exception as e:
            logger.error(f"Chyba při scrapování dotazu '{query}': {e}", exc_info=True)
            return []

    def coarse_filter(
        self,
        ad: Dict[str, Any],
        seen_ids: Set[str],
        excluded_keywords: List[str],
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Rychlá předběžná filtrace v Pythonu před voláním AI modelu.
        Kontroluje:
        1. Deduplikaci (ID již v lokální paměti)
        2. Vylučovací klíčová slova v titulku i popisu (např. 'koupím', 'na díly')
        3. Tvrdé cenové mantinely
        Vrací: (vyhovuje: bool, důvod_zamítnutí: str)
        """
        ad_id = str(ad.get("id"))

        # 1. Kontrola deduplikace
        if ad_id in seen_ids:
            return False, f"Již viděno (ID {ad_id})"

        title_lower = (ad.get("title") or "").lower()
        desc_lower = (ad.get("description") or "").lower()
        combined_text = f"{title_lower} {desc_lower}"

        # 2. Kontrola vylučovacích slov a frází
        for keyword in excluded_keywords:
            kw_clean = keyword.lower().strip()
            if not kw_clean:
                continue

            # Pokud jde o víceslovnou frázi (např. 'na díly')
            if " " in kw_clean:
                if kw_clean in combined_text:
                    return False, f"Obsahuje vyloučenou frázi: '{kw_clean}'"
            else:
                # Jednoslovná kontrola s ohraničením slov, aby se zabránilo falešným shodám
                pattern = rf"(?:\b|_){re.escape(kw_clean)}(?:\b|_)"
                if re.search(pattern, combined_text, flags=re.IGNORECASE):
                    return False, f"Obsahuje vyloučené slovo: '{kw_clean}'"

        # 3. Kontrola cenových limitů
        price = ad.get("price")
        if price is not None:
            if min_price is not None and price < min_price:
                return False, f"Cena {price} Kč je pod minimem {min_price} Kč"
            if max_price is not None and price > max_price:
                return False, f"Cena {price} Kč je nad maximem {max_price} Kč"

        return True, "Splňuje hrubý filtr"
