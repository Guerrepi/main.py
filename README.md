import os, time, json, sqlite3, requests
from datetime import datetime, timedelta
from flask import Flask, request, abort

# --- Configuración ---
TOKEN = os.environ.get("BOT_TOKEN")  # pon tu token de BotFather como variable de entorno
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # opcional, para restringir comandos
DB_PATH = "po_bot.db"

app = Flask(__name__)

# --- DB mínima ---
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        chat_id INTEGER PRIMARY KEY,
        balance REAL DEFAULT 0,
        risk_pct REAL DEFAULT 1.0,
        paused INTEGER DEFAULT 0,
        created_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        ts TEXT,
        side TEXT,
        asset TEXT,
        expiry TEXT,
        payout REAL,
        stake REAL,
        note TEXT,
        result TEXT
    )""")
    con.commit()
    con.close()

def db():
    return sqlite3.connect(DB_PATH)

# --- Utilidades Telegram ---
def tg(method, payload):
    r = requests.post(f"{BASE_URL}/{method}", json=payload, timeout=15)
    if not r.ok:
        print("TG error:", r.text)
    return r.json() if r.ok else {}

def send_message(chat_id, text, reply_markup=None, parse="HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("sendMessage", payload)

def edit_message(chat_id, message_id, text, reply_markup=None, parse="HTML"):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("editMessageText", payload)

# --- Lógica de usuario ---
def get_user(chat_id):
    con = db(); cur = con.cursor()
    cur.execute("SELECT chat_id,balance,risk_pct,paused FROM users WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO users(chat_id, balance, risk_pct, paused, created_at) VALUES(?,?,?,?,?)",
                    (chat_id, 0.0, 1.0, 0, datetime.utcnow().isoformat()))
        con.commit()
        cur.execute("SELECT chat_id,balance,risk_pct,paused FROM users WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
    con.close()
    return {"chat_id": row[0], "balance": row[1], "risk_pct": row[2], "paused": bool(row[3])}

def set_config(chat_id, balance, risk_pct):
    con = db(); cur = con.cursor()
    cur.execute("UPDATE users SET balance=?, risk_pct=? WHERE chat_id=?", (balance, risk_pct, chat_id))
    con.commit(); con.close()

def set_paused(chat_id, paused):
    con = db(); cur = con.cursor()
    cur.execute("UPDATE users SET paused=? WHERE chat_id=?", (1 if paused else 0, chat_id))
    con.commit(); con.close()

def insert_trade(chat_id, side, asset, expiry, payout, stake, note):
    con = db(); cur = con.cursor()
    cur.execute("""INSERT INTO trades(chat_id, ts, side, asset, expiry, payout, stake, note, result)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (chat_id, datetime.utcnow().isoformat(), side, asset, expiry, payout, stake, note, "open"))
    con.commit()
    tid = cur.lastrowid
    con.close()
    return tid

def update_trade(chat_id, trade_id, result):
    con = db(); cur = con.cursor()
    cur.execute("UPDATE trades SET result=? WHERE id=? AND chat_id=?", (result, trade_id, chat_id))
    con.commit(); con.close()

def apply_result_to_balance(chat_id, stake, payout, result):
    win = (result == "win")
    delta = stake * payout if win else -stake
    con = db(); cur = con.cursor()
    cur.execute("UPDATE users SET balance = balance + ? WHERE chat_id=?", (delta, chat_id))
    con.commit(); con.close()
    return delta

def today_stats(chat_id):
    start = datetime.utcnow().date().isoformat()
    con = db(); cur = con.cursor()
    cur.execute("""SELECT result, stake, payout FROM trades
                   WHERE chat_id=? AND ts>=?""", (chat_id, start))
    rows = cur.fetchall(); con.close()
    wins = sum(1 for r,_,__ in rows if r=="win")
    losses = sum(1 for r,_,__ in rows if r=="loss")
    pnl = sum((s*p if r=="win" else -s) for r,s,p in rows)
    wr = (wins/(wins+losses)*100) if (wins+losses)>0 else 0
    return wins, losses, wr, pnl

# --- Helpers de mensajes ---
CHECKLIST = (
    "🧾 <b>Checklist</b>\n"
    "• Tendencia: EMA20 vs EMA50 ✔️\n"
    "• RSI cercano a 30/70 ✔️\n"
    "• Toque en banda de Bollinger ✔️\n"
    "• Vela de confirmación en M1 ✔️\n"
    "• Evitar noticias de alto impacto ✔️"
)

def build_signal_text(user, side, asset, expiry, payout, stake, note):
    arrow = "🟢 CALL" if side=="CALL" else "🔴 PUT"
    return (
        f"{arrow} <b>{asset}</b> • Exp: <b>{expiry}</b>\n"
        f"Riesgo: <b>{user['risk_pct']:.2f}%</b>  |  Stake sugerido: <b>{stake:.2f}</b>\n"
        f"Payout: <b>{payout:.2f}</b>  |  Banca: <b>{user['balance']:.2f}</b>\n"
        f"Nota: {note or '-'}\n\n{CHECKLIST}"
    )

def ikb_for_trade(trade_id):
    return {
        "inline_keyboard":[
            [{"text":"✅ Ganó","callback_data":f"res|{trade_id}|win"},
             {"text":"❌ Perdió","callback_data":f"res|{trade_id}|loss"}]
        ]
    }

# --- Seguridad simple (opcional) ---
def allowed(chat_id):
    if ADMIN_CHAT_ID:
        return str(chat_id) == str(ADMIN_CHAT_ID)
    return True

# --- Flask handlers ---
@app.route("/", methods=["GET"])
def home():
    return "OK"

@app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    # Mensajes
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip()

        if not allowed(chat_id):
            send_message(chat_id, "Acceso no autorizado.")
            return "ok"

        user = get_user(chat_id)
        if user["paused"] and not (text.startswith("/resume") or text.startswith("/help")):
            send_message(chat_id, "⏸️ Bot en pausa. Usa /resume para reanudar.")
            return "ok"

        if text.startswith("/start"):
            send_message(chat_id,
                "¡Hola! Soy tu bot de disciplina para PO Trade.\n\n"
                "Comienza con /config <balance> <riesgo_%>\n"
                "Ejemplo: <code>/config 200 1.5</code>\n\n"
                "Luego envía /call o /put con el activo y expiración.\n"
                "Ej: <code>/call EURUSD 1m 0.82 toque banda baja + RSI 31</code>")
        elif text.startswith("/help"):
            send_message(chat_id,
                "<b>Comandos</b>\n"
                "/config <balance> <riesgo_%>\n"
                "/call <activo> <exp> [payout] [nota]\n"
                "/put  <activo> <exp> [payout] [nota]\n"
                "/stats  •  /pause  •  /resume")
        elif text.startswith("/config"):
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
        elif text.startswith("/pause"):
            set_paused(chat_id, True)
            send_message(chat_id, "⏸️ Pausado.")
        elif text.startswith("/resume"):
            set_paused(chat_id, False)
            send_message(chat_id, "▶️ Reanudado.")
        elif text.startswith("/stats"):
            w,l,wr,pnl = today_stats(chat_id)
            u = get_user(chat_id)
            send_message(chat_id, f"📊 <b>Estadísticas de Hoy</b>\n"
                                  f"Wins: <b>{w}</b> | Losses: <b>{l}</b>\n"
                                  f"Winrate: <b>{wr:.1f}%</b>\n"
                                  f"PnL: <b>{pnl:.2f}</b>\n"
                                  f"Banca actual: <b>{u['balance']:.2f}</b>")
        elif text.startswith("/call") or text.startswith("/put"):
            parts = text.split(maxsplit=4)
            if len(parts) >= 3:
                side = "CALL" if text.startswith("/call") else "PUT"
                asset = parts[1]
                expiry = parts[2]
                payout = 0.80
                note = ""
                if len(parts) >= 4:
                    try:
                        payout = float(parts[3])
                        note = parts[4] if len(parts) == 5 else ""
                    except:
                        # si no es número, lo tratamos como nota completa
                        note = " ".join(parts[3:])
                stake = round(user["balance"] * (user["risk_pct"]/100.0), 2)
                tid = insert_trade(chat_id, side, asset, expiry, payout, stake, note)
                text_sig = build_signal_text(user, side, asset, expiry, payout, stake, note)
                send_message(chat_id, text_sig, reply_markup=ikb_for_trade(tid))
            else:
                send_message(chat_id, "Formato: <code>/call EURUSD 1m [payout] [nota]</code>")
        else:
            send_message(chat_id, "No entendí. Usa /help para ver comandos.")
    # Callbacks (botones)
    elif "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        data = cq.get("data","")
        msg_id = cq["message"]["message_id"]
        user = get_user(chat_id)
        if data.startswith("res|"):
            _, tid, res = data.split("|")
            tid = int(tid); result = "win" if res=="win" else "loss"
            # Busca trade
            con = db(); cur = con.cursor()
            cur.execute("SELECT side,asset,expiry,payout,stake,note FROM trades WHERE id=? AND chat_id=?", (tid, chat_id))
            row = cur.fetchone(); con.close()
            if not row:
                return "ok"
            side, asset, expiry, payout, stake, note = row
            update_trade(chat_id, tid, result)
            delta = apply_result_to_balance(chat_id, stake, payout, result)
            new_user = get_user(chat_id)
            prefix = "✅ <b>GANÓ</b>" if result=="win" else "❌ <b>PERDIÓ</b>"
            txt = (f"{prefix}\n"
                   f"{('🟢 CALL' if side=='CALL' else '🔴 PUT')} <b>{asset}</b> • Exp: <b>{expiry}</b>\n"
                   f"Resultado: <b>{result.upper()}</b>  |  Variación: <b>{delta:+.2f}</b>\n"
                   f"Banca: <b>{new_user['balance']:.2f}</b>\n\n"
                   f"Nota: {note or '-'}")
            # quitar botones al cerrar
            edit_message(chat_id, msg_id, txt)
            # confirmar callback
            tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Anotado."})
    return "ok"

# --- Webhook setup helper ---
@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    public_url = os.environ.get("PUBLIC_URL")  # ej: https://tu-servicio.onrender.com
    if not public_url:
        return "Falta PUBLIC_URL", 400
    r = requests.get(f"{BASE_URL}/setWebhook",
                     params={"url": f"{public_url}/webhook/{TOKEN}"},
                     timeout=15)
    return r.text, r.status_code

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
