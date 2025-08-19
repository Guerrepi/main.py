import os
import requests
from flask import Flask, request

app = Flask(__name__)

# 🔑 Configuración desde variables de entorno
TOKEN = os.environ.get("BOT_TOKEN")  # tu token de BotFather (en Render → Environment)
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

# Ruta base (para probar salud del servicio)
@app.route("/", methods=["GET"])
def home():
    return "Bot está vivo ✅"

# Ruta para setear el webhook
@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    public_url = os.environ.get("PUBLIC_URL")  # en Render → Environment
    if not public_url:
        return "⚠️ Falta PUBLIC_URL en variables de entorno", 400

    url = f"{public_url}/webhook/{TOKEN}"
    r = requests.get(f"{BASE_URL}/setWebhook", params={"url": url})
    return r.text, r.status_code

# Ruta donde Telegram enviará las actualizaciones
@app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True)
    print("📩 Update recibido:", update)  # lo verás en los logs de Render

    # ejemplo de respuesta automática simple:
    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")
        if text == "/start":
            send_message(chat_id, "👋 ¡Hola! El bot ya está conectado correctamente ✅")
        else:
            send_message(chat_id, f"Echo: {text}")

    return "ok", 200

# Función auxiliar para enviar mensajes
def send_message(chat_id, text):
    requests.post(f"{BASE_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))