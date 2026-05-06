from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os

# ================== CONFIG ==================
API_KEY = "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"
print("API_KEY =", repr(API_KEY))
# ================== APP ==================
app = Flask(__name__)
CORS(app)

# ================== ROTA ==================
@app.route("/")
def home():
    return "🔥 TESTE NOVO 123 🔥"

# ================== CHAT ==================
@app.route("/chat", methods=["POST"])
def chat():
    try:
        # pega mensagem enviada pelo site
        data = request.get_json()
        mensagem = data["message"]

        # envia para OpenRouter
        resposta = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "meta-llama/llama-3-8b-instruct",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Você é Sexta-Feira, uma assistente virtual feminina, "
                            "inteligente, simpática e futurista. "
                            "Sempre responda em português do Brasil."
                        )
                    },
                    {
                        "role": "user",
                        "content": mensagem
                    }
                ]
            }
        )

        # transforma resposta em json
        resposta_json = resposta.json()

        # mostra logs no Render
        print(resposta_json)

        # pega texto da IA
        texto = resposta_json["choices"][0]["message"]["content"]

        # envia pro site
        return jsonify({
            "response": texto
        })

    except Exception as e:
        print("ERRO:", e)

        return jsonify({
            "response": str(e)
        })

# ================== RUN ==================
if __name__ == "__main__":
    print("🤖 Sexta-Feira online...")
    app.run(debug=True)
