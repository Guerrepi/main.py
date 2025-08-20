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

# --------- UMBRALES (ajustables sin tocar la lógica) ----------
# 15m (setup principal)
BB_WINDOW = 20
BB_DEV = 2.0
# “Cerca de banda” (p.ej. 15% del ancho de bandas)
BAND_TOL_FRAC = 0.15

RSI15_CALL_MAX = 45   # más laxo (antes 38)
RSI15_PUT_MIN  = 55   # más laxo (antes 62)

# MACD 15m: permitimos cruce o pendiente a favor
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Confirmación en 1m (momentum a favor)
RSI1_CONFIRM_CALL = 50   # suficiente que esté >= 50
RSI1_CONFIRM_PUT  = 50   # suficiente que esté <= 50
# ----------------------------------------------------------------

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

# --- Utilidades / (dejamos helpers previos aunque no todos se usen) ---
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

# --- Estrategia: BB + RSI + MACD (15m) con confirmación en 1m ---
def analyze_pair(symbol: str):
    try:
        # Más historial para MACD/BB en 15m
        m15 = yf.download(symbol, interval="15m", period="5d", progress=False)
        m1  = yf.download(symbol, interval="1m",  period="1d", progress=False)

        if m15 is None or m15.empty or len(m15) < 120:
            return None, "Datos 15m insuficientes"
        if m1 is None or m1.empty or len(m1) < 40:
            return None, "Datos 1m insuficientes"

        m15 = m15.dropna()
        m1  = m1.dropna()

        # ----- 15m (setup principal) -----
        close15 = m15["Close"]
        bb15 = BollingerBands(close=close15, window=BB_WINDOW, window_dev=BB_DEV)
        lband15 = bb15.bollinger_lband()
        hband15 = bb15.bollinger_hband()
        basis15 = bb15.bollinger_mavg()

        rsi15 = get_rsi(close15, 14)

        macd15_ind = MACD(close=close15, window_slow=MACD_SLOW, window_fast=MACD_FAST, window_sign=MACD_SIGNAL)
        macd15     = macd15_ind.macd()
        macdsig15  = macd15_ind.macd_signal()
        hist15     = macd15_ind.macd_diff()

        c15   = float(close15.iloc[-1])
        lb15  = float(lband15.iloc[-1]); ub15 = float(hband15.iloc[-1]); mb15 = float(basis15.iloc[-1])
        r15   = float(rsi15.iloc[-1])
        h15   = float(hist15.iloc[-1]); h15_1 = float(hist15.iloc[-2])

        # “Cerca de banda” con tolerancia
        band_width = max(ub15 - lb15, 1e-10)
        lower_thr  = lb15 + BAND_TOL_FRAC * band_width
        upper_thr  = ub15 - BAND_TOL_FRAC * band_width

        near_lower = (c15 <= lower_thr)
        near_upper = (c15 >= upper_thr)

        # MACD 15m: aceptamos cruce O pendiente a favor
        macd15_now   = float(macd15.iloc[-1]); macd15_prev = float(macd15.iloc[-2])
        sig15_now    = float(macdsig15.iloc[-1]); sig15_prev = float(macdsig15.iloc[-2])
        cross_up_15   = (macd15_now > sig15_now) and (macd15_prev <= sig15_prev)
        cross_down_15 = (macd15_now < sig15_now) and (macd15_prev >= sig15_prev)
        slope_up_15   = (h15 > h15_1)
        slope_dn_15   = (h15 < h15_1)

        call_setup_15 = near_lower and (r15 <= RSI15_CALL_MAX) and (cross_up_15 or slope_up_15)
        put_setup_15  = near_upper and (r15 >= RSI15_PUT_MIN)  and (cross_down_15 or slope_dn_15)

        if not (call_setup_15 or put_setup_15):
            return None, "15m sin setup (BB/RSI/MACD)"

        # ----- 1m (confirmación) -----
        close1 = m1["Close"]
        rsi1   = get_rsi(close1, 14)
        macd1i = MACD(close=close1, window_slow=MACD_SLOW, window_fast=MACD_FAST, window_sign=MACD_SIGNAL)
        macd1  = macd1i.macd()
        sig1   = macd1i.macd_signal()

        c1 = float(close1.iloc[-1])
        r1 = float(rsi1.iloc[-1])
        macd1_now = float(macd1.iloc[-1]); sig1_now = float(sig1.iloc[-1])

        # Confirmaciones razonables en 1m (momentum a favor)
        if call_setup_15:
            confirm = (macd1_now > sig1_now) or (r1 >= RSI1_CONFIRM_CALL)
            if confirm:
                note = (f"CALL: 15m cerca LB (c={c15:.5f}, LB*tol={lower_thr:.5f}), "
                        f"RSI15={r15:.1f}≤{RSI15_CALL_MAX}, MACD15 {'cruce↑' if cross_up_15 else 'pendiente↑'}; "
                        f"Conf 1m: {'MACD1>signal' if macd1_now>sig1_now else ''}"
                        f"{' y ' if (macd1_now>sig1_now and r1>=RSI1_CONFIRM_CALL) else ''}"
                        f"{'RSI1≥'+str(RSI1_CONFIRM_CALL) if r1>=RSI1_CONFIRM_CALL else ''}")
                return "CALL", note
            return None, "Setup CALL en 15m sin confirmación 1m"

        if put_setup_15:
            confirm = (macd1_now < sig1_now) or (r1 <= RSI1_CONFIRM_PUT)
            if confirm:
                note = (f"PUT: 15m cerca UB (c={c15:.5f}, UB*tol={upper_thr:.5f}), "
                        f"RSI15={r15:.1f}≥{RSI15_PUT_MIN}, MACD15 {'cruce↓' if cross_down_15 else 'pendiente↓'}; "
                        f"Conf 1m: {'MACD1<signal' if macd1_now<sig1_now else ''}"
                        f"{' y ' if (macd1_now<sig1_now and r1<=RSI1_CONFIRM_PUT) else ''}"
                        f"{'RSI1≤'+str(RSI1_CONFIRM_PUT) if r1<=RSI1_CONFIRM_PUT else ''}")
                return "PUT", note
            return None, "Setup PUT en 15m sin confirmación 1m"

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

# --- Rutas Flask (igual) ---
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