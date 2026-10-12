"""
Groq API klientský modul pro Bazos Scraper & AI Evaluator.
Zajišťuje optimalizaci vyhledávacích dotazů, generování negativních slov
a sémantické vyhodnocení inzerátů pomocí modelu llama-3.3-70b-versatile.
"""

import json
import logging
import re
import time
from typing import Dict, Any, List, Optional
from groq import Groq, APIConnectionError, RateLimitError, APIStatusError

logger = logging.getLogger(__name__)


class GroqEvaluator:
    """Wrapper nad Groq API pro generování strategie vyhledávání a AI hodnocení inzerátů."""

    DEFAULT_NEGATIVE_KEYWORDS = [
        "koupím",
        "poptávám",
        "sháním",
        "vyměním",
        "na díly",
        "na součástky",
        "nefunkční",
        "vadný",
        "prasklý",
        "blokovaný",
        "poškozený",
        "chybí",
        "vada",
    ]

    def __init__(
        self,
        api_key: str,
        model: str = "llama-3.3-70b-versatile",
        mock_mode: bool = False,
    ):
        self.api_key = api_key
        self.model = model
        self.mock_mode = mock_mode

        if not self.mock_mode:
            self.client = Groq(api_key=api_key)
        else:
            self.client = None
            logger.info("GroqEvaluator běží v režimu MOCK (simulace bez reálného Groq API).")

    def _extract_json(self, text: str) -> Dict[str, Any]:
        """Bezpečně extrahuje a parsuje JSON z textové odpovědi jazykového modelu."""
        text = text.strip()

        # 1. Přímý pokus o parsování
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 2. Hledání bloku ```json ... ```
        json_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # 3. Hledání prvního '{' a posledního '}'
        brace_match = re.search(r"(\{[\s\S]*\})", text)
        if brace_match:
            try:
                return json.loads(brace_match.group(1))
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Nepodařilo se dekódovat validní JSON z odpovědi: {text[:250]}...")

    def _call_with_retry(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.2,
        max_retries: int = 3,
    ) -> str:
        """Volá Groq API s automatickým ošetřením rate-limitů a výpadků sítě."""
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                chat_completion = self.client.chat.completions.create(
                    messages=messages,
                    model=self.model,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                )
                return chat_completion.choices[0].message.content or "{}"
            except RateLimitError as e:
                wait_time = attempt * 5
                logger.warning(
                    f"Překročen Groq Rate Limit (pokus {attempt}/{max_retries}). "
                    f"Čekám {wait_time} s... Chyba: {e}"
                )
                time.sleep(wait_time)
                last_error = e
            except (APIConnectionError, APIStatusError) as e:
                wait_time = attempt * 3
                logger.warning(
                    f"Chyba sítě/API Groq (pokus {attempt}/{max_retries}). "
                    f"Čekám {wait_time} s... Chyba: {e}"
                )
                time.sleep(wait_time)
                last_error = e
            except Exception as e:
                logger.error(f"Neočekávaná chyba při volání Groq API: {e}")
                raise e

        raise RuntimeError(f"Volání Groq API selhalo i po {max_retries} pokusech: {last_error}")

    def generate_search_strategy(self, user_query: str) -> Dict[str, Any]:
        """
        Převede volné zadání uživatele na klíčová slova pro Bazoš.cz a seznam vylučovacích slov.
        Vrací slovník: search_queries, excluded_keywords, min_price, max_price, explanation.
        """
        if self.mock_mode:
            return self._mock_generate_search_strategy(user_query)

        system_prompt = (
            "Jsi expertní analytik pro nákup zboží na českém inzertním portálu Bazoš.cz. "
            "Uživatel ti zadá volný text s tím, co hledá, své požadavky a rozpočet. "
            "Tvým úkolem je analyzovat zadání a vytvořit optimální strategii vyhledávání:\n"
            "1. Vygeneruj 2 až 4 nejvhodnější krátké vyhledávací dotazy pro Bazoš (search_queries). "
            "Vyhledávač Bazoše funguje nejlépe s 1 až 3 konkrétními slovy (např. 'rtx 3060', 'legion 5', 'thinkpad').\n"
            "2. Vygeneruj seznam vylučovacích slov a frází (excluded_keywords) v češtině, která indikují nevhodné nabídky "
            "(např. 'koupím', 'poptávám', 'sháním', 'vyměním', 'na díly', 'nefunkční', 'vadný', 'prasklý', 'blokovaný').\n"
            "3. Extrahuj cenové rozmezí (min_price, max_price v Kč jako celá čísla nebo null, pokud není specifikováno).\n"
            "4. Napiš krátké vysvětlení strategie v češtině.\n\n"
            "Odpověz VÝHRADNĚ ve formátu JSON s následující strukturou:\n"
            "{\n"
            '  "search_queries": ["dotaz 1", "dotaz 2"],\n'
            '  "excluded_keywords": ["koupím", "na díly", "nefunkční"],\n'
            '  "min_price": 5000,\n'
            '  "max_price": 20000,\n'
            '  "explanation": "Stručný komentář ke strategii"\n'
            "}"
        )

        user_content = f"Uživatelské zadání ke koupi: {user_query}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        logger.info("Generuji strategii vyhledávání přes Groq API...")
        raw_response = self._call_with_retry(messages, temperature=0.2)
        parsed = self._extract_json(raw_response)

        # Ošetření výstupních dotazů
        search_queries = parsed.get("search_queries") or []
        if isinstance(search_queries, str):
            search_queries = [search_queries]
        cleaned_queries = [q.strip() for q in search_queries if q and q.strip()]

        # Pokud LLM nevrátil žádný dotaz, použijeme první klíčová slova ze zadání
        if not cleaned_queries:
            cleaned_queries = [user_query[:40].strip()]

        # Sloučení vylučovacích slov s výchozími
        excluded_keywords = parsed.get("excluded_keywords") or []
        if isinstance(excluded_keywords, str):
            excluded_keywords = [excluded_keywords]

        all_excluded = list(
            set(
                [k.lower().strip() for k in excluded_keywords + self.DEFAULT_NEGATIVE_KEYWORDS if k.strip()]
            )
        )

        strategy = {
            "search_queries": cleaned_queries,
            "excluded_keywords": all_excluded,
            "min_price": parsed.get("min_price"),
            "max_price": parsed.get("max_price"),
            "explanation": parsed.get("explanation", "Strategie vygenerována modelem Groq."),
        }

        logger.info(f"Optimalizované vyhledávací dotazy: {strategy['search_queries']}")
        logger.info(f"Aktivních vylučovacích slov/frází: {len(strategy['excluded_keywords'])}")
        return strategy

    def evaluate_ad(self, ad: Dict[str, Any], user_query: str) -> Dict[str, Any]:
        """
        Sémanticky vyhodnotí jeden inzerát vůči požadavkům uživatele pomocí Groq LLM.
        Vrací: score (1-10), is_deal (bool), summary, pros, cons, estimated_fair_price, reasoning.
        """
        if self.mock_mode:
            return self._mock_evaluate_ad(ad, user_query)

        system_prompt = (
            "Jsi přísný a zkušený nákupní poradce a hodnotitel nabídek na Bazoš.cz. "
            "Analyzuješ konkrétní inzerát a porovnává se se zadanými požadavky uživatele.\n"
            "Pravidla hodnocení:\n"
            "- Přísně hodnoť poměr cena/výkon, stav zboží, relevanci ke specifikaci a důvěryhodnost inzerátu.\n"
            "- Škála skóre je 1 až 10 (celé číslo):\n"
            "  * 10: Extrémně výhodná nabídka / kup roku, přesně odpovídá zadání za skvělou cenu, bez skrytých vad.\n"
            "  * 8 - 9: Velmi výhodná nabídka, doporučeno koupit, splňuje parametry zadání.\n"
            "  * 5 - 7: Běžná tržní nabídka nebo drobné kompromisy vůči požadavkům.\n"
            "  * 1 - 4: Nevýhodná nabídka, předražená, poškozená, neodpovídá parametrům, nebo podezřelý inzerát.\n"
            "- is_deal je true právě tehdy, když je score >= 8.\n"
            "- pros: seznam hlavních výhod inzerátu (česky).\n"
            "- cons: seznam nevýhod, rizik nebo kompromisů (česky).\n"
            "- summary: stručné 1-2 větné shrnutí pro rychlé rozhodnutí kupujícího.\n"
            "- estimated_fair_price: realistický odhad férové tržní ceny zboží (např. '15 000 Kč').\n\n"
            "Odpověz VÝHRADNĚ ve formátu JSON s následující strukturou:\n"
            "{\n"
            '  "score": 8,\n'
            '  "is_deal": true,\n'
            '  "summary": "Skvělá nabídka notebooku v plné výbavě...",\n'
            '  "pros": ["Výborná cena", "Záruka ještě 6 měsíců", "16 GB RAM"],\n'
            '  "cons": ["Pouze osobní předání"],\n'
            '  "estimated_fair_price": "16 000 Kč",\n'
            '  "reasoning": "Důkladnější vysvětlení hodnocení"\n'
            "}"
        )

        user_content = (
            f"=== POŽADAVKY KUPUJÍCÍHO ===\n{user_query}\n\n"
            f"=== ANALYZOVANÝ INZERÁT ===\n"
            f"Titulek: {ad.get('title', '')}\n"
            f"Cena: {ad.get('price_str', '')} (číselně: {ad.get('price', 'N/A')} Kč)\n"
            f"Lokalita: {ad.get('location', '')}\n"
            f"Datum vložení: {ad.get('date', '')}\n"
            f"Odkaz: {ad.get('url', '')}\n"
            f"Popis inzerátu:\n{ad.get('description', '')}\n"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        logger.info(f"Hodnotím inzerát ID {ad.get('id')} ('{ad.get('title')[:35]}...') přes Groq...")
        try:
            raw_response = self._call_with_retry(messages, temperature=0.1)
            parsed = self._extract_json(raw_response)

            score = int(parsed.get("score", 5))
            score = max(1, min(10, score))
            is_deal = score >= 8

            return {
                "score": score,
                "is_deal": is_deal,
                "summary": parsed.get("summary", "Bez shrnutí"),
                "pros": parsed.get("pros", []),
                "cons": parsed.get("cons", []),
                "estimated_fair_price": parsed.get("estimated_fair_price", "Neuvedeno"),
                "reasoning": parsed.get("reasoning", ""),
            }
        except Exception as e:
            logger.error(f"Chyba při vyhodnocování inzerátu {ad.get('id')}: {e}")
            return {
                "score": 0,
                "is_deal": False,
                "summary": f"Chyba při evaluaci: {e}",
                "pros": [],
                "cons": [],
                "estimated_fair_price": "N/A",
                "reasoning": str(e),
            }

    # =========================================================================
    # Fallback / Mock metody pro lokální testování bez nutnosti aktivního Groq klíče
    # =========================================================================

    def _mock_generate_search_strategy(self, user_query: str) -> Dict[str, Any]:
        """Generuje realistickou strategii bez volání API pro testovací režim."""
        terms = []
        for word in ["rtx 3060", "rtx 3070", "herní notebook", "gaming notebook", "ryzen", "lenovo legion"]:
            if word in user_query.lower():
                terms.append(word)

        if not terms:
            terms = [user_query[:30].strip()]

        return {
            "search_queries": terms[:3],
            "excluded_keywords": self.DEFAULT_NEGATIVE_KEYWORDS,
            "min_price": 5000,
            "max_price": 20000,
            "explanation": "MOCK REŽIM: Automaticky vygenerované dotazy pro testovací běh.",
        }

    def _mock_evaluate_ad(self, ad: Dict[str, Any], user_query: str) -> Dict[str, Any]:
        """Provádí heuristické hodnocení v mock režimu."""
        title = (ad.get("title") or "").lower()
        desc = (ad.get("description") or "").lower()
        price = ad.get("price")

        score = 6
        if any(w in title or w in desc for w in ["rtx 3060", "rtx 3070", "rtx 4060"]):
            score += 2
        if price and price <= 18000:
            score += 1
        if any(w in desc for w in ["záruka", "faktura", "jako nový"]):
            score += 1

        score = max(1, min(10, score))
        is_deal = score >= 8

        return {
            "score": score,
            "is_deal": is_deal,
            "summary": f"MOCK Evaluace: Inzerát {ad.get('title')} ohodnocen na {score}/10.",
            "pros": ["Odpovídá hledaným specifikacím", "Dobrý stav dle popisu"],
            "cons": ["Ověřte funkčnost při osobním převzetí"],
            "estimated_fair_price": f"{price} Kč" if price else "16 000 Kč",
            "reasoning": "Heuristické mock vyhodnocení pro testovací účely.",
        }
