import os
import sqlite3
import requests
from flask import Flask, request
import yfinance as yf
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange

# --- Configuración ---
TOKEN = os.environ.get("BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
DB_PATH = "po_bot.db"
EXEC = ThreadPoolExecutor(max_workers=4)

# Pares soportados
AVAILABLE_PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD",
    "USDCHF", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY"
]

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

# --- Telegram ---
def tg(method, payload):
    r = requests.post(f"{BASE_URL}/{method}", json=payload, timeout=15)
    return r.json() if r.ok else {}

def send_message(chat_id, text):
    return tg("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})

# --- Indicadores / utilidades ---
def yahoo_symbol(pair: str) -> str:
    pair = pair.upper().replace(" ", "")
    return pair if pair.endswith("=X") else f"{pair}=X"

def get_ema(series: pd.Series, window: int) -> pd.Series:
    return EMAIndicator(close=series, window=window).ema_indicator()

def get_rsi(series: pd.Series, window: int = 14) -> pd.Series:
    return RSIIndicator(close=series, window=window).rsi()

def get_atr(h: pd.Series, l: pd.Series, c: pd.Series, window: int = 14) -> pd.Series:
    return AverageTrueRange(high=h, low=l, close=c, window=window).average_true_range()

def is_engulfing_bull(po, pc, lo, lc) -> bool:
    return (lc > lo) and (pc < po) and (lc >= max(po, pc)) and (lo <= min(po, pc))

def is_engulfing_bear(po, pc, lo, lc) -> bool:
    return (lc < lo) and (pc > po) and (lc <= min(po, pc)) and (lo >= max(po, pc))

# --- Estrategia ---
def analyze_pair(symbol: str):
    try:
        m15 = yf.download(symbol, interval="15m", period="4d", progress=False)
        m1  = yf.download(symbol, interval="1m",  period="1d", progress=False)

        if m15 is None or m15.empty or len(m15) < 210:
            return None, "Datos M15 insuficientes"
        if m1 is None or m1.empty or len(m1) < 30:
            return None, "Datos M1 insuficientes"

        ema50  = get_ema(m15['Close'], 50)
        ema200 = get_ema(m15['Close'], 200)
        trend = "up" if float(ema50.iloc[-1]) > float(ema200.iloc[-1]) else "down"

        rsi = get_rsi(m1['Close'], 14)
        atr = get_atr(m1['High'], m1['Low'], m1['Close'], 14)
        last_rsi = float(rsi.iloc[-1])
        last_atr = float(atr.iloc[-1]); avg_atr = float(atr.iloc[-20:].mean())

        last = m1.iloc[-1]; prev = m1.iloc[-2]
        bull = is_engulfing_bull(prev['Open'], prev['Close'], last['Open'], last['Close'])
        bear = is_engulfing_bear(prev['Open'], prev['Close'], last['Open'], last['Close'])

        if last_atr <= avg_atr:
            return None, "ATR bajo (poca volatilidad)"

        if trend == "up" and last_rsi < 30 and bull:
            return "CALL", "RSI < 30 + Tendencia alcista + Engulfing alcista"
        if trend == "down" and last_rsi > 70 and bear:
            return "PUT",  "RSI > 70 + Tendencia bajista + Engulfing bajista"

        return None, "No cumple condiciones"
    except Exception as e:
        return None, f"Error: {e}"

# --- Async handlers ---
def handle_signal_async(chat_id, pair, user):
    try:
        sig, note = analyze_pair(yahoo_symbol(pair))
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
    except Exception as e:
        send_message(chat_id, f"⚠️ Error en /signal {pair}: {e}")

def handle_signalall_async(chat_id, user):
    try:
        futures = {p: EXEC.submit(analyze_pair, yahoo_symbol(p)) for p in AVAILABLE_PAIRS}
        results = []
        for pair, fut in futures.items():
            try:
                sig, note = fut.result(timeout=25)
                if sig:
                    stake = round(user["balance"] * (user["risk_pct"] / 100.0), 2)
                    icon = "🟢" if sig == "CALL" else "🔴"
                    results.append(f"{icon} {pair}: {sig} | Stake: {stake:.2f}\n{note}")
            except Exception:
                pass
        final = ("📊 <b>Señales encontradas</b>\n\n" + "\n\n".join(results)) if results \
                else "❌ No se encontraron señales claras en los pares disponibles."
        send_message(chat_id, final)
    except Exception as e:
        send_message(chat_id, f"⚠️ Error en /signalall: {e}")

# --- Rutas Flask ---
@app.route("/")
def home():
    return "Bot vivo ✅"

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True)
    if not update:
        return "ok", 200

    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = (update["message"].get("text") or "").strip()

        if text.startswith("/start"):
            send_message(chat_id, "👋 Bienvenido! Usa /config <balance> <riesgo%> y /signal <par>.\n\nDisponibles: " + ", ".join(AVAILABLE_PAIRS))
            return "ok", 200

        elif text.startswith("/config"):
            parts = text.split()
            if len(parts) >= 3:
                try:
                    bal = float(parts[1]); rp = float(parts[2])
                    set_config(chat_id, bal, rp)
                    send_message(chat_id, f"✅ Configurado. Balance: {bal:.2f} | Riesgo: {rp:.2f}%")
                except:
                    send_message(chat_id, "Formato inválido. Ej: /config 200 1.5")
            else:
                send_message(chat_id, "Formato inválido. Ej: /config 200 1.5")
            return "ok", 200

        # --- IMPORTANTE: /signalall ANTES que /signal ---
        elif text.startswith("/signalall"):
            user = get_user(chat_id)
            send_message(chat_id, "⏳ Analizando todos los pares disponibles…")
            EXEC.submit(handle_signalall_async, chat_id, user)
            return "ok", 200

        elif text.startswith("/signal"):
            parts = text.split()
            if len(parts) >= 2:
                pair = parts[1].upper()
                if pair not in AVAILABLE_PAIRS:
                    send_message(chat_id, "⚠️ Par no soportado. Disponibles: " + ", ".join(AVAILABLE_PAIRS))
                    return "ok", 200
                user = get_user(chat_id)
                send_message(chat_id, f"⏳ Analizando {pair}…")
                EXEC.submit(handle_signal_async, chat_id, pair, user)
            else:
                send_message(chat_id, "Formato: /signal EURUSD")
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