from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import threading
import os
API_KEY = os.getenv("API_KEY")

# ================== APP ==================
app = Flask(__name__)
CORS(app)

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

    return jsonify({"response": resposta})

# ================== RUN ==================
if __name__ == "__main__":
    print("🤖 Sexta-Feira online...")
    app.run(debug=True)
