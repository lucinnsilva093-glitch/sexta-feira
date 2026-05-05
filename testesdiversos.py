from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import pyttsx3
import threading
import os
API_KEY = os.getenv("API_KEY")

# ================== APP ==================
app = Flask(__name__)
CORS(app)


# ================== VOZ ==================
engine = pyttsx3.init()

voices = engine.getProperty('voices')

# 🔥 tenta voz feminina (pode mudar índice se precisar)
engine.setProperty('voice', "HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_PT-BR_MARIA_11.0")

engine.setProperty('rate', 180)  # velocidade da fala
import pyttsx3
import threading

def falar(texto):
    try:
        engine = pyttsx3.init()  # 🔥 cria novo engine toda vez

        voices = engine.getProperty('voices')
        engine.setProperty('voice', "HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_PT-BR_MARIA_11.0")  # ajusta se quiser
        engine.setProperty('rate', 180)

        engine.say(texto)
        engine.runAndWait()
        engine.stop()

    except Exception as e:
        print("Erro voz:", e)

def falar_async(texto):
    threading.Thread(target=falar, args=(texto,), daemon=True).start()

# ================== IA ==================
def perguntar_ia(msg):
    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "openrouter/auto",
                "messages": [
                    {
                        "role": "system",
                        "content": "Você é Sexta-Feira, uma assistente virtual feminina, simpática e inteligente. Sempre responda em português do Brasil."
                    },
                    {
                        "role": "user",
                        "content": msg
                    }
                ]
            }
        )

        data = response.json()

        return data["choices"][0]["message"]["content"]

    except Exception as e:
        print("ERRO IA:", e)
        return "Deu erro ao pensar 😅"

# ================== ROTA ==================
@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    msg = data.get("message", "")

    resposta = perguntar_ia(msg)

    # 🔊 fala sem travar
    falar_async(resposta)

    return jsonify({"response": resposta})

# ================== RUN ==================
if __name__ == "__main__":
    print("🤖 Sexta-Feira online...")
    app.run(debug=True)