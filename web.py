"""
Webový dashboard a REST API pro Bazos Scraper & AI Evaluator.
Umožňuje sledování stavu, konfiguraci (Co, Kolik, Kdy), spouštění kontrol,
zobrazení TOP 3 nabídek za poslední měsíc a živé systémové logy.
Navrženo pro lokální běh a přístup přes Tailscale nebo lokální síť.
"""

import html
import json
import logging
import os
import re
import socket
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional

from flask import Flask, jsonify, request, render_template_string
import schedule

from config import config, update_env_file

logger = logging.getLogger("BazosWeb")

app = Flask(__name__)
# Vypnutí hlučných výchozích request logů werkzeugu pro čistší konzoli
logging.getLogger("werkzeug").setLevel(logging.WARNING)


def get_server_ips() -> Dict[str, str]:
    """Zjistí Tailscale IP adresu a lokální LAN IP adresu serveru."""
    ips = {"tailscale": "", "lan": "127.0.0.1"}

    # 1. Detekce Tailscale IP přes rozhraní tailscale0
    try:
        res = subprocess.run(
            ["ip", "-4", "addr", "show", "tailscale0"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", res.stdout)
        if match:
            ips["tailscale"] = match.group(1)
    except Exception:
        pass

    # 2. Detekce lokální IP adresy v síti
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ips["lan"] = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    return ips


def get_pipeline_instance():
    """Získá běžící pipeline instanci (nejdříve z __main__, pak z main)."""
    import sys
    main_mod = sys.modules.get("__main__")
    if main_mod and hasattr(main_mod, "get_pipeline"):
        p = main_mod.get_pipeline()
        if p is not None:
            return p
    try:
        from main import get_pipeline
        return get_pipeline()
    except Exception as e:
        logger.debug(f"Nelze načíst instanci pipeline: {e}")
        return None


def get_log_buffer_handler():
    """Získá log_buffer_handler z běžícího procesu."""
    import sys
    main_mod = sys.modules.get("__main__")
    if main_mod and hasattr(main_mod, "log_buffer_handler"):
        return getattr(main_mod, "log_buffer_handler")
    try:
        from main import log_buffer_handler
        return log_buffer_handler
    except Exception:
        return None


def get_seen_manager():
    """Vrátí instanci SeenAdsManager (buď z pipeline nebo lokální)."""
    p = get_pipeline_instance()
    if p and hasattr(p, "seen_manager"):
        return p.seen_manager

    # Fallback pro standalone běh web.py
    from main import SeenAdsManager
    return SeenAdsManager(config.seen_ids_file)


# =====================================================================
# REST API ENDPOINTY
# =====================================================================

@app.route("/api/status", methods=["GET"])
def api_status():
    """Vrátí aktuální provozní stav scraperu, plánovače a statistiky."""
    p = get_pipeline_instance()
    seen_mgr = get_seen_manager()

    is_running = p.is_running if p else True
    is_scraping = p.is_scraping if p else False

    # Příští naplánovaný běh
    next_run = None
    try:
        next_job = schedule.next_run()
        if next_job:
            next_run = next_job.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass

    # Čas posledního běhu
    last_run = None
    if p and p.last_run_time:
        last_run = p.last_run_time.strftime("%Y-%m-%d %H:%M:%S")

    # Kontrola aktivních hodin
    is_active_window = True
    if p:
        is_active_window = p.is_within_active_hours()

    server_ips = get_server_ips()
    stats = seen_mgr.get_stats()

    return jsonify({
        "status": "online",
        "is_running": is_running,
        "is_scraping": is_scraping,
        "is_active_window": is_active_window,
        "active_window": {
            "start": config.active_start_hour,
            "end": config.active_end_hour,
            "is_24_7": config.active_start_hour is None or config.active_end_hour is None,
        },
        "last_run": last_run,
        "last_run_stats": p.last_run_stats if p else {},
        "next_run": next_run,
        "stats": stats,
        "server_ips": server_ips,
        "web_port": config.web_port,
        "check_interval_minutes": config.check_interval_minutes,
        "score_threshold": config.score_threshold,
    })


@app.route("/api/config", methods=["GET"])
def api_get_config():
    """Vrátí aktuální konfiguraci scraperu pro formulář."""
    is_24_7 = config.active_start_hour is None or config.active_end_hour is None
    return jsonify({
        "user_query": config.user_query,
        "min_price": config.min_price if config.min_price is not None else "",
        "max_price": config.max_price if config.max_price is not None else "",
        "check_interval_minutes": config.check_interval_minutes,
        "score_threshold": config.score_threshold,
        "max_pages_to_scrape": config.max_pages_to_scrape,
        "active_start_hour": config.active_start_hour if config.active_start_hour is not None else 8,
        "active_end_hour": config.active_end_hour if config.active_end_hour is not None else 20,
        "is_24_7": is_24_7,
        "run_on_startup": config.run_on_startup,
        "mock_mode": config.mock_mode,
        "groq_model": config.groq_model,
        "web_port": config.web_port,
    })


@app.route("/api/config", methods=["POST"])
def api_update_config():
    """Uloží změněnou konfiguraci do .env a okamžitě ji přenačte v pipeline."""
    data = request.get_json() or {}

    updates: Dict[str, Any] = {}

    # 1. CO scrapovat
    if "user_query" in data:
        uq = str(data["user_query"]).strip()
        if not uq:
            return jsonify({"success": False, "error": "Zadání hledání (USER_QUERY) nesmí být prázdné."}), 400
        updates["USER_QUERY"] = uq

    if "min_price" in data:
        val = str(data["min_price"]).strip()
        updates["MIN_PRICE"] = int(val) if val.isdigit() else ""

    if "max_price" in data:
        val = str(data["max_price"]).strip()
        updates["MAX_PRICE"] = int(val) if val.isdigit() else ""

    # 2. KOLIK scrapovat
    if "check_interval_minutes" in data:
        try:
            interval = int(data["check_interval_minutes"])
            if interval < 1:
                interval = 1
            updates["CHECK_INTERVAL_MINUTES"] = interval
        except ValueError:
            pass

    if "score_threshold" in data:
        try:
            th = int(data["score_threshold"])
            if 1 <= th <= 10:
                updates["SCORE_THRESHOLD"] = th
        except ValueError:
            pass

    if "max_pages_to_scrape" in data:
        try:
            pages = int(data["max_pages_to_scrape"])
            if 1 <= pages <= 10:
                updates["MAX_PAGES_TO_SCRAPE"] = pages
        except ValueError:
            pass

    # 3. KDY scrapovat
    is_24_7 = bool(data.get("is_24_7", False))
    if is_24_7:
        updates["ACTIVE_START_HOUR"] = ""
        updates["ACTIVE_END_HOUR"] = ""
    else:
        if "active_start_hour" in data and str(data["active_start_hour"]).strip() != "":
            try:
                updates["ACTIVE_START_HOUR"] = int(data["active_start_hour"])
            except ValueError:
                pass
        if "active_end_hour" in data and str(data["active_end_hour"]).strip() != "":
            try:
                updates["ACTIVE_END_HOUR"] = int(data["active_end_hour"])
            except ValueError:
                pass

    if "run_on_startup" in data:
        updates["RUN_ON_STARTUP"] = bool(data["run_on_startup"])

    # Atomický zápis do .env
    try:
        update_env_file(updates)
        logger.info(f"Konfigurace úspěšně uložena do .env: {list(updates.keys())}")
    except Exception as e:
        logger.error(f"Chyba při ukládání .env: {e}")
        return jsonify({"success": False, "error": f"Ukládání selhalo: {e}"}), 500

    # Přenačtení konfigurace v běžící pipeline
    p = get_pipeline_instance()
    if p:
        try:
            p.reload_config()
        except Exception as e:
            logger.warning(f"Chyba při reload_config: {e}")

    return jsonify({"success": True, "message": "Konfigurace byla úspěšně uložena a aktivována."})


@app.route("/api/top_deals", methods=["GET"])
def api_top_deals():
    """Vrátí TOP N nabídek za posledních X dní (výchozí: TOP 3 za 30 dní)."""
    days = int(request.args.get("days", 30))
    limit = int(request.args.get("limit", 3))

    seen_mgr = get_seen_manager()
    deals = seen_mgr.get_top_deals(days=days, limit=limit)
    return jsonify({"success": True, "deals": deals, "days": days, "limit": limit})


@app.route("/api/all_deals", methods=["GET"])
def api_all_deals():
    """Vrátí všechny zpracované inzeráty s možností vyhledávání a filtru skóre."""
    min_score = int(request.args.get("min_score", 0))
    limit = int(request.args.get("limit", 60))
    search = request.args.get("search", "")

    seen_mgr = get_seen_manager()
    deals = seen_mgr.get_all_deals(min_score=min_score, limit=limit, search=search)
    return jsonify({"success": True, "deals": deals, "total": len(deals)})


@app.route("/api/run_now", methods=["POST"])
def api_run_now():
    """Spustí okamžitý vyhledávací cyklus na pozadí."""
    p = get_pipeline_instance()
    if not p:
        return jsonify({"success": False, "error": "Pipeline není inicializována."}), 503

    res = p.trigger_manual_run(force=True)
    return jsonify(res)


@app.route("/api/test_telegram", methods=["POST"])
def api_test_telegram():
    """Odešle testovací zprávu na Telegram."""
    p = get_pipeline_instance()
    notifier = p.notifier if p else None
    if not notifier:
        from notifier import TelegramNotifier
        notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)

    if not notifier.is_configured:
        return jsonify({
            "success": False,
            "error": "Telegram není nakonfigurován (chybí platný token nebo chat ID)."
        }), 400

    success = notifier.send_test_message()
    if success:
        return jsonify({"success": True, "message": "Testovací notifikace byla úspěšně odeslána na Telegram!"})
    else:
        return jsonify({"success": False, "error": "Odeslání zprávy na Telegram selhalo. Zkontrolujte logy."}), 500


@app.route("/api/send_top_telegram", methods=["POST"])
def api_send_top_telegram():
    """Odešle TOP 3 měsíční nabídky přímo na Telegram."""
    seen_mgr = get_seen_manager()
    top_deals = seen_mgr.get_top_deals(days=30, limit=3)

    if not top_deals:
        return jsonify({"success": False, "error": "V databázi za poslední měsíc nejsou žádné nabídky."}), 404

    p = get_pipeline_instance()
    notifier = p.notifier if p else None
    if not notifier:
        from notifier import TelegramNotifier
        notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)

    if not notifier.is_configured:
        return jsonify({
            "success": False,
            "error": "Telegram není nakonfigurován."
        }), 400

    success = notifier.send_top_deals(top_deals)
    if success:
        return jsonify({
            "success": True,
            "message": f"TOP {len(top_deals)} nabídky byly úspěšně odeslány na váš Telegram!"
        })
    else:
        return jsonify({"success": False, "error": "Odeslání na Telegram selhalo."}), 500


@app.route("/api/logs", methods=["GET"])
def api_logs():
    """Vrátí posledních N logovacích zpráv z paměťového bufferu."""
    limit = int(request.args.get("limit", 60))
    try:
        handler = get_log_buffer_handler()
        if handler and hasattr(handler, "buffer"):
            logs = list(handler.buffer)[-limit:]
            return jsonify({"success": True, "logs": logs})
        return jsonify({"success": True, "logs": []})
    except Exception as e:
        return jsonify({"success": False, "logs": [], "error": str(e)})


# =====================================================================
# HTML DASHBOARD ŠABLONA
# =====================================================================

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="cs">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bazoš Scraper AI - Ovládací Panel</title>
    <style>
        :root {
            --bg-body: #0b0f19;
            --bg-card: #111827;
            --bg-card-hover: #172033;
            --bg-input: #1f2937;
            --border-color: #374151;
            --text-main: #f3f4f6;
            --text-muted: #9ca3af;
            --accent-primary: #3b82f6;
            --accent-primary-hover: #2563eb;
            --accent-success: #10b981;
            --accent-warning: #f59e0b;
            --accent-danger: #ef4444;
            --gold: #f59e0b;
            --silver: #9ca3af;
            --bronze: #d97706;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-body);
            color: var(--text-main);
            line-height: 1.5;
            padding: 16px;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
        }

        /* HEADER */
        header {
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            background-color: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 18px 24px;
            margin-bottom: 24px;
            gap: 16px;
        }

        .brand {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .brand-icon {
            font-size: 28px;
            background: rgba(59, 130, 246, 0.15);
            width: 48px;
            height: 48px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 10px;
            border: 1px solid rgba(59, 130, 246, 0.3);
        }

        .brand-text h1 {
            font-size: 20px;
            font-weight: 700;
            letter-spacing: -0.02em;
        }

        .brand-text p {
            font-size: 13px;
            color: var(--text-muted);
        }

        .status-badge-container {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }

        .badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 13px;
            font-weight: 600;
            padding: 6px 14px;
            border-radius: 9999px;
            border: 1px solid transparent;
        }

        .badge-online {
            background-color: rgba(16, 185, 129, 0.15);
            color: #34d399;
            border-color: rgba(16, 185, 129, 0.3);
        }

        .badge-scraping {
            background-color: rgba(59, 130, 246, 0.15);
            color: #60a5fa;
            border-color: rgba(59, 130, 246, 0.3);
            animation: pulse 1.5s infinite;
        }

        .badge-sleeping {
            background-color: rgba(245, 158, 11, 0.15);
            color: #fbbf24;
            border-color: rgba(245, 158, 11, 0.3);
        }

        .pulse-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background-color: currentColor;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        /* NETWORK INFO BAR */
        .info-bar {
            background: rgba(31, 41, 55, 0.6);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 10px 16px;
            font-size: 13px;
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            gap: 12px;
        }

        .ip-chip {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: #1f2937;
            padding: 4px 10px;
            border-radius: 6px;
            border: 1px solid #4b5563;
            font-family: monospace;
            font-size: 12px;
            color: #93c5fd;
        }

        /* TOP 3 HERO SECTION */
        .section-title {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 16px;
        }

        .section-title h2 {
            font-size: 18px;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .top-deals-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 16px;
            margin-bottom: 28px;
        }

        .deal-card {
            background-color: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            position: relative;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            transition: transform 0.2s, border-color 0.2s;
        }

        .deal-card:hover {
            border-color: #4b5563;
            transform: translateY(-2px);
        }

        .deal-card.rank-1 {
            border-top: 4px solid var(--gold);
            background: linear-gradient(180deg, rgba(245, 158, 11, 0.05) 0%, var(--bg-card) 60%);
        }

        .deal-card.rank-2 {
            border-top: 4px solid var(--silver);
            background: linear-gradient(180deg, rgba(156, 163, 175, 0.05) 0%, var(--bg-card) 60%);
        }

        .deal-card.rank-3 {
            border-top: 4px solid var(--bronze);
            background: linear-gradient(180deg, rgba(217, 119, 6, 0.05) 0%, var(--bg-card) 60%);
        }

        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 12px;
            gap: 8px;
        }

        .medal-badge {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            font-size: 13px;
            font-weight: 700;
            padding: 4px 10px;
            border-radius: 6px;
        }

        .medal-1 { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }
        .medal-2 { background: rgba(156, 163, 175, 0.2); color: #d1d5db; border: 1px solid rgba(156, 163, 175, 0.4); }
        .medal-3 { background: rgba(217, 119, 6, 0.2); color: #f59e0b; border: 1px solid rgba(217, 119, 6, 0.4); }

        .score-pill {
            background: rgba(16, 185, 129, 0.2);
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.4);
            font-size: 13px;
            font-weight: 700;
            padding: 4px 10px;
            border-radius: 6px;
        }

        .card-title {
            font-size: 16px;
            font-weight: 600;
            line-height: 1.4;
            margin-bottom: 8px;
        }

        .card-title a {
            color: var(--text-main);
            text-decoration: none;
        }

        .card-title a:hover {
            color: var(--accent-primary);
            text-decoration: underline;
        }

        .card-price {
            font-size: 22px;
            font-weight: 800;
            color: #38bdf8;
            margin-bottom: 12px;
        }

        .card-meta {
            font-size: 12px;
            color: var(--text-muted);
            margin-bottom: 12px;
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
        }

        .card-summary {
            font-size: 13px;
            color: #d1d5db;
            background: rgba(31, 41, 55, 0.5);
            padding: 10px 12px;
            border-radius: 8px;
            margin-bottom: 16px;
            border-left: 3px solid var(--accent-primary);
        }

        .card-actions {
            display: flex;
            gap: 8px;
        }

        /* BUTTONS */
        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            padding: 8px 16px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            border: 1px solid transparent;
            transition: background 0.15s, border-color 0.15s, transform 0.1s;
            text-decoration: none;
        }

        .btn:active {
            transform: scale(0.98);
        }

        .btn-primary {
            background-color: var(--accent-primary);
            color: #ffffff;
        }

        .btn-primary:hover {
            background-color: var(--accent-primary-hover);
        }

        .btn-success {
            background-color: #059669;
            color: #ffffff;
        }

        .btn-success:hover {
            background-color: #10b981;
        }

        .btn-secondary {
            background-color: #374151;
            color: #f3f4f6;
            border-color: #4b5563;
        }

        .btn-secondary:hover {
            background-color: #4b5563;
        }

        .btn-sm {
            padding: 6px 12px;
            font-size: 12px;
        }

        /* SETTINGS FORM - CO / KOLIK / KDY */
        .settings-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 20px;
            margin-bottom: 24px;
        }

        .settings-card {
            background-color: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
        }

        .settings-card-header {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 16px;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 10px;
        }

        .settings-card-header h3 {
            font-size: 16px;
            font-weight: 700;
        }

        .form-group {
            margin-bottom: 14px;
        }

        .form-group:last-child {
            margin-bottom: 0;
        }

        label {
            display: block;
            font-size: 13px;
            font-weight: 600;
            color: #d1d5db;
            margin-bottom: 6px;
        }

        .help-text {
            font-size: 12px;
            color: var(--text-muted);
            margin-top: 4px;
        }

        input[type="text"],
        input[type="number"],
        textarea,
        select {
            width: 100%;
            background-color: var(--bg-input);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 10px 12px;
            color: var(--text-main);
            font-size: 14px;
            font-family: inherit;
            outline: none;
            transition: border-color 0.15s, box-shadow 0.15s;
        }

        input[type="text"]:focus,
        input[type="number"]:focus,
        textarea:focus,
        select:focus {
            border-color: var(--accent-primary);
            box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2);
        }

        textarea {
            resize: vertical;
            min-height: 105px;
        }

        .row-2 {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }

        .checkbox-label {
            display: flex;
            align-items: center;
            gap: 10px;
            cursor: pointer;
            font-size: 13px;
            user-select: none;
        }

        .checkbox-label input[type="checkbox"] {
            width: 18px;
            height: 18px;
            accent-color: var(--accent-primary);
            cursor: pointer;
        }

        .action-bar {
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            margin-bottom: 30px;
            background-color: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 16px 20px;
            align-items: center;
            justify-content: space-between;
        }

        /* TABS & ACTIVITY */
        .tabs {
            display: flex;
            gap: 8px;
            border-bottom: 1px solid var(--border-color);
            margin-bottom: 16px;
        }

        .tab-btn {
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: 14px;
            font-weight: 600;
            padding: 10px 16px;
            cursor: pointer;
            border-bottom: 2px solid transparent;
            transition: color 0.15s, border-color 0.15s;
        }

        .tab-btn:hover {
            color: var(--text-main);
        }

        .tab-btn.active {
            color: var(--accent-primary);
            border-bottom-color: var(--accent-primary);
        }

        .tab-pane {
            display: none;
        }

        .tab-pane.active {
            display: block;
        }

        /* LOG CONSOLE */
        .terminal-window {
            background-color: #030712;
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 14px;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 12px;
            color: #d1d5db;
            max-height: 380px;
            overflow-y: auto;
            line-height: 1.6;
        }

        .log-entry {
            display: flex;
            gap: 10px;
            word-break: break-all;
            padding: 2px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.03);
        }

        .log-time { color: #6b7280; flex-shrink: 0; }
        .log-level-INFO { color: #60a5fa; font-weight: 600; flex-shrink: 0; }
        .log-level-WARNING { color: #fbbf24; font-weight: 600; flex-shrink: 0; }
        .log-level-ERROR { color: #f87171; font-weight: 700; flex-shrink: 0; }

        /* ALL DEALS TABLE */
        .table-container {
            background-color: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            overflow-x: auto;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            text-align: left;
        }

        th {
            background-color: #1f2937;
            padding: 12px 16px;
            font-weight: 600;
            color: #d1d5db;
            border-bottom: 1px solid var(--border-color);
        }

        td {
            padding: 12px 16px;
            border-bottom: 1px solid var(--border-color);
        }

        tr:last-child td {
            border-bottom: none;
        }

        tr:hover td {
            background-color: var(--bg-card-hover);
        }

        /* TOAST NOTIFICATION */
        #toast {
            position: fixed;
            bottom: 24px;
            right: 24px;
            background-color: #1f2937;
            color: #ffffff;
            border: 1px solid #374151;
            padding: 12px 20px;
            border-radius: 8px;
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.5);
            font-size: 14px;
            font-weight: 500;
            display: none;
            z-index: 1000;
            animation: fadeIn 0.2s ease-out;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- HEADER -->
        <header>
            <div class="brand">
                <div class="brand-icon">🤖</div>
                <div class="brand-text">
                    <h1>Bazoš Scraper & AI Evaluator</h1>
                    <p>Inteligentní vyhodnocování výhodných nabídek v reálném čase</p>
                </div>
            </div>
            <div class="status-badge-container">
                <div id="statusBadge" class="badge badge-online">
                    <span class="pulse-dot"></span>
                    <span id="statusText">Načítám stav...</span>
                </div>
                <button class="btn btn-primary btn-sm" onclick="triggerManualRun()" id="btnRunNow">
                    🚀 Spustit vyhledávání ihned
                </button>
            </div>
        </header>

        <!-- NETWORK INFO BAR -->
        <div class="info-bar">
            <div>
                <strong>Tailscale přístup:</strong>
                <span class="ip-chip" id="tailscaleIpChip">http://...</span>
                <span style="margin: 0 8px; color: #4b5563;">|</span>
                <strong>Lokální síť:</strong>
                <span class="ip-chip" id="lanIpChip">http://...</span>
            </div>
            <div>
                <span id="nextRunInfo" style="color: var(--text-muted);">Příští kontrola: ...</span>
            </div>
        </div>

        <!-- HERO SECTION: TOP 3 ZA POSLEDNÍ MĚSÍC -->
        <div class="section-title">
            <h2>🏆 TOP 3 nejlepší nabídky za poslední měsíc</h2>
            <div style="display: flex; gap: 8px;">
                <button class="btn btn-secondary btn-sm" onclick="loadTopDeals()">
                    🔄 Obnovit
                </button>
                <button class="btn btn-success btn-sm" onclick="sendTopDealsToTelegram()">
                    📲 Odeslat TOP 3 do Telegramu
                </button>
            </div>
        </div>

        <div class="top-deals-grid" id="topDealsContainer">
            <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-muted); background: var(--bg-card); border-radius: 12px; border: 1px solid var(--border-color);">
                Načítám nejlepší nabídky...
            </div>
        </div>

        <!-- FORMULÁŘ NASTAVENÍ: CO, KOLIK, KDY -->
        <div class="section-title">
            <h2>⚙️ Nastavení scraperu (Co, Kolik, Kdy)</h2>
            <span style="font-size: 13px; color: var(--text-muted);">Změny se ihned uloží a aplikují v běžícím procesu</span>
        </div>

        <form id="configForm" onsubmit="saveConfig(event)">
            <div class="settings-grid">
                <!-- 1. CO SCRAPOVAT -->
                <div class="settings-card">
                    <div class="settings-card-header">
                        <span style="font-size: 20px;">🎯</span>
                        <h3>1. Co scrapovat?</h3>
                    </div>
                    <div class="form-group">
                        <label for="user_query">Zadání přirozeným jazykem (AI Prompt):</label>
                        <textarea id="user_query" name="user_query" required placeholder="Např. Levná 3D tiskárna Creality nebo Prusa, plně funkční..."></textarea>
                        <div class="help-text">Groq LLM si ze zadání sám odvodí klíčová a vylučovací slova.</div>
                    </div>
                    <div class="row-2">
                        <div class="form-group">
                            <label for="min_price">Minimální cena (Kč):</label>
                            <input type="number" id="min_price" name="min_price" placeholder="např. 500">
                        </div>
                        <div class="form-group">
                            <label for="max_price">Maximální cena (Kč):</label>
                            <input type="number" id="max_price" name="max_price" placeholder="např. 4500">
                        </div>
                    </div>
                </div>

                <!-- 2. KOLIK SCRAPOVAT -->
                <div class="settings-card">
                    <div class="settings-card-header">
                        <span style="font-size: 20px;">📄</span>
                        <h3>2. Kolik scrapovat?</h3>
                    </div>
                    <div class="form-group">
                        <label for="max_pages_to_scrape">Počet stránek ke stažení na dotaz:</label>
                        <select id="max_pages_to_scrape" name="max_pages_to_scrape">
                            <option value="1">1 stránka (nejnovějších 20 inzerátů)</option>
                            <option value="2">2 stránky (40 inzerátů)</option>
                            <option value="3">3 stránky (60 inzerátů)</option>
                            <option value="5">5 stránek (100 inzerátů)</option>
                        </select>
                        <div class="help-text">Doporučujeme 1–2 stránky pro rychlou a spolehlivou kontrolu.</div>
                    </div>
                    <div class="form-group">
                        <label for="score_threshold">Minimální AI skóre pro Telegram notifikaci:</label>
                        <select id="score_threshold" name="score_threshold">
                            <option value="7">7 / 10 (Dobré nabídky)</option>
                            <option value="8">8 / 10 (Skvělé nabídky – doporučeno)</option>
                            <option value="9">9 / 10 (Výjimečné úlovky)</option>
                            <option value="10">10 / 10 (Absolutní jackpot)</option>
                        </select>
                        <div class="help-text">Inzeráty s tímto a vyšším skóre obdržíte ihned do mobilu.</div>
                    </div>
                </div>

                <!-- 3. KDY SCRAPOVAT -->
                <div class="settings-card">
                    <div class="settings-card-header">
                        <span style="font-size: 20px;">⏰</span>
                        <h3>3. Kdy scrapovat?</h3>
                    </div>
                    <div class="form-group">
                        <label for="check_interval_minutes">Interval kontrol (minuty):</label>
                        <input type="number" id="check_interval_minutes" name="check_interval_minutes" min="1" max="1440" required>
                        <div class="help-text">Jak často automatický plánovač projde Bazoš (např. 30 minut).</div>
                    </div>
                    <div class="form-group">
                        <label class="checkbox-label">
                            <input type="checkbox" id="is_24_7" name="is_24_7" onchange="toggleHoursVisibility()">
                            <strong>Běžet nepřetržitě 24/7</strong>
                        </label>
                    </div>
                    <div class="row-2" id="hoursWindowRow">
                        <div class="form-group">
                            <label for="active_start_hour">Aktivní od (hodina):</label>
                            <input type="number" id="active_start_hour" name="active_start_hour" min="0" max="23" value="8">
                        </div>
                        <div class="form-group">
                            <label for="active_end_hour">Aktivní do (hodina):</label>
                            <input type="number" id="active_end_hour" name="active_end_hour" min="0" max="23" value="20">
                        </div>
                    </div>
                    <div class="form-group" style="margin-top: 10px;">
                        <label class="checkbox-label">
                            <input type="checkbox" id="run_on_startup" name="run_on_startup">
                            <span>Vynutit okamžitý běh při startu aplikace</span>
                        </label>
                    </div>
                </div>
            </div>

            <!-- ACTION BAR -->
            <div class="action-bar">
                <div style="display: flex; gap: 12px; align-items: center;">
                    <button type="submit" class="btn btn-primary" id="btnSaveConfig">
                        💾 Uložit nastavení
                    </button>
                    <button type="button" class="btn btn-secondary" onclick="testTelegram()">
                        🔔 Otestovat Telegram spojení
                    </button>
                </div>
                <div style="font-size: 13px; color: var(--text-muted);" id="statsSummary">
                    Načítám statistiky...
                </div>
            </div>
        </form>

        <!-- TABS: HISTORIE & ŽIVÉ LOGY -->
        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('tab-deals', this)">
                📋 Všechny nalezené inzeráty
            </button>
            <button class="tab-btn" onclick="switchTab('tab-logs', this)">
                📟 Živá systémová konzole (Logy)
            </button>
        </div>

        <!-- TAB 1: SEZNAM INZERÁTŮ -->
        <div id="tab-deals" class="tab-pane active">
            <div style="display: flex; gap: 12px; margin-bottom: 14px; flex-wrap: wrap;">
                <input type="text" id="dealSearchInput" placeholder="Hledat v názvu inzerátu..." style="max-width: 320px;" oninput="filterAllDeals()">
                <select id="scoreFilterSelect" style="max-width: 200px;" onchange="filterAllDeals()">
                    <option value="0">Všechna skóre</option>
                    <option value="7">Skóre 7 a vyšší</option>
                    <option value="8">Pouze TOP nabídky (8+)</option>
                </select>
                <button class="btn btn-secondary btn-sm" onclick="loadAllDeals()">Obnovit seznam</button>
            </div>
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Název inzerátu</th>
                            <th>Cena</th>
                            <th>AI Skóre</th>
                            <th>Nalezeno</th>
                            <th>Telegram</th>
                            <th>Akce</th>
                        </tr>
                    </thead>
                    <tbody id="allDealsTableBody">
                        <tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Načítám inzeráty...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- TAB 2: TERMINÁL LOGŮ -->
        <div id="tab-logs" class="tab-pane">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                <span style="font-size: 13px; color: var(--text-muted);">Posledních 60 událostí ze systémového logu:</span>
                <div style="display: flex; gap: 8px;">
                    <button class="btn btn-secondary btn-sm" onclick="loadLogs()">🔄 Obnovit logy</button>
                </div>
            </div>
            <div class="terminal-window" id="terminalLog">
                Načítám logy...
            </div>
        </div>
    </div>

    <!-- TOAST POPUP -->
    <div id="toast"></div>

    <script>
        let currentStatus = {};

        // Inicializace aplikace
        document.addEventListener('DOMContentLoaded', () => {
            loadStatus();
            loadConfig();
            loadTopDeals();
            loadAllDeals();
            loadLogs();

            // Pravidelná aktualizace každých 10 sekund
            setInterval(() => {
                loadStatus();
            }, 10000);

            // Pravidelná aktualizace logů každých 5 sekund
            setInterval(() => {
                if (document.getElementById('tab-logs').classList.contains('active')) {
                    loadLogs();
                }
            }, 5000);
        });

        function showToast(message, isError = false) {
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.style.borderColor = isError ? '#ef4444' : '#10b981';
            toast.style.display = 'block';
            setTimeout(() => {
                toast.style.display = 'none';
            }, 4000);
        }

        function switchTab(tabId, btn) {
            document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            document.getElementById(tabId).classList.add('active');
            btn.classList.add('active');
            if (tabId === 'tab-logs') {
                loadLogs();
            }
        }

        function toggleHoursVisibility() {
            const is24_7 = document.getElementById('is_24_7').checked;
            document.getElementById('hoursWindowRow').style.opacity = is24_7 ? '0.35' : '1';
            document.getElementById('active_start_hour').disabled = is24_7;
            document.getElementById('active_end_hour').disabled = is24_7;
        }

        // NAČTENÍ STAVU
        async function loadStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                currentStatus = data;

                const badge = document.getElementById('statusBadge');
                const statusText = document.getElementById('statusText');

                if (data.is_scraping) {
                    badge.className = 'badge badge-scraping';
                    statusText.textContent = 'Právě scrapuje a vyhodnocuje...';
                } else if (!data.is_active_window) {
                    badge.className = 'badge badge-sleeping';
                    statusText.textContent = 'Mimo aktivní hodiny (šetří tokeny)';
                } else {
                    badge.className = 'badge badge-online';
                    statusText.textContent = 'Online (Plánovač aktivní)';
                }

                // IP čipy
                const port = data.web_port || 5050;
                const tailscaleIp = data.server_ips.tailscale ? `http://${data.server_ips.tailscale}:${port}` : 'Tailscale nenalezeno';
                const lanIp = `http://${data.server_ips.lan}:${port}`;

                document.getElementById('tailscaleIpChip').textContent = tailscaleIp;
                document.getElementById('lanIpChip').textContent = lanIp;

                // Příští kontrola
                document.getElementById('nextRunInfo').textContent = data.next_run
                    ? `Příští kontrola: ${data.next_run}`
                    : `Interval kontroly: každých ${data.check_interval_minutes} min`;

                // Shrnutí statistik
                if (data.stats) {
                    document.getElementById('statsSummary').textContent = 
                        `Celkem prověřeno: ${data.stats.total_seen} | AI skórováno: ${data.stats.total_evaluated} | TOP úlovků: ${data.stats.total_deals}`;
                }
            } catch (err) {
                console.error('Chyba při načítání stavu:', err);
            }
        }

        // NAČTENÍ TOP 3 NABÍDEK ZA POSLEDNÍ MĚSÍC
        async function loadTopDeals() {
            const container = document.getElementById('topDealsContainer');
            try {
                const res = await fetch('/api/top_deals?limit=3&days=30');
                const data = await res.json();

                if (!data.deals || data.deals.length === 0) {
                    container.innerHTML = `
                        <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-muted); background: var(--bg-card); border-radius: 12px; border: 1px solid var(--border-color);">
                            ℹ️ V databázi za poslední měsíc zatím nejsou žádné ohodnocené inzeráty.<br>
                            Klikněte na <strong>"Spustit vyhledávání ihned"</strong> pro provedení prvního cyklu!
                        </div>
                    `;
                    return;
                }

                const medals = [
                    { label: '🥇 1. Místo', class: 'medal-1' },
                    { label: '🥈 2. Místo', class: 'medal-2' },
                    { label: '🥉 3. Místo', class: 'medal-3' }
                ];

                container.innerHTML = data.deals.map((deal, idx) => {
                    const medal = medals[idx] || { label: `#${idx + 1}`, class: 'medal-3' };
                    const priceFormatted = typeof deal.price === 'number'
                        ? `${deal.price.toLocaleString('cs-CZ')} Kč`
                        : (deal.price || 'Neuvedeno');

                    const dateStr = deal.seen_at ? deal.seen_at.substring(0, 10) : '';
                    const summaryHtml = deal.summary ? `<div class="card-summary">💡 ${escapeHtml(deal.summary)}</div>` : '';

                    return `
                        <div class="deal-card rank-${idx + 1}">
                            <div>
                                <div class="card-header">
                                    <span class="medal-badge ${medal.class}">${medal.label}</span>
                                    <span class="score-pill">⭐️ ${deal.score} / 10</span>
                                </div>
                                <h3 class="card-title">
                                    <a href="${deal.url}" target="_blank" rel="noopener noreferrer">${escapeHtml(deal.title || 'Inzerát bez názvu')} ↗</a>
                                </h3>
                                <div class="card-price">${priceFormatted}</div>
                                <div class="card-meta">
                                    <span>🗓 ${dateStr}</span>
                                    <span>${deal.notified ? '✅ Odesláno do Telegramu' : '⚪ Neodesláno'}</span>
                                </div>
                                ${summaryHtml}
                            </div>
                            <div class="card-actions">
                                <a href="${deal.url}" target="_blank" rel="noopener noreferrer" class="btn btn-secondary btn-sm" style="flex: 1;">
                                    Otevřít na Bazoš.cz ↗
                                </a>
                            </div>
                        </div>
                    `;
                }).join('');
            } catch (err) {
                container.innerHTML = `<div style="grid-column: 1/-1; color: var(--accent-danger);">Chyba při načítání nabídek: ${err.message}</div>`;
            }
        }

        // NAČTENÍ KONFIGURACE DO FORMULÁŘE
        async function loadConfig() {
            try {
                const res = await fetch('/api/config');
                const cfg = await res.json();

                document.getElementById('user_query').value = cfg.user_query || '';
                document.getElementById('min_price').value = cfg.min_price || '';
                document.getElementById('max_price').value = cfg.max_price || '';
                document.getElementById('check_interval_minutes').value = cfg.check_interval_minutes || 30;
                document.getElementById('score_threshold').value = cfg.score_threshold || 8;
                document.getElementById('max_pages_to_scrape').value = cfg.max_pages_to_scrape || 1;
                document.getElementById('active_start_hour').value = cfg.active_start_hour !== undefined ? cfg.active_start_hour : 8;
                document.getElementById('active_end_hour').value = cfg.active_end_hour !== undefined ? cfg.active_end_hour : 20;
                document.getElementById('is_24_7').checked = !!cfg.is_24_7;
                document.getElementById('run_on_startup').checked = !!cfg.run_on_startup;

                toggleHoursVisibility();
            } catch (err) {
                console.error('Chyba při načítání konfigurace:', err);
            }
        }

        // ULOŽENÍ KONFIGURACE
        async function saveConfig(e) {
            e.preventDefault();
            const btn = document.getElementById('btnSaveConfig');
            btn.disabled = true;
            btn.textContent = 'Ukládám...';

            const payload = {
                user_query: document.getElementById('user_query').value,
                min_price: document.getElementById('min_price').value,
                max_price: document.getElementById('max_price').value,
                check_interval_minutes: document.getElementById('check_interval_minutes').value,
                score_threshold: document.getElementById('score_threshold').value,
                max_pages_to_scrape: document.getElementById('max_pages_to_scrape').value,
                is_24_7: document.getElementById('is_24_7').checked,
                active_start_hour: document.getElementById('active_start_hour').value,
                active_end_hour: document.getElementById('active_end_hour').value,
                run_on_startup: document.getElementById('run_on_startup').checked,
            };

            try {
                const res = await fetch('/api/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (data.success) {
                    showToast('✅ Nastavení bylo úspěšně uloženo a aplikováno!');
                    loadStatus();
                } else {
                    showToast(`❌ Chyba: ${data.error || 'Neznámá chyba'}`, true);
                }
            } catch (err) {
                showToast(`❌ Chyba sítě: ${err.message}`, true);
            } finally {
                btn.disabled = false;
                btn.textContent = '💾 Uložit nastavení';
            }
        }

        // SPUŠTĚNÍ KONTROLY IHNED
        async function triggerManualRun() {
            const btn = document.getElementById('btnRunNow');
            btn.disabled = true;
            btn.textContent = '⏳ Spouštím...';

            try {
                const res = await fetch('/api/run_now', { method: 'POST' });
                const data = await res.json();
                if (data.status === 'started') {
                    showToast('🚀 Kontrolní vyhledávací cyklus byl spuštěn na pozadí!');
                    loadStatus();
                } else {
                    showToast(`ℹ️ ${data.message || 'Nelze spustit'}`);
                }
            } catch (err) {
                showToast(`❌ Chyba: ${err.message}`, true);
            } finally {
                btn.disabled = false;
                btn.textContent = '🚀 Spustit vyhledávání ihned';
            }
        }

        // TEST TELEGRAMU
        async function testTelegram() {
            showToast('📤 Odesílám testovací zprávu na Telegram...');
            try {
                const res = await fetch('/api/test_telegram', { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    showToast('✅ Testovací notifikace dorazila na váš Telegram!');
                } else {
                    showToast(`❌ Chyba Telegramu: ${data.error}`, true);
                }
            } catch (err) {
                showToast(`❌ Chyba: ${err.message}`, true);
            }
        }

        // ODESLÁNÍ TOP 3 NA TELEGRAM
        async function sendTopDealsToTelegram() {
            showToast('📤 Odesílám TOP 3 nabídky na Telegram...');
            try {
                const res = await fetch('/api/send_top_telegram', { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    showToast('✅ TOP 3 nabídky byly úspěšně doručeny na Telegram!');
                } else {
                    showToast(`❌ Chyba: ${data.error}`, true);
                }
            } catch (err) {
                showToast(`❌ Chyba spojení: ${err.message}`, true);
            }
        }

        // HISTORIE VŠECH INZERÁTŮ
        let allDealsCache = [];

        async function loadAllDeals() {
            const tbody = document.getElementById('allDealsTableBody');
            tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Načítám inzeráty...</td></tr>';
            try {
                const res = await fetch('/api/all_deals?limit=100');
                const data = await res.json();
                allDealsCache = data.deals || [];
                renderAllDealsTable(allDealsCache);
            } catch (err) {
                tbody.innerHTML = `<tr><td colspan="7" style="color: var(--accent-danger);">Chyba: ${err.message}</td></tr>`;
            }
        }

        function filterAllDeals() {
            const query = document.getElementById('dealSearchInput').value.toLowerCase();
            const minScore = parseInt(document.getElementById('scoreFilterSelect').value, 10);

            const filtered = allDealsCache.filter(deal => {
                const matchesQuery = !query || (deal.title && deal.title.toLowerCase().includes(query));
                const matchesScore = (deal.score || 0) >= minScore;
                return matchesQuery && matchesScore;
            });
            renderAllDealsTable(filtered);
        }

        function renderAllDealsTable(deals) {
            const tbody = document.getElementById('allDealsTableBody');
            if (deals.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">Nenalezeny žádné inzeráty odpovídající filtru.</td></tr>';
                return;
            }

            tbody.innerHTML = deals.map(deal => {
                const scoreBadge = deal.score >= 8
                    ? `<span style="color: #34d399; font-weight: 700;">⭐️ ${deal.score}/10</span>`
                    : (deal.score > 0 ? `<span>${deal.score}/10</span>` : `<span style="color: #6b7280;">-</span>`);

                const priceFormatted = typeof deal.price === 'number'
                    ? `${deal.price.toLocaleString('cs-CZ')} Kč`
                    : (deal.price || 'Neuvedeno');

                const dateStr = deal.seen_at ? deal.seen_at.substring(0, 16).replace('T', ' ') : '';

                return `
                    <tr>
                        <td style="font-family: monospace; color: var(--text-muted);">${deal.id}</td>
                        <td><strong>${escapeHtml(deal.title || 'Bez názvu')}</strong></td>
                        <td style="font-weight: 600; color: #38bdf8;">${priceFormatted}</td>
                        <td>${scoreBadge}</td>
                        <td style="color: var(--text-muted); font-size: 12px;">${dateStr}</td>
                        <td>${deal.notified ? '✅ Ano' : '⚪ Ne'}</td>
                        <td>
                            <a href="${deal.url}" target="_blank" rel="noopener noreferrer" class="btn btn-secondary btn-sm">Bazoš ↗</a>
                        </td>
                    </tr>
                `;
            }).join('');
        }

        // NAČTENÍ LOGŮ
        async function loadLogs() {
            const terminal = document.getElementById('terminalLog');
            try {
                const res = await fetch('/api/logs?limit=70');
                const data = await res.json();
                if (!data.logs || data.logs.length === 0) {
                    terminal.textContent = 'Zatím nebyly zaznamenány žádné logy.';
                    return;
                }

                terminal.innerHTML = data.logs.map(log => {
                    const levelClass = `log-level-${log.level}`;
                    return `
                        <div class="log-entry">
                            <span class="log-time">[${log.timestamp || ''}]</span>
                            <span class="${levelClass}">[${log.level}]</span>
                            <span>${escapeHtml(log.message || '')}</span>
                        </div>
                    `;
                }).join('');

                terminal.scrollTop = terminal.scrollHeight;
            } catch (err) {
                terminal.textContent = `Chyba při načítání logů: ${err.message}`;
            }
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
    </script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    """Hlavní dashboard stránka."""
    return render_template_string(DASHBOARD_HTML)


def run_web_server(host: str = "0.0.0.0", port: int = 5050):
    """Spustí webový server aplikace."""
    logger.info(f"Spouštím webový server na {host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    # Samostatné spuštění webového serveru
    logging.basicConfig(level=logging.INFO)
    port = config.web_port or 5050
    logger.info(f"Spouštím standalone web server na http://0.0.0.0:{port}")
    run_web_server(host="0.0.0.0", port=port)
