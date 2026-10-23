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
6. **Běh 24/7 (PM2 / Docker / systémová služba):**
   - Automatický plánovač (`schedule`) spouští kontrolu ve zvoleném intervalu (např. každých 30 minut).
7. **Lokální webový dashboard (Tailscale / LAN):**
   - Responzivní webový ovládací panel na portu 5050.
   - Možnost naklikat **Co** (zadání, min/max cena), **Kolik** (počet stránek, práh skóre) a **Kdy** (interval, 24/7 vs denní hodiny).
   - Zobrazení **🏆 TOP 3 nabídek za poslední měsíc** s možností odeslání do Telegramu na jedno kliknutí.
   - Živá systémová konzole s výpisem událostí a tlačítko pro okamžité spuštění kontroly.

---

## 📁 Struktura projektu

```text
Bazar-scraper-ai/
├── Dockerfile              # Lehký Python 3.11-slim Docker image
├── docker-compose.yml      # Správa kontejneru s perzistentním volume pro data
├── ecosystem.config.js     # PM2 konfigurace pro automatický start a restart
├── requirements.txt        # Závislosti projektu (groq, requests, bs4, schedule, flask, ...)
├── .dockerignore           # Vyloučení zbytečných souborů z Docker buildu
├── .env                    # Vaše aktivní konfigurace (API klíče, zadání, filtry)
├── .env.example            # Vzorový konfigurační soubor s nápovědou
├── config.py               # Načítání a validace konfigurace s typovou kontrolou
├── scraper.py              # Bazoš scraper s ošetřením kódování a hrubým filtrem
├── groq_client.py          # Groq API klient s retry logikou a fallbackem
├── notifier.py             # Telegram notifikátor s HTML formátováním a obrázky
├── web.py                  # Lokální Flask webový dashboard a REST API
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

## 🌐 Lokální Webový Dashboard (Tailscale / LAN)

Aplikace má integrovaný webový dashboard běžící na portu `5050`:

- **Přístup přes Tailscale:** `http://<Vase-Tailscale-IP>:5050` (např. `http://100.107.207.42:5050`)
- **Přístup v lokální síti:** `http://<IP-serveru>:5050` (např. `http://192.168.0.104:5050`)
- **Přímo na serveru:** `http://localhost:5050`

### Co dashboard umožňuje:
- **🏆 TOP 3 nabídky za poslední měsíc:** Zlatá, stříbrná a bronzová medaile pro nejvýhodnější úlovky podle AI hodnocení, s možností poslat je do Telegramu na jedno kliknutí.
- **🎯 Co scrapovat:** Úprava přirozeného AI promptu a cenového rozptylu (Min/Max Kč).
- **📄 Kolik scrapovat:** Počet procházených stránek a práh skóre pro okamžité notifikace (např. 8/10).
- **⏰ Kdy scrapovat:** Nastavení periody v minutách a volba mezi nepřetržitým režimem 24/7 nebo časovým oknem (např. 08:00–20:00 pro úsporu API tokenů).
- **🚀 Okamžitá kontrola:** Tlačítko pro spuštění vyhledávacího cyklu ihned bez čekání na plánovač.
- **🔔 Test Telegramu:** Ověření spojení s Telegram botem.
- **📟 Živý terminál:** Zobrazení posledních systémových logů přímo v prohlížeči.

---

## 🧪 Spuštění testů

Projekt obsahuje kompletní sadu automatických testů:

```bash
pytest tests/ -v
```