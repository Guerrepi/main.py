import os
import sqlite3
import requests
from datetime import datetime, timedelta
from flask import Flask, request

import yfinance as yf
import pandas as pd
import talib

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

# --- Estrategia de señal ---
def analyze_pair(symbol):
    try:
        # M15 para tendencia
        data_m15 = yf.download(symbol, interval="15m", period="5d")
        ema50 = talib.EMA(data_m15['Close'], timeperiod=50)
        ema200 = talib.EMA(data_m15['Close'], timeperiod=200)
        trend = "up" if ema50.iloc[-1] > ema200.iloc[-1] else "down"

        # M1 para setup
        data_m1 = yf.download(symbol, interval="1m", period="1d")
        rsi = talib.RSI(data_m1['Close'], timeperiod=14)
        atr = talib.ATR(data_m1['High'], data_m1['Low'], data_m1['Close'], timeperiod=14)

        last_rsi = rsi.iloc[-1]
        last_atr = atr.iloc[-1]
        avg_atr = atr.iloc[-20:].mean()

        last_candle = data_m1.iloc[-1]
        prev_candle = data_m1.iloc[-2]

        # Patrón simple: engulfing alcista/bajista
        engulfing_bull = last_candle['Close'] > prev_candle['Open'] and last_candle['Open'] < prev_candle['Close']
        engulfing_bear = last_candle['Close'] < prev_candle['Open'] and last_candle['Open'] > prev_candle['Close']

        signal = None
        note = []

        if trend == "up" and last_rsi < 30 and last_atr > avg_atr and engulfing_bull:
            signal = "CALL"
            note.append("RSI < 30, tendencia alcista, vela engulfing alcista")
        elif trend == "down" and last_rsi > 70 and last_atr > avg_atr and engulfing_bear:
            signal = "PUT"
            note.append("RSI > 70, tendencia bajista, vela engulfing bajista")

        return signal, "; ".join(note) if note else "No cumple condiciones"
    except Exception as e:
        return None, f"Error analizando {symbol}: {e}"

# --- Flask routes ---
@app.route("/")
def home():
    return "Bot está vivo ✅"

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True)
    if not update: return "no update",200
    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text","").strip()
        user = get_user(chat_id)

        if text.startswith("/start"):
            send_message(chat_id,"👋 Bienvenido! Usa /config <balance> <riesgo%> para iniciar.\nEjemplo: <code>/config 200 1.5</code>")
        elif text.startswith("/config"):
            parts = text.split()
            if len(parts)>=3:
                try:
                    bal=float(parts[1]); rp=float(parts[2])
                    set_config(chat_id,bal,rp)
                    send_message(chat_id,f"✅ Configurado. Balance: <b>{bal:.2f}</b> | Riesgo: <b>{rp:.2f}%</b>")
                except:
                    send_message(chat_id,"Formato inválido. Ej: /config 200 1.5")
            else:
                send_message(chat_id,"Formato inválido. Ej: /config 200 1.5")
        elif text.startswith("/signal"):
            parts = text.split()
            if len(parts)>=2:
                pair = parts[1].upper()
                # Yahoo Finance necesita formato con =X para forex
                yf_symbol = f"{pair}=X" if not pair.endswith("=X") else pair
                send_message(chat_id,f"⏳ Analizando {pair}...")
                sig, note = analyze_pair(yf_symbol)
                if sig:
                    stake = round(user["balance"]*(user["risk_pct"]/100),2)
                    send_message(chat_id,
                        f"📊 Señal detectada\n"
                        f"{'🟢' if sig=='CALL' else '🔴'} {sig} {pair} (exp 5m)\n"
                        f"Stake: <b>{stake:.2f}</b>\n"
                        f"Condiciones: {note}"
                    )
                else:
                    send_message(chat_id,f"❌ No hay señal clara en {pair}\n{note}")
            else:
                send_message(chat_id,"Formato: /signal EURUSD")
    return "ok",200

@app.route("/set_webhook")
def set_webhook():
    public_url=os.environ.get("PUBLIC_URL")
    if not public_url: return "Falta PUBLIC_URL",400
    url=f"{public_url}/webhook"
    r=requests.get(f"{BASE_URL}/setWebhook",params={"url":url})
    return r.text, r.status_code

if __name__=="__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",8000)))