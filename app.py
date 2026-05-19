import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, g, jsonify, request
from flask_cors import CORS

# =========================================================
# CONFIG
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

OPENROUTER_MODEL = "openrouter/free"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

REQUEST_TIMEOUT = 60
MAX_EXCHANGES = 10
SESSION_TTL_SECONDS = 3600
CLEANUP_INTERVAL_SECONDS = 300

SEARCH_RATE_LIMIT = 10
SEARCH_RATE_WINDOW = 60
SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 100
CONTEXT_CHARS = 80

SYSTEM_PROMPT = """
Você é Sexta-Feira,
uma inteligência artificial avançada.

Seu comportamento:
- natural
- inteligente
- objetiva
- amigável
- sem inventar fatos
- sem misturar informações
- responde em português brasileiro
- possui memória das conversas anteriores
"""

# =========================================================
# BANCO DE MEMÓRIA
# =========================================================

conn = sqlite3.connect(
    "memoria.db",
    check_same_thread=False
)

cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS memoria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pergunta TEXT,
    resposta TEXT,
    timestamp TEXT
)
""")

conn.commit()

# =========================================================
# MEMÓRIA RAM
# =========================================================

_sessions = {}
_lock = threading.Lock()

_search_rate = defaultdict(list)
_rate_lock = threading.Lock()

_server_start = datetime.now(timezone.utc)

_stats = {
    "request_count": 0,
    "total_response_ms": 0.0,
    "search_count": 0,
}

_stats_lock = threading.Lock()

# =========================================================
# HELPERS
# =========================================================

def _now():
    return datetime.now(timezone.utc)

def _iso(dt):
    return dt.isoformat()

def _get_or_create(session_id):

    if session_id not in _sessions:

        ts = _now()

        _sessions[session_id] = {
            "messages": [],
            "created_at": ts,
            "last_activity": ts,
        }

    return _sessions[session_id]

def _touch(session):
    session["last_activity"] = _now()

def _trim(session):

    max_msgs = MAX_EXCHANGES * 2

    if len(session["messages"]) > max_msgs:
        session["messages"] = session["messages"][-max_msgs:]

def _history_for_api(session):

    return [
        {
            "role": m["role"],
            "content": m["content"]
        }
        for m in session["messages"]
    ]

def _memory_bytes(session):

    return len(
        json.dumps(
            session["messages"],
            ensure_ascii=False
        ).encode("utf-8")
    )

# =========================================================
# LIMPEZA
# =========================================================

def _cleanup_stale_sessions():

    cutoff = _now() - timedelta(
        seconds=SESSION_TTL_SECONDS
    )

    with _lock:

        stale = [
            sid for sid, s in _sessions.items()
            if s["last_activity"] < cutoff
        ]

        for sid in stale:
            del _sessions[sid]

    if stale:

        logger.info(
            "Limpeza automática: %d sessão(ões) removida(s)",
            len(stale)
        )

def _schedule_cleanup():

    _cleanup_stale_sessions()

    t = threading.Timer(
        CLEANUP_INTERVAL_SECONDS,
        _schedule_cleanup
    )

    t.daemon = True
    t.start()

if not os.environ.get("WERKZEUG_RUN_MAIN"):
    _schedule_cleanup()

# =========================================================
# REQUEST TRACKING
# =========================================================

HEALTHZ_PATH = "/flask-api/healthz"

@app.before_request
def _before():

    if request.path != HEALTHZ_PATH:
        g._req_start = time.perf_counter()

@app.after_request
def _after(response):

    if request.path != HEALTHZ_PATH and hasattr(g, "_req_start"):

        elapsed_ms = (
            time.perf_counter() - g._req_start
        ) * 1000

        with _stats_lock:

            _stats["request_count"] += 1
            _stats["total_response_ms"] += elapsed_ms

    return response

# =========================================================
# MEMÓRIA INTELIGENTE
# =========================================================

def buscar_memorias():

    try:

        cursor.execute("""
        SELECT pergunta, resposta
        FROM memoria
        ORDER BY id DESC
        LIMIT 5
        """)

        memorias = cursor.fetchall()

        contexto = ""

        for pergunta, resposta in memorias:

            contexto += (
                f"Usuário: {pergunta}\n"
                f"Sexta-Feira: {resposta}\n\n"
            )

        return contexto

    except Exception as erro:

        logger.error(
            "Erro ao buscar memória: %s",
            erro
        )

        return ""

def salvar_memoria(pergunta, resposta):

    try:

        cursor.execute("""
        INSERT INTO memoria (
            pergunta,
            resposta,
            timestamp
        )
        VALUES (?, ?, ?)
        """, (
            pergunta,
            resposta,
            datetime.now().isoformat()
        ))

        conn.commit()

    except Exception as erro:

        logger.error(
            "Erro ao salvar memória: %s",
            erro
        )

# =========================================================
# IA
# =========================================================

@app.route("/perguntar", methods=["POST"])
def perguntar():

    try:

        data = request.get_json()

        mensagem = data.get(
            "mensagem",
            ""
        ).strip()

        if not mensagem:

            return jsonify({
                "erro": "Mensagem vazia"
            }), 400

        contexto_memoria = buscar_memorias()

        messages_for_api = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            },
            {
                "role": "system",
                "content":
                    "Memórias recentes:\n\n"
                    f"{contexto_memoria}"
            },
            {
                "role": "user",
                "content": mensagem
            }
        ]

        logger.info(
            "Pergunta recebida: %s",
            mensagem
        )

        response = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization":
                    f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type":
                    "application/json",
                "HTTP-Referer":
                    "http://localhost",
                "X-Title":
                    "Sexta-Feira"
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": messages_for_api
            },
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code != 200:

            logger.error(
                "Erro OpenRouter: %s",
                response.text
            )

            return jsonify({
                "erro": "Erro OpenRouter",
                "detalhes": response.text
            }), 500

        resposta = response.json()

        texto = (
            resposta["choices"][0]
            ["message"]["content"]
        )

        salvar_memoria(
            mensagem,
            texto
        )

        logger.info(
            "Resposta enviada com sucesso"
        )

        return jsonify({
            "resposta": texto
        })

    except Exception as erro:

        logger.exception(
            "Erro interno do servidor"
        )

        return jsonify({
            "erro":
                "Erro interno do servidor",
            "detalhes":
                str(erro)
        }), 500

# =========================================================
# HISTÓRICO
# =========================================================

@app.route(
    "/flask-api/historico",
    methods=["GET"]
)
def historico():

    session_id = request.args.get(
        "session_id",
        ""
    ).strip()

    if not session_id:

        return jsonify({
            "erro":
                "Parâmetro session_id obrigatório"
        }), 400

    with _lock:

        session = _sessions.get(
            session_id
        )

        if not session:

            return jsonify({
                "session_id": session_id,
                "historico": [],
                "total_mensagens": 0
            })

        msgs = list(session["messages"])

    return jsonify({
        "session_id": session_id,
        "historico": msgs,
        "total_mensagens": len(msgs)
    })

# =========================================================
# LIMPAR HISTÓRICO
# =========================================================

@app.route(
    "/flask-api/limpar-historico",
    methods=["POST"]
)
def limpar_historico():

    dados = (
        request.get_json(
            silent=True
        ) or {}
    )

    session_id = dados.get(
        "session_id",
        ""
    ).strip()

    if not session_id:

        return jsonify({
            "erro":
                "Campo session_id obrigatório"
        }), 400

    with _lock:

        session = _sessions.pop(
            session_id,
            None
        )

    removidas = (
        len(session["messages"])
        if session else 0
    )

    return jsonify({
        "mensagens_removidas": removidas,
        "mensagem": "Histórico apagado"
    })

# =========================================================
# HEALTH
# =========================================================

@app.route(
    HEALTHZ_PATH,
    methods=["GET"]
)
def healthz():

    now = _now()

    uptime_secs = (
        now - _server_start
    ).total_seconds()

    with _stats_lock:

        req_count = _stats["request_count"]

        avg_response_ms = 0

        if req_count:

            avg_response_ms = round(
                _stats["total_response_ms"]
                / req_count,
                2
            )

    return jsonify({
        "status": "ok",
        "timestamp": _iso(now),
        "uptime_segundos":
            round(uptime_secs, 1),
        "requests": req_count,
        "tempo_medio_ms":
            avg_response_ms,
        "modelo": OPENROUTER_MODEL
    })

# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():

    return {
        "status": "online",
        "ia": "Sexta-Feira",
        "mensagem":
            "Sistema operacional ativo."
    }, 200

@app.route("/ping")
def ping():

    return {
        "pong": True
    }, 200

# =========================================================
# ERROS
# =========================================================

@app.errorhandler(404)
def not_found(e):

    return jsonify({
        "erro":
            "Rota não encontrada"
    }), 404

@app.errorhandler(405)
def method_not_allowed(e):

    return jsonify({
        "erro":
            "Método não permitido"
    }), 405

@app.errorhandler(500)
def server_error(e):

    logger.exception(
        "Erro interno do servidor"
    )

    return jsonify({
        "erro":
            "Erro interno do servidor"
    }), 500

# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    debug = (
        os.environ.get(
            "FLASK_ENV"
        ) == "development"
    )

    if not OPENROUTER_API_KEY:

        logger.warning(
            "OPENROUTER_API_KEY não configurada"
        )

    logger.info(
        "Iniciando servidor na porta %d",
        port
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=debug
    )
