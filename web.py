"""
Webový dashboard a REST API pro Bazos Scraper & AI Evaluator.
Umožňuje sledování stavu, správu vícero aktivních hledání (Multi-Target Search),
konfiguraci plánovače, spouštění kontrol, zobrazení TOP 3 nabídek za poslední měsíc
a živé systémové logy. Navrženo pro lokální běh a přístup přes Tailscale nebo lokální síť.
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
logging.getLogger("werkzeug").setLevel(logging.WARNING)


def get_server_ips() -> Dict[str, str]:
    """Zjistí Tailscale IP adresu a lokální LAN IP adresu serveru."""
    ips = {"tailscale": "", "lan": "127.0.0.1"}

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

    from main import SeenAdsManager
    return SeenAdsManager(config.seen_ids_file)


def get_targets_manager():
    """Vrátí instanci TargetManager (buď z pipeline nebo lokální)."""
    p = get_pipeline_instance()
    if p and hasattr(p, "targets_manager"):
        return p.targets_manager

    from targets_manager import TargetManager
    return TargetManager(
        config.targets_file,
        default_query=config.user_query,
        default_min_price=config.min_price,
        default_max_price=config.max_price,
        default_score_threshold=config.score_threshold,
    )


# =====================================================================
# REST API ENDPOINTY
# =====================================================================

@app.route("/api/status", methods=["GET"])
def api_status():
    """Vrátí provozní stav scraperu, aktivní cíle a statistiky."""
    p = get_pipeline_instance()
    seen_mgr = get_seen_manager()
    tm = get_targets_manager()

    is_running = p.is_running if p else True
    is_scraping = p.is_scraping if p else False

    next_run = None
    try:
        next_job = schedule.next_run()
        if next_job:
            next_run = next_job.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass

    last_run = None
    if p and p.last_run_time:
        last_run = p.last_run_time.strftime("%Y-%m-%d %H:%M:%S")

    is_active_window = True
    if p:
        is_active_window = p.is_within_active_hours()

    server_ips = get_server_ips()
    stats = seen_mgr.get_stats()
    all_targets = tm.get_all_targets()
    active_targets = tm.get_active_targets()

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
        "active_targets_count": len(active_targets),
        "total_targets_count": len(all_targets),
        "targets": [{"id": t["id"], "name": t.get("name", "Hledání"), "is_active": t.get("is_active", True)} for t in all_targets],
    })


# --- SPRÁVA VYHLEDÁVACÍCH CÍLŮ (MULTI-TARGET) ---

@app.route("/api/targets", methods=["GET"])
def api_get_targets():
    """Vrátí všechny vyhledávací cíle."""
    tm = get_targets_manager()
    targets = tm.get_all_targets()
    return jsonify({"success": True, "targets": targets})


@app.route("/api/targets", methods=["POST"])
def api_add_target():
    """Vytvoří nový vyhledávací profil."""
    data = request.get_json() or {}
    name = str(data.get("name", "")).strip()
    query = str(data.get("query", "")).strip()

    if not name:
        return jsonify({"success": False, "error": "Název hledání nesmí být prázdný."}), 400
    if not query:
        return jsonify({"success": False, "error": "Zadání pro AI nesmí být prázdné."}), 400

    min_price = None
    if str(data.get("min_price", "")).strip().isdigit():
        min_price = int(data["min_price"])

    max_price = None
    if str(data.get("max_price", "")).strip().isdigit():
        max_price = int(data["max_price"])

    score_threshold = None
    if str(data.get("score_threshold", "")).strip().isdigit():
        score_threshold = int(data["score_threshold"])

    is_active = bool(data.get("is_active", True))

    tm = get_targets_manager()
    target = tm.add_target(
        name=name,
        query=query,
        min_price=min_price,
        max_price=max_price,
        score_threshold=score_threshold,
        is_active=is_active,
    )

    # Předgenerovat AI strategii
    p = get_pipeline_instance()
    if p and hasattr(p, "groq") and p.groq:
        try:
            strategy = p.groq.generate_search_strategy(query)
            if min_price is not None:
                strategy["min_price"] = min_price
            if max_price is not None:
                strategy["max_price"] = max_price
            tm.update_strategy(target["id"], strategy)
            target["strategy"] = strategy
        except Exception as e:
            logger.warning(f"Nelze ihned vygenerovat strategii pro cíl {name}: {e}")

    return jsonify({"success": True, "target": target})


@app.route("/api/targets/<target_id>", methods=["PUT"])
def api_update_target(target_id):
    """Aktualizuje existující vyhledávací profil."""
    data = request.get_json() or {}
    updates: Dict[str, Any] = {}

    if "name" in data:
        updates["name"] = data["name"]
    if "query" in data:
        updates["query"] = data["query"]
    if "min_price" in data:
        val = str(data["min_price"]).strip()
        updates["min_price"] = int(val) if val.isdigit() else None
    if "max_price" in data:
        val = str(data["max_price"]).strip()
        updates["max_price"] = int(val) if val.isdigit() else None
    if "score_threshold" in data:
        val = str(data["score_threshold"]).strip()
        updates["score_threshold"] = int(val) if val.isdigit() else None
    if "is_active" in data:
        updates["is_active"] = bool(data["is_active"])

    tm = get_targets_manager()
    target = tm.update_target(target_id, updates)
    if not target:
        return jsonify({"success": False, "error": "Cíl nenalezen."}), 404

    # Pokud bylo změněno zadání, znovu vygenerujeme strategii
    p = get_pipeline_instance()
    if p and hasattr(p, "groq") and p.groq and "query" in updates:
        try:
            strategy = p.groq.generate_search_strategy(target["query"])
            if target.get("min_price") is not None:
                strategy["min_price"] = target["min_price"]
            if target.get("max_price") is not None:
                strategy["max_price"] = target["max_price"]
            tm.update_strategy(target_id, strategy)
            target["strategy"] = strategy
        except Exception as e:
            logger.warning(f"Nelze aktualizovat strategii pro cíl {target_id}: {e}")

    return jsonify({"success": True, "target": target})


@app.route("/api/targets/<target_id>/toggle", methods=["POST"])
def api_toggle_target(target_id):
    """Přepne stav aktivní/pozastaveno pro vybrané hledání."""
    tm = get_targets_manager()
    target = tm.toggle_target(target_id)
    if not target:
        return jsonify({"success": False, "error": "Cíl nenalezen."}), 404
    return jsonify({"success": True, "target": target})


@app.route("/api/targets/<target_id>", methods=["DELETE"])
def api_delete_target(target_id):
    """Smaže vyhledávací profil."""
    tm = get_targets_manager()
    success = tm.delete_target(target_id)
    if not success:
        return jsonify({"success": False, "error": "Cíl nenalezen."}), 404
    return jsonify({"success": True, "message": "Hledání bylo úspěšně smazáno."})


# --- SYSTÉMOVÁ KONFIGURACE (PLÁNOVAČ, HODINY) ---

@app.route("/api/config", methods=["GET"])
def api_get_config():
    """Vrátí globální konfiguraci plánovače."""
    is_24_7 = config.active_start_hour is None or config.active_end_hour is None
    return jsonify({
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
    """Uloží globální konfiguraci do .env a přenačte ji."""
    data = request.get_json() or {}
    updates: Dict[str, Any] = {}

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

    try:
        update_env_file(updates)
    except Exception as e:
        logger.error(f"Chyba při ukládání .env: {e}")
        return jsonify({"success": False, "error": f"Ukládání selhalo: {e}"}), 500

    p = get_pipeline_instance()
    if p:
        try:
            p.reload_config()
        except Exception as e:
            logger.warning(f"Chyba při reload_config: {e}")

    return jsonify({"success": True, "message": "Nastavení plánovače bylo úspěšně uloženo."})


# --- NABÍDKY & LOGY ---

@app.route("/api/top_deals", methods=["GET"])
def api_top_deals():
    """Vrátí TOP N nabídek za posledních X dní (volitelně filtrováno podle target_id)."""
    days = int(request.args.get("days", 30))
    limit = int(request.args.get("limit", 3))
    target_id = request.args.get("target_id") or None

    seen_mgr = get_seen_manager()
    deals = seen_mgr.get_top_deals(days=days, limit=limit, target_id=target_id)
    return jsonify({"success": True, "deals": deals, "days": days, "limit": limit, "target_id": target_id})


@app.route("/api/all_deals", methods=["GET"])
def api_all_deals():
    """Vrátí všechny zpracované inzeráty s filtrem skóre, hledáním a target_id."""
    min_score = int(request.args.get("min_score", 0))
    limit = int(request.args.get("limit", 60))
    search = request.args.get("search", "")
    target_id = request.args.get("target_id") or None

    seen_mgr = get_seen_manager()
    deals = seen_mgr.get_all_deals(min_score=min_score, limit=limit, search=search, target_id=target_id)
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
    target_id = request.args.get("target_id") or None
    top_deals = seen_mgr.get_top_deals(days=30, limit=3, target_id=target_id)

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
# HTML DASHBOARD ŠABLONA S MULTI-TARGET PODPOROU
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

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-body);
            color: var(--text-main);
            line-height: 1.5;
            padding: 16px;
        }

        .container { max-width: 1200px; margin: 0 auto; }

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

        .brand { display: flex; align-items: center; gap: 12px; }

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

        .brand-text h1 { font-size: 20px; font-weight: 700; letter-spacing: -0.02em; }
        .brand-text p { font-size: 13px; color: var(--text-muted); }

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

        .badge-online { background-color: rgba(16, 185, 129, 0.15); color: #34d399; border-color: rgba(16, 185, 129, 0.3); }
        .badge-scraping { background-color: rgba(59, 130, 246, 0.15); color: #60a5fa; border-color: rgba(59, 130, 246, 0.3); animation: pulse 1.5s infinite; }
        .badge-sleeping { background-color: rgba(245, 158, 11, 0.15); color: #fbbf24; border-color: rgba(245, 158, 11, 0.3); }

        .pulse-dot { width: 8px; height: 8px; border-radius: 50%; background-color: currentColor; }

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

        /* SECTION TITLES */
        .section-title {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 16px;
            flex-wrap: wrap;
            gap: 10px;
        }

        .section-title h2 {
            font-size: 18px;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        /* TARGETS (MULTI-SEARCH) GRID */
        .targets-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
            gap: 16px;
            margin-bottom: 28px;
        }

        .target-card {
            background-color: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            transition: border-color 0.15s, transform 0.15s;
        }

        .target-card:hover { border-color: #4b5563; }
        .target-card.paused { opacity: 0.6; filter: grayscale(30%); }

        .target-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 10px;
            gap: 10px;
        }

        .target-title {
            font-size: 17px;
            font-weight: 700;
            color: #f9fafb;
        }

        .target-query-box {
            background: rgba(31, 41, 55, 0.4);
            border: 1px solid rgba(75, 85, 99, 0.4);
            border-radius: 8px;
            padding: 10px 12px;
            font-size: 13px;
            color: #d1d5db;
            margin-bottom: 12px;
            line-height: 1.4;
            max-height: 80px;
            overflow-y: auto;
        }

        .target-meta {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 14px;
            font-size: 12px;
        }

        .meta-pill {
            background: #1f2937;
            padding: 3px 8px;
            border-radius: 6px;
            border: 1px solid #374151;
            color: #93c5fd;
        }

        .target-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-top: 1px solid rgba(55, 65, 81, 0.6);
            padding-top: 12px;
            gap: 8px;
        }

        /* TOGGLE SWITCH */
        .toggle-switch {
            position: relative;
            display: inline-flex;
            align-items: center;
            cursor: pointer;
            font-size: 13px;
            font-weight: 600;
            user-select: none;
            gap: 8px;
        }

        .switch-input { display: none; }

        .switch-slider {
            width: 38px;
            height: 20px;
            background-color: #4b5563;
            border-radius: 20px;
            position: relative;
            transition: background-color 0.2s;
        }

        .switch-slider::before {
            content: "";
            position: absolute;
            width: 14px;
            height: 14px;
            border-radius: 50%;
            background-color: #ffffff;
            top: 3px;
            left: 3px;
            transition: transform 0.2s;
        }

        .switch-input:checked + .switch-slider { background-color: var(--accent-success); }
        .switch-input:checked + .switch-slider::before { transform: translateX(18px); }

        /* TOP 3 HERO SECTION */
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

        .deal-card:hover { border-color: #4b5563; transform: translateY(-2px); }

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

        .target-tag {
            display: inline-block;
            font-size: 11px;
            font-weight: 600;
            color: #a78bfa;
            background: rgba(139, 92, 246, 0.15);
            border: 1px solid rgba(139, 92, 246, 0.3);
            padding: 2px 8px;
            border-radius: 4px;
            margin-bottom: 6px;
        }

        .card-title {
            font-size: 16px;
            font-weight: 600;
            line-height: 1.4;
            margin-bottom: 8px;
        }

        .card-title a { color: var(--text-main); text-decoration: none; }
        .card-title a:hover { color: var(--accent-primary); text-decoration: underline; }

        .card-price { font-size: 22px; font-weight: 800; color: #38bdf8; margin-bottom: 12px; }

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

        .card-actions { display: flex; gap: 8px; }

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

        .btn:active { transform: scale(0.98); }
        .btn-primary { background-color: var(--accent-primary); color: #ffffff; }
        .btn-primary:hover { background-color: var(--accent-primary-hover); }
        .btn-success { background-color: #059669; color: #ffffff; }
        .btn-success:hover { background-color: #10b981; }
        .btn-secondary { background-color: #374151; color: #f3f4f6; border-color: #4b5563; }
        .btn-secondary:hover { background-color: #4b5563; }
        .btn-danger { background-color: #dc2626; color: #ffffff; }
        .btn-danger:hover { background-color: #ef4444; }
        .btn-sm { padding: 6px 12px; font-size: 12px; }

        /* SETTINGS FORM & MODAL */
        .settings-card {
            background-color: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 24px;
        }

        .form-group { margin-bottom: 14px; }
        .form-group:last-child { margin-bottom: 0; }

        label {
            display: block;
            font-size: 13px;
            font-weight: 600;
            color: #d1d5db;
            margin-bottom: 6px;
        }

        .help-text { font-size: 12px; color: var(--text-muted); margin-top: 4px; }

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

        textarea { resize: vertical; min-height: 90px; }

        .row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        .row-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }

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

        /* MODAL DIALOG */
        .modal-overlay {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.75);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            padding: 16px;
        }

        .modal {
            background-color: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 14px;
            max-width: 580px;
            width: 100%;
            padding: 24px;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);
            animation: fadeIn 0.15s ease-out;
        }

        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 18px;
        }

        .modal-header h3 { font-size: 18px; font-weight: 700; }
        .modal-close {
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: 24px;
            cursor: pointer;
            padding: 0 4px;
        }
        .modal-close:hover { color: var(--text-main); }

        /* TABS */
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

        .tab-btn:hover { color: var(--text-main); }
        .tab-btn.active { color: var(--accent-primary); border-bottom-color: var(--accent-primary); }

        .tab-pane { display: none; }
        .tab-pane.active { display: block; }

        /* TERMINAL LOGS */
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

        /* TABLE */
        .table-container {
            background-color: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            overflow-x: auto;
        }

        table { width: 100%; border-collapse: collapse; font-size: 13px; text-align: left; }
        th { background-color: #1f2937; padding: 12px 16px; font-weight: 600; color: #d1d5db; border-bottom: 1px solid var(--border-color); }
        td { padding: 12px 16px; border-bottom: 1px solid var(--border-color); }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background-color: var(--bg-card-hover); }

        #toast {
            position: fixed;
            bottom: 24px; right: 24px;
            background-color: #1f2937;
            color: #ffffff;
            border: 1px solid #374151;
            padding: 12px 20px;
            border-radius: 8px;
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.5);
            font-size: 14px;
            font-weight: 500;
            display: none;
            z-index: 2000;
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
                    <p>Sledování více aktivních hledání naraz s AI hodnocením</p>
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
                <strong>Tailscale:</strong>
                <span class="ip-chip" id="tailscaleIpChip">http://...</span>
                <span style="margin: 0 8px; color: #4b5563;">|</span>
                <strong>LAN:</strong>
                <span class="ip-chip" id="lanIpChip">http://...</span>
            </div>
            <div>
                <span id="nextRunInfo" style="color: var(--text-muted);">Příští kontrola: ...</span>
            </div>
        </div>

        <!-- SECTION: MOJE AKTIVNÍ HLEDÁNÍ (MULTI-SEARCH) -->
        <div class="section-title">
            <h2>🎯 Sledovaná hledání (Aktivní kategorie)</h2>
            <div style="display: flex; gap: 8px;">
                <button class="btn btn-primary btn-sm" onclick="openAddTargetModal()">
                    ➕ Přidat nové hledání
                </button>
            </div>
        </div>

        <div class="targets-grid" id="targetsContainer">
            <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-muted); background: var(--bg-card); border-radius: 12px; border: 1px solid var(--border-color);">
                Načítám sledovaná hledání...
            </div>
        </div>

        <!-- HERO SECTION: TOP 3 ZA POSLEDNÍ MĚSÍC -->
        <div class="section-title">
            <h2>🏆 TOP 3 nejlepší nabídky za poslední měsíc</h2>
            <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
                <select id="topDealsTargetFilter" onchange="loadTopDeals()" style="max-width: 220px; padding: 6px 10px; font-size: 13px;">
                    <option value="">Všechny kategorie</option>
                </select>
                <button class="btn btn-secondary btn-sm" onclick="loadTopDeals()">🔄 Obnovit</button>
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

        <!-- TABS: NASTAVENÍ / HISTORIE / LOGY -->
        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('tab-deals', this)">
                📋 Všechny nalezené inzeráty
            </button>
            <button class="tab-btn" onclick="switchTab('tab-settings', this)">
                ⚙️ Globální plánovač & systém
            </button>
            <button class="tab-btn" onclick="switchTab('tab-logs', this)">
                📟 Živá systémová konzole (Logy)
            </button>
        </div>

        <!-- TAB 1: SEZNAM INZERÁTŮ -->
        <div id="tab-deals" class="tab-pane active">
            <div style="display: flex; gap: 12px; margin-bottom: 14px; flex-wrap: wrap;">
                <input type="text" id="dealSearchInput" placeholder="Hledat v názvu inzerátu..." style="max-width: 280px;" oninput="filterAllDeals()">
                <select id="dealTargetFilterSelect" style="max-width: 220px;" onchange="filterAllDeals()">
                    <option value="">Všechna hledání</option>
                </select>
                <select id="scoreFilterSelect" style="max-width: 180px;" onchange="filterAllDeals()">
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
                            <th>Kategorie</th>
                            <th>Název inzerátu</th>
                            <th>Cena</th>
                            <th>AI Skóre</th>
                            <th>Nalezeno</th>
                            <th>Telegram</th>
                            <th>Akce</th>
                        </tr>
                    </thead>
                    <tbody id="allDealsTableBody">
                        <tr><td colspan="8" style="text-align: center; color: var(--text-muted);">Načítám inzeráty...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- TAB 2: GLOBÁLNÍ NASTAVENÍ -->
        <div id="tab-settings" class="tab-pane">
            <div class="settings-card">
                <form id="globalConfigForm" onsubmit="saveGlobalConfig(event)">
                    <h3 style="font-size: 16px; margin-bottom: 16px; border-bottom: 1px solid var(--border-color); padding-bottom: 8px;">
                        ⏰ Nastavení plánovače kontrol
                    </h3>
                    <div class="row-3" style="margin-bottom: 16px;">
                        <div class="form-group">
                            <label for="check_interval_minutes">Interval kontrol (minuty):</label>
                            <input type="number" id="check_interval_minutes" name="check_interval_minutes" min="1" max="1440" required>
                            <div class="help-text">Jak často se spustí vyhledávací cyklus (např. 30 min).</div>
                        </div>
                        <div class="form-group">
                            <label for="max_pages_to_scrape">Počet stránek na dotaz:</label>
                            <select id="max_pages_to_scrape" name="max_pages_to_scrape">
                                <option value="1">1 stránka (20 nejnovějších)</option>
                                <option value="2">2 stránky (40 inzerátů)</option>
                                <option value="3">3 stránky (60 inzerátů)</option>
                            </select>
                            <div class="help-text">Doporučujeme 1–2 stránky.</div>
                        </div>
                        <div class="form-group">
                            <label for="score_threshold">Výchozí práh pro notifikace:</label>
                            <select id="score_threshold" name="score_threshold">
                                <option value="7">7 / 10</option>
                                <option value="8">8 / 10 (Doporučeno)</option>
                                <option value="9">9 / 10</option>
                            </select>
                            <div class="help-text">Pokud cíl nemá vlastní práh.</div>
                        </div>
                    </div>

                    <div class="form-group">
                        <label class="checkbox-label">
                            <input type="checkbox" id="is_24_7" name="is_24_7" onchange="toggleHoursVisibility()">
                            <strong>Běžet nepřetržitě 24/7</strong>
                        </label>
                    </div>

                    <div class="row-2" id="hoursWindowRow" style="margin-bottom: 16px;">
                        <div class="form-group">
                            <label for="active_start_hour">Aktivní od (hodina 0–23):</label>
                            <input type="number" id="active_start_hour" name="active_start_hour" min="0" max="23" value="8">
                        </div>
                        <div class="form-group">
                            <label for="active_end_hour">Aktivní do (hodina 0–23):</label>
                            <input type="number" id="active_end_hour" name="active_end_hour" min="0" max="23" value="20">
                        </div>
                    </div>

                    <div class="form-group" style="margin-bottom: 20px;">
                        <label class="checkbox-label">
                            <input type="checkbox" id="run_on_startup" name="run_on_startup">
                            <span>Vynutit okamžitý běh po startu aplikace</span>
                        </label>
                    </div>

                    <div style="display: flex; gap: 12px; align-items: center;">
                        <button type="submit" class="btn btn-primary" id="btnSaveConfig">
                            💾 Uložit globální nastavení
                        </button>
                        <button type="button" class="btn btn-secondary" onclick="testTelegram()">
                            🔔 Otestovat Telegram spojení
                        </button>
                    </div>
                </form>
            </div>
        </div>

        <!-- TAB 3: TERMINÁL LOGŮ -->
        <div id="tab-logs" class="tab-pane">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                <span style="font-size: 13px; color: var(--text-muted);">Posledních 60 událostí ze systémového logu:</span>
                <button class="btn btn-secondary btn-sm" onclick="loadLogs()">🔄 Obnovit logy</button>
            </div>
            <div class="terminal-window" id="terminalLog">
                Načítám logy...
            </div>
        </div>
    </div>

    <!-- MODAL: PŘIDAT / UPRAVIT HLEDÁNÍ -->
    <div class="modal-overlay" id="targetModal">
        <div class="modal">
            <div class="modal-header">
                <h3 id="targetModalTitle">➕ Nové sledované hledání</h3>
                <button class="modal-close" onclick="closeTargetModal()">&times;</button>
            </div>
            <form id="targetForm" onsubmit="saveTargetForm(event)">
                <input type="hidden" id="modal_target_id" value="">
                
                <div class="form-group">
                    <label for="modal_target_name">Název hledání / kategorie:</label>
                    <input type="text" id="modal_target_name" required placeholder="např. Herní notebook RTX 3060 nebo 3D tiskárna">
                    <div class="help-text">Slouží pro přehlednost v dashboardu a štítek v Telegram zprávách.</div>
                </div>

                <div class="form-group">
                    <label for="modal_target_query">Zadání přirozeným jazykem (AI Prompt):</label>
                    <textarea id="modal_target_query" required placeholder="Popište co nejpřesněji, co chcete koupit, jaké modely, značky, výbavu a jaká rizika/vady vyřadit..."></textarea>
                    <div class="help-text">Groq LLM automaticky odvodí vyhledávací fráze a vylučovací slova.</div>
                </div>

                <div class="row-2">
                    <div class="form-group">
                        <label for="modal_target_min_price">Minimální cena (Kč):</label>
                        <input type="number" id="modal_target_min_price" placeholder="např. 500">
                    </div>
                    <div class="form-group">
                        <label for="modal_target_max_price">Maximální cena (Kč):</label>
                        <input type="number" id="modal_target_max_price" placeholder="např. 4500">
                    </div>
                </div>

                <div class="form-group">
                    <label for="modal_target_threshold">Práh AI skóre pro upozornění:</label>
                    <select id="modal_target_threshold">
                        <option value="">Výchozí (8 / 10)</option>
                        <option value="7">7 / 10 (Dobré nabídky)</option>
                        <option value="8">8 / 10 (Skvělé nabídky – doporučeno)</option>
                        <option value="9">9 / 10 (Výjimečné nabídky)</option>
                    </select>
                </div>

                <div class="form-group" style="margin-bottom: 20px;">
                    <label class="checkbox-label">
                        <input type="checkbox" id="modal_target_active" checked>
                        <strong>Aktivní (sledovat toto hledání při každém cyklu)</strong>
                    </label>
                </div>

                <div style="display: flex; justify-content: flex-end; gap: 10px;">
                    <button type="button" class="btn btn-secondary" onclick="closeTargetModal()">Zrušit</button>
                    <button type="submit" class="btn btn-primary" id="btnSaveTargetModal">Uložit hledání</button>
                </div>
            </form>
        </div>
    </div>

    <!-- TOAST NOTIFICATION -->
    <div id="toast"></div>

    <script>
        let cachedTargets = [];
        let allDealsCache = [];

        document.addEventListener('DOMContentLoaded', () => {
            loadStatus();
            loadTargets();
            loadConfig();
            loadTopDeals();
            loadAllDeals();
            loadLogs();

            setInterval(() => loadStatus(), 10000);
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
            setTimeout(() => { toast.style.display = 'none'; }, 4000);
        }

        function switchTab(tabId, btn) {
            document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            document.getElementById(tabId).classList.add('active');
            btn.classList.add('active');
            if (tabId === 'tab-logs') loadLogs();
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

                const badge = document.getElementById('statusBadge');
                const statusText = document.getElementById('statusText');

                if (data.is_scraping) {
                    badge.className = 'badge badge-scraping';
                    statusText.textContent = 'Právě scrapuje a vyhodnocuje...';
                } else if (!data.is_active_window) {
                    badge.className = 'badge badge-sleeping';
                    statusText.textContent = 'Mimo aktivní okno (šetří tokeny)';
                } else {
                    badge.className = 'badge badge-online';
                    statusText.textContent = `Online (${data.active_targets_count || 0} aktivních hledání)`;
                }

                const port = data.web_port || 5050;
                const tailscaleIp = data.server_ips.tailscale ? `http://${data.server_ips.tailscale}:${port}` : 'Tailscale nenalezeno';
                const lanIp = `http://${data.server_ips.lan}:${port}`;

                document.getElementById('tailscaleIpChip').textContent = tailscaleIp;
                document.getElementById('lanIpChip').textContent = lanIp;

                document.getElementById('nextRunInfo').textContent = data.next_run
                    ? `Příští kontrola: ${data.next_run}`
                    : `Interval: každých ${data.check_interval_minutes} min`;
            } catch (err) {
                console.error('Chyba stavu:', err);
            }
        }

        // SPRÁVA VYHLEDÁVACÍCH CÍLŮ
        async function loadTargets() {
            const container = document.getElementById('targetsContainer');
            try {
                const res = await fetch('/api/targets');
                const data = await res.json();
                cachedTargets = data.targets || [];

                updateTargetFilterDropdowns();

                if (cachedTargets.length === 0) {
                    container.innerHTML = `
                        <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-muted); background: var(--bg-card); border-radius: 12px; border: 1px solid var(--border-color);">
                            Zatím nemáte vytvořené žádné hledání.<br>
                            Klikněte na <strong>"➕ Přidat nové hledání"</strong> a vytvořte si první kategorii (např. 3D tiskárny, notebooky atd.).
                        </div>
                    `;
                    return;
                }

                container.innerHTML = cachedTargets.map(t => {
                    const priceRange = (t.min_price || t.max_price)
                        ? `${t.min_price ? t.min_price.toLocaleString('cs-CZ') + ' Kč' : '0 Kč'} – ${t.max_price ? t.max_price.toLocaleString('cs-CZ') + ' Kč' : 'bez limitu'}`
                        : 'Cena bez omezení';

                    const scoreText = t.score_threshold ? `Práh: ${t.score_threshold}/10` : 'Práh: výchozí';

                    return `
                        <div class="target-card ${t.is_active ? '' : 'paused'}">
                            <div>
                                <div class="target-header">
                                    <span class="target-title">${escapeHtml(t.name)}</span>
                                    <label class="toggle-switch">
                                        <input type="checkbox" class="switch-input" ${t.is_active ? 'checked' : ''} onchange="toggleTargetActive('${t.id}')">
                                        <span class="switch-slider"></span>
                                        <span style="font-size: 11px; color: ${t.is_active ? '#34d399' : '#9ca3af'};">${t.is_active ? 'Aktivní' : 'Pozastaveno'}</span>
                                    </label>
                                </div>
                                <div class="target-meta">
                                    <span class="meta-pill">💰 ${priceRange}</span>
                                    <span class="meta-pill">⭐️ ${scoreText}</span>
                                </div>
                                <div class="target-query-box" title="${escapeHtml(t.query)}">
                                    ${escapeHtml(t.query)}
                                </div>
                            </div>
                            <div class="target-footer">
                                <span style="font-size: 11px; color: var(--text-muted);">
                                    ${t.last_scraped_at ? 'Zkontrolováno: ' + t.last_scraped_at.substring(11, 16) : 'Čeká na první cyklus'}
                                </span>
                                <div style="display: flex; gap: 6px;">
                                    <button class="btn btn-secondary btn-sm" onclick="openEditTargetModal('${t.id}')">✏️ Upravit</button>
                                    <button class="btn btn-danger btn-sm" onclick="deleteTarget('${t.id}')">🗑️</button>
                                </div>
                            </div>
                        </div>
                    `;
                }).join('');
            } catch (err) {
                container.innerHTML = `<div style="grid-column: 1/-1; color: var(--accent-danger);">Chyba při načítání hledání: ${err.message}</div>`;
            }
        }

        function updateTargetFilterDropdowns() {
            const topFilter = document.getElementById('topDealsTargetFilter');
            const dealsFilter = document.getElementById('dealTargetFilterSelect');

            const options = '<option value="">Všechny kategorie</option>' + cachedTargets.map(t =>
                `<option value="${t.id}">${escapeHtml(t.name)}</option>`
            ).join('');

            const prevTopVal = topFilter.value;
            const prevDealsVal = dealsFilter.value;

            topFilter.innerHTML = options;
            dealsFilter.innerHTML = options;

            if (prevTopVal) topFilter.value = prevTopVal;
            if (prevDealsVal) dealsFilter.value = prevDealsVal;
        }

        async function toggleTargetActive(targetId) {
            try {
                const res = await fetch(`/api/targets/${targetId}/toggle`, { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    showToast(`Hledání bylo ${data.target.is_active ? 'aktivováno' : 'pozastaveno'}.`);
                    loadTargets();
                    loadStatus();
                }
            } catch (err) {
                showToast(`Chyba: ${err.message}`, true);
            }
        }

        async function deleteTarget(targetId) {
            if (!confirm('Opravdu chcete smazat toto vyhledávání?')) return;
            try {
                const res = await fetch(`/api/targets/${targetId}`, { method: 'DELETE' });
                const data = await res.json();
                if (data.success) {
                    showToast('Hledání bylo smazáno.');
                    loadTargets();
                    loadStatus();
                }
            } catch (err) {
                showToast(`Chyba: ${err.message}`, true);
            }
        }

        function openAddTargetModal() {
            document.getElementById('targetModalTitle').textContent = '➕ Nové sledované hledání';
            document.getElementById('modal_target_id').value = '';
            document.getElementById('modal_target_name').value = '';
            document.getElementById('modal_target_query').value = '';
            document.getElementById('modal_target_min_price').value = '';
            document.getElementById('modal_target_max_price').value = '';
            document.getElementById('modal_target_threshold').value = '';
            document.getElementById('modal_target_active').checked = true;
            document.getElementById('targetModal').style.display = 'flex';
        }

        function openEditTargetModal(targetId) {
            const t = cachedTargets.find(x => x.id === targetId);
            if (!t) return;
            document.getElementById('targetModalTitle').textContent = `✏️ Upravit: ${t.name}`;
            document.getElementById('modal_target_id').value = t.id;
            document.getElementById('modal_target_name').value = t.name || '';
            document.getElementById('modal_target_query').value = t.query || '';
            document.getElementById('modal_target_min_price').value = t.min_price || '';
            document.getElementById('modal_target_max_price').value = t.max_price || '';
            document.getElementById('modal_target_threshold').value = t.score_threshold || '';
            document.getElementById('modal_target_active').checked = !!t.is_active;
            document.getElementById('targetModal').style.display = 'flex';
        }

        function closeTargetModal() {
            document.getElementById('targetModal').style.display = 'none';
        }

        async function saveTargetForm(e) {
            e.preventDefault();
            const targetId = document.getElementById('modal_target_id').value;
            const payload = {
                name: document.getElementById('modal_target_name').value,
                query: document.getElementById('modal_target_query').value,
                min_price: document.getElementById('modal_target_min_price').value,
                max_price: document.getElementById('modal_target_max_price').value,
                score_threshold: document.getElementById('modal_target_threshold').value,
                is_active: document.getElementById('modal_target_active').checked,
            };

            const btn = document.getElementById('btnSaveTargetModal');
            btn.disabled = true;
            btn.textContent = 'Ukládám...';

            try {
                const url = targetId ? `/api/targets/${targetId}` : '/api/targets';
                const method = targetId ? 'PUT' : 'POST';

                const res = await fetch(url, {
                    method: method,
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (data.success) {
                    showToast('✅ Vyhledávací profil byl úspěšně uložen!');
                    closeTargetModal();
                    loadTargets();
                    loadStatus();
                } else {
                    showToast(`❌ Chyba: ${data.error}`, true);
                }
            } catch (err) {
                showToast(`❌ Chyba: ${err.message}`, true);
            } finally {
                btn.disabled = false;
                btn.textContent = 'Uložit hledání';
            }
        }

        // TOP 3 NABÍDKY
        async function loadTopDeals() {
            const container = document.getElementById('topDealsContainer');
            const targetId = document.getElementById('topDealsTargetFilter').value;
            try {
                let url = '/api/top_deals?limit=3&days=30';
                if (targetId) url += `&target_id=${encodeURIComponent(targetId)}`;

                const res = await fetch(url);
                const data = await res.json();

                if (!data.deals || data.deals.length === 0) {
                    container.innerHTML = `
                        <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-muted); background: var(--bg-card); border-radius: 12px; border: 1px solid var(--border-color);">
                            ℹ️ Pro vybranou kategorii zatím nejsou žádné ohodnocené inzeráty.<br>
                            Spusťte kontrolu tlačítkem <strong>"Spustit vyhledávání ihned"</strong>.
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
                    const targetTagHtml = deal.target_name ? `<div class="target-tag">🏷️ ${escapeHtml(deal.target_name)}</div>` : '';

                    return `
                        <div class="deal-card rank-${idx + 1}">
                            <div>
                                <div class="card-header">
                                    <span class="medal-badge ${medal.class}">${medal.label}</span>
                                    <span class="score-pill">⭐️ ${deal.score} / 10</span>
                                </div>
                                ${targetTagHtml}
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
                container.innerHTML = `<div style="grid-column: 1/-1; color: var(--accent-danger);">Chyba: ${err.message}</div>`;
            }
        }

        // GLOBÁLNÍ KONFIGURACE PLÁNOVAČE
        async function loadConfig() {
            try {
                const res = await fetch('/api/config');
                const cfg = await res.json();
                document.getElementById('check_interval_minutes').value = cfg.check_interval_minutes || 30;
                document.getElementById('score_threshold').value = cfg.score_threshold || 8;
                document.getElementById('max_pages_to_scrape').value = cfg.max_pages_to_scrape || 1;
                document.getElementById('active_start_hour').value = cfg.active_start_hour !== undefined ? cfg.active_start_hour : 8;
                document.getElementById('active_end_hour').value = cfg.active_end_hour !== undefined ? cfg.active_end_hour : 20;
                document.getElementById('is_24_7').checked = !!cfg.is_24_7;
                document.getElementById('run_on_startup').checked = !!cfg.run_on_startup;
                toggleHoursVisibility();
            } catch (err) {
                console.error(err);
            }
        }

        async function saveGlobalConfig(e) {
            e.preventDefault();
            const btn = document.getElementById('btnSaveConfig');
            btn.disabled = true;
            btn.textContent = 'Ukládám...';

            const payload = {
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
                    showToast('✅ Globální nastavení plánovače bylo uloženo!');
                    loadStatus();
                } else {
                    showToast(`❌ Chyba: ${data.error}`, true);
                }
            } catch (err) {
                showToast(`❌ Chyba: ${err.message}`, true);
            } finally {
                btn.disabled = false;
                btn.textContent = '💾 Uložit globální nastavení';
            }
        }

        // AKČNÍ FUNKCE
        async function triggerManualRun() {
            const btn = document.getElementById('btnRunNow');
            btn.disabled = true;
            btn.textContent = '⏳ Spouštím...';

            try {
                const res = await fetch('/api/run_now', { method: 'POST' });
                const data = await res.json();
                if (data.status === 'started') {
                    showToast('🚀 Vyhledávací cyklus přes všechna aktivní hledání spuštěn!');
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

        async function testTelegram() {
            showToast('📤 Testuji spojení s Telegramem...');
            try {
                const res = await fetch('/api/test_telegram', { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    showToast('✅ Testovací notifikace byla úspěšně doručena!');
                } else {
                    showToast(`❌ Chyba Telegramu: ${data.error}`, true);
                }
            } catch (err) {
                showToast(`❌ Chyba: ${err.message}`, true);
            }
        }

        async function sendTopDealsToTelegram() {
            const targetId = document.getElementById('topDealsTargetFilter').value;
            let url = '/api/send_top_telegram';
            if (targetId) url += `?target_id=${encodeURIComponent(targetId)}`;

            showToast('📤 Odesílám TOP 3 na Telegram...');
            try {
                const res = await fetch(url, { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    showToast('✅ TOP 3 nabídky byly úspěšně odeslány do Telegramu!');
                } else {
                    showToast(`❌ Chyba: ${data.error}`, true);
                }
            } catch (err) {
                showToast(`❌ Chyba: ${err.message}`, true);
            }
        }

        // SEZNAM VŠECH INZERÁTŮ
        async function loadAllDeals() {
            const tbody = document.getElementById('allDealsTableBody');
            tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-muted);">Načítám inzeráty...</td></tr>';
            try {
                const res = await fetch('/api/all_deals?limit=120');
                const data = await res.json();
                allDealsCache = data.deals || [];
                renderAllDealsTable(allDealsCache);
            } catch (err) {
                tbody.innerHTML = `<tr><td colspan="8" style="color: var(--accent-danger);">Chyba: ${err.message}</td></tr>`;
            }
        }

        function filterAllDeals() {
            const query = document.getElementById('dealSearchInput').value.toLowerCase();
            const minScore = parseInt(document.getElementById('scoreFilterSelect').value, 10);
            const targetId = document.getElementById('dealTargetFilterSelect').value;

            const filtered = allDealsCache.filter(deal => {
                const matchesQuery = !query || (deal.title && deal.title.toLowerCase().includes(query));
                const matchesScore = (deal.score || 0) >= minScore;
                const matchesTarget = !targetId || deal.target_id === targetId;
                return matchesQuery && matchesScore && matchesTarget;
            });
            renderAllDealsTable(filtered);
        }

        function renderAllDealsTable(deals) {
            const tbody = document.getElementById('allDealsTableBody');
            if (deals.length === 0) {
                tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-muted);">Nenalezeny žádné inzeráty odpovídající filtru.</td></tr>';
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
                const categoryBadge = deal.target_name ? `<span style="color: #a78bfa; font-weight: 600;">${escapeHtml(deal.target_name)}</span>` : '<span style="color: #6b7280;">-</span>';

                return `
                    <tr>
                        <td style="font-family: monospace; color: var(--text-muted); font-size: 11px;">${deal.id}</td>
                        <td>${categoryBadge}</td>
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

        // LOGY
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
    logging.basicConfig(level=logging.INFO)
    port = config.web_port or 5050
    logger.info(f"Spouštím standalone web server na http://0.0.0.0:{port}")
    run_web_server(host="0.0.0.0", port=port)
