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

    data = request.get_json()

    mensagem = data["message"]

    return jsonify({
        "response": "Recebi: " + mensagem
    })
# ================== RUN ==================
if __name__ == "__main__":
    print("🤖 Sexta-Feira online...")
    app.run(debug=True)
