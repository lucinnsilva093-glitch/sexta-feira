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

# ================== ESCOLHA DE IA ==================

def escolher_modelo(mensagem):

    mensagem = mensagem.lower()

    palavras_complexas = [
        "código",
        "programação",
        "python",
        "resolver",
        "equação",
        "matemática",
        "hack",
        "script",
        "explique",
        "ciência",
        "física",
        "química",
        "filosofia",
        "detalhado",
        "complexo",
        "difícil",
        "crie",
        "desenvolva"
    ]

    for palavra in palavras_complexas:

        if palavra in mensagem:

            # IA mais inteligente
            return "qwen/qwen-2.5-72b-instruct:free"

    # IA mais natural/conversa
    return "meta-llama/llama-3.3-70b-instruct:free"

# ================== CHAT ==================

@app.route("/chat", methods=["POST"])
def chat():

    try:

        data = request.get_json()

        mensagem = data["message"]

        # escolhe IA automaticamente
        modelo = escolher_modelo(mensagem)

        print("MODELO USADO:", modelo)

        resposta = requests.post(

            "https://openrouter.ai/api/v1/chat/completions",

            headers={

                "Authorization": f"Bearer {API_KEY.strip()}",
                "HTTP-Referer": "https://sextafeira-lc.netlify.app",
                "X-Title": "Sexta-Feira",
                "Content-Type": "application/json"
            },

            json={

                "model": modelo,

                "messages": [

                    {
                        "role": "system",

                        "content": (
                            "Você é Sexta-Feira, "
                            "uma assistente virtual feminina, "
                            "futurista, elegante, inteligente e natural. "
                            "Você conversa como uma IA avançada de filme futurista. "
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

        resposta_json = resposta.json()

        print(resposta_json)

        # pega resposta da IA
        texto = resposta_json["choices"][0]["message"]["content"]

        return jsonify({

            "response": texto,
            "model": modelo
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
