# Bazos Scraper & AI Evaluator (Groq API + Telegram)

Plně automatizovaná, kontejnerizovaná pipeline v Pythonu pro nepřetržité sledování inzertního serveru **Bazoš.cz**, sémantické vyhodnocování výhodnosti nabídek pomocí **Groq API** (`llama-3.3-70b-versatile`) a okamžité odesílání nejlepších úlovků (skóre 8/10+) přes **Telegram Bot API**.

---

## 🚀 Hlavní funkce

1. **AI strategie vyhledávání z volného textu:**
   - Zadáte přirozeným jazykem, co hledáte (např. *"Herní notebook s RTX 3060, min. 16 GB RAM do 18 000 Kč"*).
   - Groq LLM automaticky vytvoří optimalizovaná klíčová slova pro Bazoš a sadu negativních/vylučovacích slov (*koupím, na díly, nefunkční, prasklý...*).
2. **Spolehlivý scraper Bazoš.cz:**
   - Robustní dekódování české diakritiky (UTF-8 i Windows-1250).
   - Bezchybné stránkování a stahování plného znění inzerátů.
3. **Dvoustupňová filtrace:**
   - **Hrubý filtr v kódu:** okamžité vyřazení podle ceny a vylučovacích slov bez zbytečného čerpání API tokenů.
   - **Sémantický AI filtr:** Groq model analyzuje poměr cena/výkon, výbavu, stav a rizika, přiřadí skóre 1–10 a odhadne férovou tržní cenu.
4. **Lokální deduplikace paměti (`data/seen_ids.json`):**
   - Inzeráty se nikdy nehodnotí ani neposílají dvakrát.
   - Atomický zápis na disk chrání před poškozením databáze.
5. **Okamžité Telegram notifikace:**
   - Inzeráty se skóre 8/10+ jsou okamžitě doručeny na váš telefon včetně fotky, výhod, rizik a přímého odkazu.
6. **Běh 24/7 v Docker kontejneru:**
   - Automatický plánovač (`schedule`) spouští kontrolu ve zvoleném intervalu (např. každých 30 minut).

---

## 📁 Struktura projektu

```text
Bazar-scraper-ai/
├── Dockerfile              # Lehký Python 3.11-slim Docker image
├── docker-compose.yml      # Správa kontejneru s perzistentním volume pro data
├── requirements.txt        # Závislosti projektu (groq, requests, bs4, schedule, ...)
├── .dockerignore           # Vyloučení zbytečných souborů z Docker buildu
├── .env                    # Vaše aktivní konfigurace (API klíče, zadání, filtry)
├── .env.example            # Vzorový konfigurační soubor s nápovědou
├── config.py               # Načítání a validace konfigurace s typovou kontrolou
├── scraper.py              # Bazoš scraper s ošetřením kódování a hrubým filtrem
├── groq_client.py          # Groq API klient s retry logikou a fallbackem
├── notifier.py             # Telegram notifikátor s HTML formátováním a obrázky
├── main.py                 # Hlavní pipeline orchestrátor s 24/7 plánovačem
├── tests/
│   └── test_pipeline.py    # Sada jednotkových testů (pytest)
└── data/
    └── seen_ids.json       # Perzistentní JSON paměť zpracovaných ID inzerátů
```

---

## ⚙️ Konfigurace (`.env`)

Vytvořte nebo upravte soubor `.env` podle vzoru:

```ini
# 1. Groq API (získejte zdarma na https://console.groq.com/keys)
GROQ_API_KEY=gsk_vas_groq_api_klic
GROQ_MODEL=llama-3.3-70b-versatile

# 2. Telegram Bot (vytvořte přes @BotFather na Telegramu)
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
TELEGRAM_CHAT_ID=123456789

# 3. Volné zadání uživatele (Co chcete koupit a za jakých podmínek)
USER_QUERY="Herní notebook s grafickou kartou RTX 3060 nebo lepší, minimálně 16 GB RAM, procesor AMD Ryzen nebo Intel i5/i7 (11. gen a novější), bez poškození a vad, cena do 18000 Kč"

# 4. Hrubý filtr cen (v Kč) - volitelné
MIN_PRICE=5000
MAX_PRICE=20000

# 5. Plánovač a filtrace
CHECK_INTERVAL_MINUTES=30
SCORE_THRESHOLD=8
MAX_PAGES_TO_SCRAPE=1

# 6. Režim simulace / Mock (nastavte na true pro otestování bez Groq klíče)
MOCK_MODE=false
```

> 💡 **Tip:** Pokud zatím nemáte Telegram bot token, nechte pole prázdná – aplikace poběží normálně a všechny nalezené nabídky přehledně vypíše do konzole / Docker logu.

---

## 🐳 Spuštění přes Docker Compose (Doporučeno)

Projekt je připraven k okamžitému spuštění na jedno kliknutí:

```bash
# 1. Spuštění kontejneru na pozadí
docker compose up -d --build

# 2. Sledování živých logů
docker compose logs -f

# 3. Zastavení kontejneru
docker compose down
```

Data o viděných inzerátech se ukládají do složky `./data/seen_ids.json`, která je namapovaná do kontejneru, takže o historii nepřijdete ani při restartu kontejneru.

---

## 💻 Spuštění lokálně bez Dockeru

```bash
# 1. Vytvoření virtuálního prostředí (volitelné)
python -m venv venv
# Windows:
.\venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# 2. Instalace závislostí
pip install -r requirements.txt

# 3. Spuštění
python main.py
```

---

## 🧪 Spuštění testů

Projekt obsahuje sadu automatických testů:

```bash
pytest tests/test_pipeline.py -v
```