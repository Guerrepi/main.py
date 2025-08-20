import os
import sqlite3
import requests
from flask import Flask, request
import yfinance as yf
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange, BollingerBands

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

# --------- UMBRALES (puedes afinarlos sin tocar la lógica) ----------
RSI15_CALL_MAX = 38   # señal CALL si RSI15 <= 38 y en banda baja
RSI15_PUT_MIN  = 62   # señal PUT  si RSI15 >= 62 y en banda alta
RSI1_CONFIRM_CALL = 50  # confirmación M1 CALL: RSI1 >= 50
RSI1_CONFIRM_PUT  = 50  # confirmación M1 PUT : RSI1 <= 50
BB_WINDOW = 20
BB_DEV = 2
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
# -------------------------------------------------------------------

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

# --- Utilidades / (dejanmos las funciones previas aunque no todas se usen) ---
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

# --- Estrategia: Bollinger + RSI + MACD (M15) con confirmación en M1 ---
def analyze_pair(symbol: str):
    try:
        # Más historial para MACD/BB en 15m
        m15 = yf.download(symbol, interval="15m", period="5d", progress=False)
        m1  = yf.download(symbol, interval="1m",  period="1d", progress=False)

        if m15 is None or m15.empty or len(m15) < 120:
            return None, "Datos 15m insuficientes"
        if m1 is None or m1.empty or len(m1) < 40:
            return None, "Datos 1m insuficientes"

        # ----- 15m (setup principal) -----
        bb15 = BollingerBands(close=m15["Close"], window=BB_WINDOW, window_dev=BB_DEV)
        lband15 = bb15.bollinger_lband()
        hband15 = bb15.bollinger_hband()
        basis15 = bb15.bollinger_mavg()

        rsi15 = get_rsi(m15["Close"], 14)
        macd15_ind = MACD(close=m15["Close"], window_slow=MACD_SLOW, window_fast=MACD_FAST, window_sign=MACD_SIGNAL)
        macd15 = macd15_ind.macd()
        macdsig15 = macd15_ind.macd_signal()

        c15 = float(m15["Close"].iloc[-1])
        lb15 = float(lband15.iloc[-1]); ub15 = float(hband15.iloc[-1]); mb15 = float(basis15.iloc[-1])
        r15 = float(rsi15.iloc[-1])
        macd15_now = float(macd15.iloc[-1]); macd15_prev = float(macd15.iloc[-2])
        sig15_now  = float(macdsig15.iloc[-1]); sig15_prev  = float(macdsig15.iloc[-2])

        cross_up_15   = (macd15_now > sig15_now) and (macd15_prev <= sig15_prev)
        cross_down_15 = (macd15_now < sig15_now) and (macd15_prev >= sig15_prev)

        call_setup_15 = (c15 <= lb15) and (r15 <= RSI15_CALL_MAX) and cross_up_15
        put_setup_15  = (c15 >= ub15) and (r15 >= RSI15_PUT_MIN) and cross_down_15

        # Si no hay setup en 15m, no seguimos
        if not (call_setup_15 or put_setup_15):
            return None, "15m sin setup (BB/RSI/MACD)"

        # ----- 1m (confirmación) -----
        bb1 = BollingerBands(close=m1["Close"], window=BB_WINDOW, window_dev=BB_DEV)
        basis1 = float(bb1.bollinger_mavg().iloc[-1])
        rsi1 = get_rsi(m1["Close"], 14)
        macd1_ind = MACD(close=m1["Close"], window_slow=MACD_SLOW, window_fast=MACD_FAST, window_sign=MACD_SIGNAL)
        macd1 = macd1_ind.macd(); macdsig1 = macd1_ind.macd_signal()

        c1 = float(m1["Close"].iloc[-1])
        r1 = float(rsi1.iloc[-1])
        macd1_now = float(macd1.iloc[-1]); macdsig1_now = float(macdsig1.iloc[-1])

        # Confirmaciones simples en 1m (momentum a favor)
        if call_setup_15:
            confirm = (r1 >= RSI1_CONFIRM_CALL) and (macd1_now > macdsig1_now) and (c1 >= basis1)
            if confirm:
                note = (f"CALL: 15m cierre<=LB ({c15:.5f}<= {lb15:.5f}), RSI15={r15:.1f}≤{RSI15_CALL_MAX}, "
                        f"MACD15 cruce↑; Confirmación 1m: RSI1={r1:.1f}≥{RSI1_CONFIRM_CALL}, MACD1>signal, Close≥Base")
                return "CALL", note
            return None, "Setup CALL en 15m pero sin confirmación en 1m"

        if put_setup_15:
            confirm = (r1 <= RSI1_CONFIRM_PUT) and (macd1_now < macdsig1_now) and (c1 <= basis1)
            if confirm:
                note = (f"PUT: 15m cierre>=UB ({c15:.5f}≥ {ub15:.5f}), RSI15={r15:.1f}≥{RSI15_PUT_MIN}, "
                        f"MACD15 cruce↓; Confirmación 1m: RSI1={r1:.1f}≤{RSI1_CONFIRM_PUT}, MACD1<signal, Close≤Base")
            #   return
                return "PUT", note
            return None, "Setup PUT en 15m pero sin confirmación en 1m"

        return None, "Sin señal"
    except Exception as e:
        return None, f"Error: {e}"

# --- Async handlers (igual que ya tenías) ---
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

# --- Rutas Flask (sin cambios) ---
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