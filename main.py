import os
import sqlite3
import requests
from datetime import datetime
from flask import Flask, request

import yfinance as yf
import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange

# --- Configuración ---
TOKEN = os.environ.get("BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
DB_PATH = "po_bot.db"

app = Flask(__name__)

# --- DB ---
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        chat_id INTEGER PRIMARY KEY,
        balance REAL DEFAULT 0,
        risk_pct REAL DEFAULT 1.0
    )""")
    con.commit()
    con.close()

def db():
    return sqlite3.connect(DB_PATH)

def get_user(chat_id):
    con = db(); cur = con.cursor()
    cur.execute("SELECT chat_id,balance,risk_pct FROM users WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO users(chat_id,balance,risk_pct) VALUES(?,?,?)",
                    (chat_id, 0.0, 1.0))
        con.commit()
        cur.execute("SELECT chat_id,balance,risk_pct FROM users WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
    con.close()
    return {"chat_id": row[0], "balance": row[1], "risk_pct": row[2]}

def set_config(chat_id, balance, risk_pct):
    con = db(); cur = con.cursor()
    cur.execute("UPDATE users SET balance=?,risk_pct=? WHERE chat_id=?",
                (balance, risk_pct, chat_id))
    con.commit(); con.close()

# --- Telegram helpers ---
def tg(method, payload):
    r = requests.post(f"{BASE_URL}/{method}", json=payload, timeout=15)
    return r.json() if r.ok else {}

def send_message(chat_id, text):
    return tg("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})

# --- Utilidades ---
def yahoo_symbol(pair: str) -> str:
    pair = pair.upper().replace(" ", "")
    # Acepta EURUSD o EURUSD=X
    return pair if pair.endswith("=X") else f"{pair}=X"

def get_ema(series: pd.Series, window: int) -> pd.Series:
    return EMAIndicator(close=series, window=window).ema_indicator()

def get_rsi(series: pd.Series, window: int = 14) -> pd.Series:
    return RSIIndicator(close=series, window=window).rsi()

def get_atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    return AverageTrueRange(high=high, low=low, close=close, window=window).average_true_range()

def is_engulfing_bull(prev_o, prev_c, last_o, last_c) -> bool:
    # Criterio simple: vela verde que envuelve cuerpo previo
    return (last_c > last_o) and (prev_c < prev_o) and (last_c >= max(prev_o, prev_c)) and (last_o <= min(prev_o, prev_c))

def is_engulfing_bear(prev_o, prev_c, last_o, last_c) -> bool:
    # Criterio simple: vela roja que envuelve cuerpo previo
    return (last_c < last_o) and (prev_c > prev_o) and (last_c <= min(prev_o, prev_c)) and (last_o >= max(prev_o, prev_c))

# --- Estrategia de señal (solo bajo demanda) ---
def analyze_pair(symbol: str):
    try:
        # Tendencia en M15 con EMA 50/200
        m15 = yf.download(symbol, interval="15m", period="5d", progress=False)
        if m15 is None or m15.empty or len(m15) < 220:
            return None, "Datos insuficientes en M15"

        ema50 = get_ema(m15['Close'], 50)
        ema200 = get_ema(m15['Close'], 200)
        trend = "up" if ema50.iloc[-1] > ema200.iloc[-1] else "down"

        # Setup en M1
        m1 = yf.download(symbol, interval="1m", period="1d", progress=False)
        if m1 is None or m1.empty or len(m1) < 30:
            return None, "Datos insuficientes en M1"

        rsi = get_rsi(m1['Close'], 14)
        atr = get_atr(m1['High'], m1['Low'], m1['Close'], 14)

        last_rsi = float(rsi.iloc[-1])
        last_atr = float(atr.iloc[-1])
        avg_atr = float(atr.iloc[-20:].mean())

        last = m1.iloc[-1]
        prev = m1.iloc[-2]

        engulf_bull = is_engulfing_bull(prev['Open'], prev['Close'], last['Open'], last['Close'])
        engulf_bear = is_engulfing_bear(prev['Open'], prev['Close'], last['Open'], last['Close'])

        # Reglas
        signal = None
        notes = []

        # Filtro volatilidad
        if last_atr <= avg_atr:
            notes.append("ATR bajo (poca volatilidad)")
            return None, "; ".join(notes)

        if trend == "up" and last_rsi < 30 and engulf_bull:
            signal = "CALL"
            notes.append("RSI < 30 + Tendencia alcista + Engulfing alcista")
        elif trend == "down" and last_rsi > 70 and engulf_bear:
            signal = "PUT"
            notes.append("RSI > 70 + Tendencia bajista + Engulfing bajista")
        else:
            notes.append("No cumple condiciones (RSI/tendencia/patrón)")

        return signal, "; ".join(notes)
    except Exception as e:
        return None, f"Error analizando {symbol}: {e}"

# --- Rutas Flask ---
@app.route("/")
def home():
    return "Bot está vivo ✅"

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True)
    if not update:
        return "ok", 200

    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = (update["message"].get("text") or "").strip()

        if text.startswith("/start"):
            send_message(chat_id, "👋 Bienvenido! Usa /config <balance> <riesgo%> y /signal <par>\nEj: <code>/config 200 1.5</code>  |  <code>/signal EURUSD</code>")
            return "ok", 200

        if text.startswith("/config"):
            parts = text.split()
            if len(parts) >= 3:
                try:
                    bal = float(parts[1]); rp = float(parts[2])
                    set_config(chat_id, bal, rp)
                    send_message(chat_id, f"✅ Configurado. Balance: <b>{bal:.2f}</b> | Riesgo: <b>{rp:.2f}%</b>")
                except:
                    send_message(chat_id, "Formato inválido. Ej: <code>/config 200 1.5</code>")
            else:
                send_message(chat_id, "Formato inválido. Ej: <code>/config 200 1.5</code>")
            return "ok", 200

        if text.startswith("/signal"):
            parts = text.split()
            if len(parts) >= 2:
                pair = parts[1].upper()
                yf_symbol = yahoo_symbol(pair)
                user = get_user(chat_id)
                send_message(chat_id, f"⏳ Analizando {pair}...")
                sig, note = analyze_pair(yf_symbol)
                if sig:
                    stake = round(user["balance"] * (user["risk_pct"] / 100.0), 2)
                    icon = "🟢" if sig == "CALL" else "🔴"
                    send_message(chat_id,
                                 f"📊 <b>Señal detectada</b>\n"
                                 f"{icon} {sig} {pair} (exp 5m)\n"
                                 f"Stake: <b>{stake:.2f}</b>\n"
                                 f"Condiciones: {note}")
                else:
                    send_message(chat_id, f"❌ No hay señal clara en {pair}\n{note}")
            else:
                send_message(chat_id, "Formato: <code>/signal EURUSD</code>")
            return "ok", 200

    return "ok", 200

@app.route("/set_webhook")
def set_webhook():
    public_url = os.environ.get("PUBLIC_URL")
    if not public_url:
        return "Falta PUBLIC_URL", 400
    url = f"{public_url}/webhook"
    r = requests.get(f"{BASE_URL}/setWebhook", params={"url": url})
    return r.text, r.status_code

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))