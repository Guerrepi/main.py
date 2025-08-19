import os
import requests
from flask import Flask, request

app = Flask(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

@app.route("/")
def home():
    return "Bot está vivo ✅"

# Ruta de webhook SIN token en la URL
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True)
    print("📩 Update recibido:", update)
    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")
        if text == "/start":
            send_message(chat_id, "👋 Hola, el bot ya está conectado correctamente ✅")
        else:
            send_message(chat_id, f"Echo: {text}")
    return "ok", 200

def send_message(chat_id, text):
    requests.post(f"{BASE_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text
    })

@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    public_url = os.environ.get("PUBLIC_URL")
    if not public_url:
        return "Falta PUBLIC_URL", 400
    url = f"{public_url}/webhook"
    r = requests.get(f"{BASE_URL}/setWebhook", params={"url": url})
    return r.text, r.status_code

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))