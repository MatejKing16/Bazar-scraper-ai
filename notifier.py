"""
Telegram Bot notifikační modul pro Bazos Scraper & AI Evaluator.
Zajišťuje odesílání strukturovaných zpráv o výhodných inzerátech přes Telegram Bot API.
"""

import html
import logging
from typing import Dict, Any, List, Optional
import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Správce Telegram Bot notifikací s podporou formátování HTML a obrázků."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"

        # Detekce platných přihlašovacích údajů
        self.is_configured = bool(
            self.bot_token
            and self.chat_id
            and "your_telegram" not in self.bot_token.lower()
            and "your_telegram" not in self.chat_id.lower()
        )

        if not self.is_configured:
            logger.warning(
                "TelegramNotifier: Chybí TELEGRAM_BOT_TOKEN nebo TELEGRAM_CHAT_ID. "
                "Notifikace se budou simulovaně vypisovat do konzole."
            )

    def test_connection(self) -> bool:
        """Ověří spojení s Telegram Bot API pomocí metody getMe."""
        if not self.is_configured:
            return False
        try:
            resp = requests.get(f"{self.api_base}/getMe", timeout=10)
            data = resp.json()
            if data.get("ok"):
                bot_user = data.get("result", {}).get("username", "BazosBot")
                logger.info(f"Telegram Bot úspěšně připojen jako @{bot_user}!")
                return True
            else:
                logger.error(f"Telegram getMe selhalo: {data.get('description')}")
                return False
        except Exception as e:
            logger.error(f"Chyba při testování Telegram spojení: {e}")
            return False

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Odešle textovou zprávu (HTML nebo plain text) do zadaného Telegram chatu."""
        if not self.is_configured:
            logger.info(f"\n[SIMULACE TELEGRAM ZPRÁVY]\n{text}\n{'-'*50}")
            return True

        url = f"{self.api_base}/sendMessage"
        # Omezení délky zprávy v Telegramu je 4096 znaků
        safe_text = text if len(text) <= 4000 else text[:3990] + "\n\n<i>[Zkráceno]</i>"
        payload = {
            "chat_id": self.chat_id,
            "text": safe_text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": False,
        }

        try:
            resp = requests.post(url, json=payload, timeout=15)
            data = resp.json()
            if not data.get("ok"):
                logger.error(f"Telegram sendMessage selhalo: {data.get('description')}")
                return False
            return True
        except Exception as e:
            logger.error(f"Chyba při odesílání Telegram zprávy: {e}")
            return False

    def send_deal_alert(self, ad: Dict[str, Any], evaluation: Dict[str, Any]) -> bool:
        """
        Zformátuje a odešle upozornění na vysoce hodnocený inzerát (score >= 8/10).
        Pokud je k dispozici obrázek, odešle jej přes sendPhoto s popiskem.
        Při selhání nebo absenci obrázku odešle standardní HTML zprávu.
        """
        score = evaluation.get("score", 0)
        stars = "⭐️" * max(1, min(5, (score // 2) + (1 if score % 2 else 0)))

        title = html.escape(ad.get("title", "Bez názvu"))
        url = ad.get("url", "")
        price_str = html.escape(ad.get("price_str", "Neuvedena"))
        location = html.escape(ad.get("location", "Neuvedena"))
        date = html.escape(ad.get("date", ""))
        summary = html.escape(evaluation.get("summary", ""))
        fair_price = html.escape(str(evaluation.get("estimated_fair_price", "N/A")))

        # Formátování výhod
        pros = evaluation.get("pros", [])
        pros_text = ""
        if pros:
            pros_text = "\n<b>✅ Výhody:</b>\n" + "\n".join(f"• {html.escape(str(p))}" for p in pros[:4])

        # Formátování rizik a nevýhod
        cons = evaluation.get("cons", [])
        cons_text = ""
        if cons:
            cons_text = "\n<b>⚠️ Rizika / Nevýhody:</b>\n" + "\n".join(f"• {html.escape(str(c))}" for c in cons[:3])

        caption = (
            f"🎯 <b>NALEZEN VÝHODNÝ INZERÁT ({score}/10)</b> {stars}\n\n"
            f"📌 <b><a href=\"{url}\">{title}</a></b>\n"
            f"💰 <b>Cena:</b> {price_str} <i>(Odhad férové ceny: {fair_price})</i>\n"
            f"📍 <b>Lokalita:</b> {location}\n"
            f"🗓 <b>Datum:</b> {date}\n\n"
            f"💡 <b>Hodnocení AI:</b>\n{summary}\n"
            f"{pros_text}"
            f"{cons_text}\n\n"
            f"🔗 <a href=\"{url}\">Otevřít inzerát na Bazoš.cz</a>"
        )

        image_url = ad.get("image_url")

        # Pokus o odeslání s fotografií
        if self.is_configured and image_url and image_url.startswith("http"):
            send_photo_url = f"{self.api_base}/sendPhoto"
            # Omezení popisku fotky v Telegramu je 1024 znaků
            photo_caption = caption if len(caption) <= 1000 else caption[:995] + "..."
            payload = {
                "chat_id": self.chat_id,
                "photo": image_url,
                "caption": photo_caption,
                "parse_mode": "HTML",
            }
            try:
                resp = requests.post(send_photo_url, json=payload, timeout=15)
                data = resp.json()
                if data.get("ok"):
                    logger.info(f"Odeslána foto notifikace pro inzerát {ad.get('id')}.")
                    return True
                else:
                    logger.warning(
                        f"Telegram sendPhoto selhalo ({data.get('description')}). "
                        "Přepínám na textovou zprávu..."
                    )
            except Exception as e:
                logger.warning(f"sendPhoto vyvolalo výjimku: {e}. Přepínám na sendMessage...")

        # Záložní odeslání textové zprávy
        return self.send_message(caption)

    def send_startup_notification(
        self,
        search_queries: List[str],
        excluded_keywords: List[str],
        user_query: str,
    ) -> bool:
        """Odešle uvítací potvrzovací zprávu při startu monitoringu."""
        queries_str = ", ".join(f"<code>{html.escape(q)}</code>" for q in search_queries[:5])
        negatives_count = len(excluded_keywords)

        msg = (
            "🚀 <b>Bazoš Scraper & AI Evaluator spuštěn!</b>\n\n"
            f"📋 <b>Vaše zadání:</b>\n<i>{html.escape(user_query)}</i>\n\n"
            f"🔍 <b>Hledané dotazy:</b> {queries_str}\n"
            f"🛑 <b>Vylučovací slova:</b> {negatives_count} aktivních filtrů\n"
            "⏰ <b>Režim:</b> Běží 24/7 s automatickým plánovačem.\n\n"
            "Při nalezení inzerátu s hodnocením <b>8/10 a vyšším</b> obdržíte okamžité upozornění."
        )
        return self.send_message(msg)
