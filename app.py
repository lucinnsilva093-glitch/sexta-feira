import os
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS

from memoria import Memoria
from ia import IA
from database import criar_tabelas

# ==========================================================
# CONFIGURAÇÃO
# ==========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger(__name__)

# ==========================================================
# FLASK
# ==========================================================

app = Flask(__name__)

CORS(app)

# ==========================================================
# BANCO DE DADOS
# ==========================================================

criar_tabelas()

# ==========================================================
# MEMÓRIA
# ==========================================================

memoria = Memoria()

# ==========================================================
# IA
# ==========================================================

ia = IA(memoria)

# ==========================================================
# HOME
# ==========================================================

@app.route("/")
def home():

    return jsonify({

        "status": "online",

        "assistente": "Sexta-Feira",

        "versao": "0.8"

    })

# ==========================================================
# PING
# ==========================================================

@app.route("/ping")
def ping():

    return jsonify({

        "pong": True

    })

# ==========================================================
# PERGUNTAR
# ==========================================================

@app.route(
    "/perguntar",
    methods=["POST"]
)
def perguntar():

    try:

        data = request.get_json()

        if not data:

            return jsonify({
                "erro": "JSON inválido"
            }),400

        mensagem = (
            data.get(
                "mensagem",
                ""
            ).strip()
        )

        session_id = (
            data.get(
                "session_id",
                "anonimo"
            )
        )

        if not mensagem:

            return jsonify({
                "erro":"Mensagem vazia"
            }),400

        logger.info(
            "[%s] %s",
            session_id,
            mensagem
        )

        # ===========================
        # IA
        # ===========================

        resposta = ia.conversar(

            mensagem,

            session_id

        )

        return jsonify({

            "resposta": resposta,

            "sucesso": True

        })

    except Exception as erro:

        logger.exception(erro)

        return jsonify({

            "erro": str(erro),

            "sucesso": False

        }),500


# ==========================================================
# HISTÓRICO
# ==========================================================

@app.route(
    "/historico/<session_id>",
    methods=["GET"]
)
def historico(session_id):

    try:

        historico = memoria.historico(
            session_id
        )

        return jsonify({

            "historico": historico,

            "sucesso": True

        })

    except Exception as erro:

        return jsonify({

            "erro": str(erro),

            "sucesso": False

        }),500

# ==========================================================
# HEALTH
# ==========================================================

@app.route(
    "/health",
    methods=["GET"]
)
def health():

    return jsonify({

        "status": "ok",

        "versao": "0.8",

        "ia": "Sexta-Feira"

    })


# ==========================================================
# INFO
# ==========================================================

@app.route(
    "/info",
    methods=["GET"]
)
def info():

    return jsonify({

        "assistente": "Sexta-Feira",

        "versao": "0.8",

        "backend": "Flask",

        "memoria": "SQLite"

    })


# ==========================================================
# LIMPAR MEMÓRIA
# ==========================================================

@app.route(
    "/limpar_memoria/<session_id>",
    methods=["POST"]
)
def limpar_memoria(session_id):

    try:

        memoria.limpar(session_id)

        return jsonify({

            "sucesso": True,

            "mensagem": "Memória apagada."

        })

    except Exception as erro:

        return jsonify({

            "sucesso": False,

            "erro": str(erro)

        }),500


# ==========================================================
# 404
# ==========================================================

@app.errorhandler(404)
def pagina_nao_encontrada(e):

    return jsonify({

        "erro": "Rota não encontrada."

    }),404


# ==========================================================
# 500
# ==========================================================

@app.errorhandler(500)
def erro_servidor(e):

    logger.exception(e)

    return jsonify({

        "erro": "Erro interno do servidor."

    }),500


# ==========================================================
# MAIN
# ==========================================================

if __name__ == "__main__":

    porta = int(

        os.getenv(
            "PORT",
            5000
        )

    )

    logger.info(

        "Sexta-Feira iniciada."

    )

    app.run(

        host="0.0.0.0",

        port=porta,

        debug=False

    )
