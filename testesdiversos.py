from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os

# ================== CONFIG ==================
API_KEY = os.environ["API_KEY"]

print("CHAVE =", repr(API_KEY))

# ================== APP ==================
app = Flask(__name__)
CORS(app)

# ================== HOME ==================
@app.route("/")
def home():
    return "🤖 Sexta-Feira online"

# ================== CHAT ==================
@app.route("/chat", methods=["POST"])
def chat():
    try:

        # recebe mensagem do frontend
        data = request.get_json()

        mensagem = data["message"]

        # envia para OpenRouter
        resposta = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",

            headers={
                "Authorization": f"Bearer {API_KEY.strip()}",
                "HTTP-Referer": "https://sextafeira-lc.netlify.app",
                "X-Title": "Sexta-Feira",
                "Content-Type": "application/json"
            },

            json={
                "model": "meta-llama/llama-3-8b-instruct",

                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Você é Sexta-Feira, "
                            "uma assistente virtual feminina, "
                            "simpática, inteligente e futurista. "
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

        # transforma em json
        resposta_json = resposta.json()

        # mostra nos logs
        print(resposta_json)

        # pega resposta da IA
        texto = resposta_json["choices"][0]["message"]["content"]

        # envia pro frontend
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
    print("🤖 Sexta-Feira iniciando...")
    app.run(debug=True)
