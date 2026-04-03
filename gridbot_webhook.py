import os
import json
import math
import time
import base64
from threading import Lock

import requests
from flask import Flask, request, jsonify

# =========================
# SETTINGS
# =========================
USE_DEMO = True
DRY_RUN = True                  # set False when ready
USE_EXTENDED_HOURS = True
BUY_VALUE = 25.0
STATE_FILE = "gridbot_webhook_state.json"

API_KEY = os.getenv("API_KEY","")
API_SECRET = os.getenv("API_SECRET","")

# Shared secret to stop random requests hitting your webhook
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET,"")

BASE_URL = "https://demo.trading212.com/api/v0" if USE_DEMO else "https://live.trading212.com/api/v0"

app = Flask(__name__)
state_lock = Lock()

# =========================
# STATE
# =========================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}

    state.setdefault("last_action_bar", {})
    state.setdefault("buy_lots", {})
    return state

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

STATE = load_state()

# =========================
# HELPERS
# =========================
def get_headers():
    creds = f"{API_KEY}:{API_SECRET}".encode("utf-8")
    return {
        "Authorization": "Basic " + base64.b64encode(creds).decode("utf-8"),
        "Content-Type": "application/json",
    }

def get_instruments():
    r = requests.get(
        f"{BASE_URL}/equity/metadata/instruments",
        headers=get_headers(),
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    return {i["ticker"].split("_")[0]: i["ticker"] for i in data}

def get_positions():
    r = requests.get(
        f"{BASE_URL}/equity/positions",
        headers=get_headers(),
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()

    positions = {}
    for p in data:
        full_ticker = p.get("instrument", {}).get("ticker", "")
        simple = full_ticker.split("_")[0] if full_ticker else ""
        qty = float(p.get("quantity", 0) or 0)
        if simple:
            positions[simple] = {
                "quantity": qty,
                "full_ticker": full_ticker,
                "average_price": float(p.get("averagePricePaid", 0) or 0),
                "current_price": float(p.get("currentPrice", 0) or 0),
            }
    return positions

def shares_from_cash(cash_amount: float, price: float) -> float:
    if price <= 0:
        return 0.0

    qty = cash_amount / price

    # small floor to avoid silly quantities
    if qty < 0.01:
        return 0.0

    # safer precision for T212
    return round(qty, 4)

def place_market_order(ticker: str, quantity: float):
    payload = {
        "ticker": ticker,
        "quantity": quantity,  # positive buy, negative sell
        "extendedHours": USE_EXTENDED_HOURS,
    }

    if DRY_RUN:
        return {"dry_run": True, "payload": payload}

    r = requests.post(
        f"{BASE_URL}/equity/orders/market",
        headers=get_headers(),
        json=payload,
        timeout=20,
    )

    if not r.ok:
        print("ORDER ERROR BODY:", r.text)

    r.raise_for_status()
    return r.json()

def get_symbol_lots(state, symbol):
    return state["buy_lots"].setdefault(symbol, [])

def find_biggest_profit_lot(lots, sell_price: float):
    best_index = None
    best_lot = None
    best_profit = -1.0

    for idx, lot in enumerate(lots):
        buy_price = float(lot["price"])
        lot_qty = float(lot["qty"])

        if sell_price <= buy_price or lot_qty <= 0:
            continue

        cash_profit = (sell_price - buy_price) * lot_qty
        if cash_profit > best_profit:
            best_profit = cash_profit
            best_index = idx
            best_lot = lot

    return best_index, best_lot, best_profit

def normalize_symbol(raw_symbol: str) -> str:
    """
    Accepts values like:
    - AAPL
    - NASDAQ:AAPL
    - NYSE:MSFT
    and returns AAPL / MSFT
    """
    raw_symbol = (raw_symbol or "").strip().upper()
    if ":" in raw_symbol:
        return raw_symbol.split(":")[-1]
    return raw_symbol

def parse_json_payload():
    if request.is_json:
        return request.get_json(silent=True) or {}

    raw = request.get_data(as_text=True).strip()
    if not raw:
        return {}

    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}

# Cache instruments so we do not re-fetch every alert
INSTRUMENT_MAP = None
def get_instrument_map_cached():
    global INSTRUMENT_MAP
    if INSTRUMENT_MAP is None:
        INSTRUMENT_MAP = get_instruments()
    return INSTRUMENT_MAP

# =========================
# WEBHOOK
# =========================
@app.route("/webhook", methods=["POST"])
def webhook():
    global STATE

    payload = parse_json_payload()

    secret = str(payload.get("secret", ""))
    if secret != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "invalid secret"}), 401

    symbol = normalize_symbol(str(payload.get("symbol", "")))
    action = str(payload.get("action", "")).upper().strip()
    price_raw = payload.get("price")
    bar_time = str(payload.get("bar_time", "")).strip()

    if not symbol:
        return jsonify({"ok": False, "error": "missing symbol"}), 400
    if action not in {"BUY", "SELL"}:
        return jsonify({"ok": False, "error": "action must be BUY or SELL"}), 400
    if price_raw in (None, ""):
        return jsonify({"ok": False, "error": "missing price"}), 400
    if not bar_time:
        return jsonify({"ok": False, "error": "missing bar_time"}), 400

    try:
        close_price = float(price_raw)
    except ValueError:
        return jsonify({"ok": False, "error": "invalid price"}), 400

    with state_lock:
        instrument_map = get_instrument_map_cached()
        ticker = instrument_map.get(symbol)

        if not ticker:
            return jsonify({"ok": False, "error": f"symbol {symbol} not found in Trading 212 instruments"}), 400

        last_action_bar = STATE["last_action_bar"]
        lots = get_symbol_lots(STATE, symbol)

        # stop duplicate action on the same bar
        if last_action_bar.get(symbol) == bar_time:
            return jsonify({
                "ok": True,
                "skipped": True,
                "reason": f"already acted on bar {bar_time}",
                "symbol": symbol,
                "action": action
            })

        try:
            if action == "BUY":
                qty = shares_from_cash(BUY_VALUE, close_price)
                if qty <= 0:
                    return jsonify({"ok": False, "error": "calculated buy quantity <= 0"}), 400

                approx_spend = round(qty * close_price, 2)
                print(f"DEBUG BUY -> {symbol} | price={close_price} | qty={qty}")

                response = place_market_order(ticker, qty)

                lots.append({
                    "price": close_price,
                    "value": BUY_VALUE,
                    "qty": qty,
                    "bar_time": bar_time,
                })

                last_action_bar[symbol] = bar_time
                save_state(STATE)

                return jsonify({
                    "ok": True,
                    "symbol": symbol,
                    "action": "BUY",
                    "qty": qty,
                    "approx_spend": approx_spend,
                    "response": response
                })

            if action == "SELL":
                positions = get_positions()
                held_qty = float(positions.get(symbol, {}).get("quantity", 0.0))
                if held_qty <= 0:
                    return jsonify({"ok": True, "skipped": True, "reason": "no position held", "symbol": symbol})

                if not lots:
                    return jsonify({"ok": True, "skipped": True, "reason": "no tracked buy lots", "symbol": symbol})

                lot_index, lot, best_profit = find_biggest_profit_lot(lots, close_price)
                if lot is None:
                    return jsonify({"ok": True, "skipped": True, "reason": "no profitable tracked lot", "symbol": symbol})

                buy_price = float(lot["price"])
                buy_value = float(lot["value"])
                lot_qty = float(lot["qty"])

                target_sell_value = buy_value * (close_price / buy_price)
                sell_qty = shares_from_cash(target_sell_value, close_price)
                sell_qty = min(sell_qty, held_qty, lot_qty)

                if sell_qty <= 0:
                    return jsonify({"ok": True, "skipped": True, "reason": "sell quantity <= 0", "symbol": symbol})

                approx_value = round(sell_qty * close_price, 2)
                approx_profit_taken = round((close_price - buy_price) * sell_qty, 2)

                print(f"DEBUG SELL -> {symbol} | price={close_price} | qty={sell_qty}")

                response = place_market_order(ticker, -sell_qty)

                remaining_qty = round(lot_qty - sell_qty, 4)
                if remaining_qty <= 0:
                    lots.pop(lot_index)
                else:
                    remaining_value = round(remaining_qty * buy_price, 2)
                    lots[lot_index] = {
                        "price": buy_price,
                        "value": remaining_value,
                        "qty": remaining_qty,
                        "bar_time": lot["bar_time"],
                    }

                last_action_bar[symbol] = bar_time
                save_state(STATE)

                return jsonify({
                    "ok": True,
                    "symbol": symbol,
                    "action": "SELL",
                    "qty": sell_qty,
                    "approx_value": approx_value,
                    "approx_profit_taken": approx_profit_taken,
                    "response": response
                })

        except requests.HTTPError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "mode": "DEMO" if USE_DEMO else "LIVE", "dry_run": DRY_RUN})

if __name__ == "__main__":
    # TradingView requires a reachable HTTP(S) endpoint; for local use, run this and expose it via ngrok or similar.
    app.run(host="0.0.0.0", port=5000, debug=False)

