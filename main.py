import os
import sqlite3
import requests
from datetime import datetime
from flask import Flask, request

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
        risk_pct REAL DEFAULT 1.0,
        paused INTEGER DEFAULT 0
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

# --- Telegram helpers ---
def tg(method, payload):
    r = requests.post(f"{BASE_URL}/{method}", json=payload, timeout=15)
    return r.json() if r.ok else {}

def send_message(chat_id, text, reply_markup=None, parse="HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("sendMessage", payload)

def edit_message(chat_id, message_id, text, reply_markup=None, parse="HTML"):
    payload = {"chat_id": chat_id, "message_id": message_id,
               "text": text, "parse_mode": parse}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("editMessageText", payload)

# --- Users ---
def get_user(chat_id):
    con = db(); cur = con.cursor()
    cur.execute("SELECT chat_id,balance,risk_pct,paused FROM users WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO users(chat_id,balance,risk_pct,paused) VALUES(?,?,?,?)",
                    (chat_id, 0.0, 1.0, 0))
        con.commit()
        cur.execute("SELECT chat_id,balance,risk_pct,paused FROM users WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
    con.close()
    return {"chat_id": row[0], "balance": row[1], "risk_pct": row[2], "paused": bool(row[3])}

def set_config(chat_id, balance, risk_pct):
    con = db(); cur = con.cursor()
    cur.execute("UPDATE users SET balance=?,risk_pct=? WHERE chat_id=?",
                (balance, risk_pct, chat_id))
    con.commit(); con.close()

def set_paused(chat_id, paused):
    con = db(); cur = con.cursor()
    cur.execute("UPDATE users SET paused=? WHERE chat_id=?",
                (1 if paused else 0, chat_id))
    con.commit(); con.close()

# --- Trades ---
def insert_trade(chat_id, side, asset, expiry, payout, stake, note):
    con = db(); cur = con.cursor()
    cur.execute("""INSERT INTO trades(chat_id, ts, side, asset, expiry, payout, stake, note, result)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (chat_id, datetime.utcnow().isoformat(), side, asset, expiry, payout, stake, note, "open"))
    con.commit(); tid = cur.lastrowid; con.close()
    return tid

def update_trade(chat_id, trade_id, result):
    con = db(); cur = con.cursor()
    cur.execute("UPDATE trades SET result=? WHERE id=? AND chat_id=?",
                (result, trade_id, chat_id))
    con.commit(); con.close()

def apply_result(chat_id, stake, payout, result):
    win = (result == "win")
    delta = stake * payout if win else -stake
    con = db(); cur = con.cursor()
    cur.execute("UPDATE users SET balance=balance+? WHERE chat_id=?", (delta, chat_id))
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

# --- Messages ---
def build_signal_text(user, side, asset, expiry, payout, stake, note):
    arrow = "🟢 CALL" if side=="CALL" else "🔴 PUT"
    return (f"{arrow} <b>{asset}</b> • Exp: <b>{expiry}</b>\n"
            f"Riesgo: <b>{user['risk_pct']:.2f}%</b> | Stake: <b>{stake:.2f}</b>\n"
            f"Payout: <b>{payout:.2f}</b> | Banca: <b>{user['balance']:.2f}</b>\n"
            f"Nota: {note or '-'}")

def ikb_for_trade(trade_id):
    return {"inline_keyboard":[
        [{"text":"✅ Ganó","callback_data":f"res|{trade_id}|win"},
         {"text":"❌ Perdió","callback_data":f"res|{trade_id}|loss"}]
    ]}

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

        if user["paused"] and text not in ["/resume","/help"]:
            send_message(chat_id,"⏸ Bot en pausa. Usa /resume para reanudar.")
            return "ok",200

        if text.startswith("/start"):
            send_message(chat_id,"👋 Bienvenido! Configura tu banca con:\n<code>/config 200 1.5</code>")
        elif text.startswith("/help"):
            send_message(chat_id,"<b>Comandos:</b>\n/config <bal> <riesgo%>\n/call <activo> <exp> [payout] [nota]\n/put <activo> <exp> [payout] [nota]\n/stats\n/pause | /resume")
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
        elif text.startswith("/pause"):
            set_paused(chat_id,True); send_message(chat_id,"⏸ Pausado.")
        elif text.startswith("/resume"):
            set_paused(chat_id,False); send_message(chat_id,"▶️ Reanudado.")
        elif text.startswith("/stats"):
            w,l,wr,pnl = today_stats(chat_id)
            u=get_user(chat_id)
            send_message(chat_id,f"📊 <b>Hoy</b>\nWins: {w} | Losses: {l}\nWinrate: {wr:.1f}%\nPnL: {pnl:.2f}\nBanca: {u['balance']:.2f}")
        elif text.startswith("/call") or text.startswith("/put"):
            parts=text.split(maxsplit=4)
            if len(parts)>=3:
                side="CALL" if text.startswith("/call") else "PUT"
                asset=parts[1]; expiry=parts[2]
                payout=0.80; note=""
                if len(parts)>=4:
                    try:
                        payout=float(parts[3])
                        note=parts[4] if len(parts)==5 else ""
                    except:
                        note=" ".join(parts[3:])
                stake=round(user["balance"]*(user["risk_pct"]/100),2)
                tid=insert_trade(chat_id,side,asset,expiry,payout,stake,note)
                send_message(chat_id,build_signal_text(user,side,asset,expiry,payout,stake,note),
                             reply_markup=ikb_for_trade(tid))
            else:
                send_message(chat_id,"Formato: /call EURUSD 1m [payout] [nota]")
    elif "callback_query" in update:
        cq=update["callback_query"]
        chat_id=cq["message"]["chat"]["id"]; data=cq.get("data","")
        msg_id=cq["message"]["message_id"]
        if data.startswith("res|"):
            _,tid,res=data.split("|")
            tid=int(tid); result="win" if res=="win" else "loss"
            con=db(); cur=con.cursor()
            cur.execute("SELECT side,asset,expiry,payout,stake,note FROM trades WHERE id=? AND chat_id=?",(tid,chat_id))
            row=cur.fetchone(); con.close()
            if not row: return "ok",200
            side,asset,expiry,payout,stake,note=row
            update_trade(chat_id,tid,result)
            delta=apply_result(chat_id,stake,payout,result)
            new_u=get_user(chat_id)
            prefix="✅ GANÓ" if result=="win" else "❌ PERDIÓ"
            txt=(f"{prefix}\n{('🟢 CALL' if side=='CALL' else '🔴 PUT')} {asset} Exp: {expiry}\n"
                 f"Resultado: {result.upper()} | Δ {delta:+.2f}\nBanca: {new_u['balance']:.2f}\nNota: {note or '-'}")
            edit_message(chat_id,msg_id,txt)
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