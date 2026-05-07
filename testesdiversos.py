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

        modelos = [

            "meta-llama/llama-3.3-70b-instruct:free",

            "qwen/qwen-2.5-72b-instruct:free",

            "deepseek/deepseek-r1:free",

            "meta-llama/llama-3-8b-instruct"
        ]

        resposta_texto = None
        modelo_usado = None

        for modelo in modelos:

            try:

                print("Tentando modelo:", modelo)

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
                                    "futurista, elegante e inteligente. "
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

                if "choices" in resposta_json:

                    resposta_texto = (
                        resposta_json["choices"][0]
                        ["message"]["content"]
                    )

                    modelo_usado = modelo

                    break

            except Exception as erro_modelo:

                print("Erro no modelo:", modelo)
                print(erro_modelo)

        if resposta_texto:

            return jsonify({

                "response": resposta_texto,
                "model": modelo_usado
            })

        else:

            return jsonify({

                "response":
                "⚠️ Todas as IAs estão ocupadas no momento."
            })

    except Exception as e:

        print("ERRO GERAL:", e)

        return jsonify({

            "response": str(e)
        })
# ================== RUN ==================

if __name__ == "__main__":

    print("🤖 Sexta-Feira iniciando...")

    app.run(debug=True)
