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
     print("ERRO:", e)
     return jsonify({"response": str(e)})

# ================== ROTA ==================
@app.route("/chat", methods=["POST"])
def chat():
    try:
        data = request.get_json()
        mensagem = data["message"]

        resposta = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "openai/gpt-3.5-turbo",
                "messages": [
                    {"role": "user", "content": mensagem}
                ]
            }
        )

        resposta_json = resposta.json()

        print(resposta_json)

        texto = resposta_json["choices"][0]["message"]["content"]

        return jsonify({
            "response": texto
        })

    except Exception as e:
        print("ERRO:", e)
        return jsonify({
            "response": str(e)
        })
    # 🔊 fala sem travar

    return jsonify({"response": resposta})

# ================== RUN ==================
if __name__ == "__main__":
    print("🤖 Sexta-Feira online...")
    app.run(debug=True)
print("CHAVE:", API_KEY)
