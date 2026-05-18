import json
import logging
import os
import re
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, g, jsonify, make_response, request
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

OPENROUTER_MODEL = [

    "google/gemini-2.0-flash-exp:free",

    "meta-llama/llama-3-8b-instruct:free",

    "deepseek/deepseek-chat:free"

]

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

REQUEST_TIMEOUT = 30
MAX_EXCHANGES = 10
SESSION_TTL_SECONDS = 3600
CLEANUP_INTERVAL_SECONDS = 300

SEARCH_RATE_LIMIT = 10
SEARCH_RATE_WINDOW = 60
SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 100
CONTEXT_CHARS = 80

SYSTEM_PROMPT = """
Você é Sexta Feira,
uma inteligência artificial avançada.

Responda:
- naturalmente
- sem inventar fatos
- objetiva
- inteligente
- sem misturar cidades ou informações erradas
"""

# =========================================================
# MEMÓRIA
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
# CLEANUP
# =========================================================

def _cleanup_stale_sessions():

    cutoff = _now() - timedelta(seconds=SESSION_TTL_SECONDS)

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
# IA
# =========================================================

@app.route("/perguntar", methods=["POST"])
def perguntar():

    dados = request.json
    pergunta = dados.get("mensagem")

    ultimo_erro = "Nenhum modelo respondeu"

    for modelo in OPENROUTER_MODELS:

        try:

            response = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "http://localhost",
                    "X-Title": "Sexta-Feira"
                },
                json={
                    "model": modelo,
                    "messages": [
                        {
                            "role": "system",
                            "content": SYSTEM_PROMPT
                        },
                        {
                            "role": "user",
                            "content": pergunta
                        }
                    ]
                },
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code != 200:

                logger.warning(
                    "Modelo falhou: %s | %s",
                    modelo,
                    response.text[:200]
                )

                ultimo_erro = response.text

                continue

            resposta = response.json()

            texto = resposta["choices"][0]["message"]["content"]

            logger.info("Modelo usado: %s", modelo)

            return jsonify({
                "resposta": texto,
                "modelo": modelo
            })

        except Exception as erro:

            logger.error(
                "Erro no modelo %s: %s",
                modelo,
                erro
            )

            ultimo_erro = str(erro)

            continue

    return jsonify({
        "erro": "Todos os modelos falharam",
        "detalhes": ultimo_erro
    }), 500
# =========================================================
# HISTÓRICO
# =========================================================

@app.route("/flask-api/historico", methods=["GET"])
def historico():

    session_id = request.args.get("session_id", "").strip()

    if not session_id:
        return jsonify({
            "erro": "Parâmetro 'session_id' obrigatório"
        }), 400

    with _lock:

        session = _sessions.get(session_id)

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

@app.route("/flask-api/limpar-historico", methods=["POST"])
def limpar_historico():

    dados = request.get_json(silent=True) or {}

    session_id = dados.get("session_id", "").strip()

    if not session_id:
        return jsonify({
            "erro": "Campo 'session_id' obrigatório"
        }), 400

    with _lock:
        session = _sessions.pop(session_id, None)

    removidas = len(session["messages"]) if session else 0

    return jsonify({
        "mensagens_removidas": removidas,
        "mensagem": "Histórico apagado"
    })

# =========================================================
# LISTAR SESSÕES
# =========================================================

@app.route("/flask-api/sessoes", methods=["GET"])
def sessoes():

    with _lock:

        result = []

        for sid, s in _sessions.items():

            result.append({
                "session_id": sid,
                "total_mensagens": len(s["messages"]),
                "created_at": _iso(s["created_at"]),
                "last_activity": _iso(s["last_activity"]),
                "memoria_bytes": _memory_bytes(s)
            })

    result.sort(
        key=lambda x: x["last_activity"],
        reverse=True
    )

    return jsonify({
        "total_sessoes": len(result),
        "sessoes": result
    })

# =========================================================
# BUSCA
# =========================================================

VALID_ROLES = {"user", "assistant"}

def _check_rate_limit(ip):

    window_start = _now() - timedelta(seconds=SEARCH_RATE_WINDOW)

    with _rate_lock:

        _search_rate[ip] = [
            ts for ts in _search_rate[ip]
            if ts > window_start
        ]

        if len(_search_rate[ip]) >= SEARCH_RATE_LIMIT:
            return False

        _search_rate[ip].append(_now())

        return True

def _highlight_snippet(content, keyword):

    snippets = []

    pattern = re.compile(
        re.escape(keyword),
        re.IGNORECASE
    )

    for m in pattern.finditer(content):

        start, end = m.start(), m.end()

        ctx_start = max(0, start - CONTEXT_CHARS)
        ctx_end = min(len(content), end + CONTEXT_CHARS)

        prefix = (
            "..." if ctx_start > 0 else ""
        ) + content[ctx_start:start]

        match = f"<<{content[start:end]}>>"

        suffix = content[end:ctx_end] + (
            "..." if ctx_end < len(content) else ""
        )

        snippets.append({
            "trecho": prefix + match + suffix,
            "inicio": start,
            "fim": end
        })

    return snippets

def _collect_search_results(keyword, role_filter, session_filter):

    with _lock:

        snapshot = {
            sid: {
                "messages": list(s["messages"]),
                "last_activity": s["last_activity"]
            }
            for sid, s in _sessions.items()
            if session_filter is None or sid == session_filter
        }

    results = []

    for sid, s in snapshot.items():

        for idx, msg in enumerate(s["messages"]):

            if role_filter and msg["role"] != role_filter:
                continue

            snippets = _highlight_snippet(
                msg["content"],
                keyword
            )

            if not snippets:
                continue

            results.append({
                "session_id": sid,
                "mensagem_index": idx,
                "role": msg["role"],
                "timestamp": msg.get("timestamp", ""),
                "trechos": snippets,
                "conteudo_completo": msg["content"]
            })

    return results

@app.route("/flask-api/sessoes/buscar", methods=["GET"])
def buscar():

    ip = request.remote_addr or "unknown"

    if not _check_rate_limit(ip):
        return jsonify({
            "erro": "Rate limit atingido"
        }), 429

    keyword = request.args.get("q", "").strip()

    if not keyword:
        return jsonify({
            "erro": "Parâmetro q obrigatório"
        }), 400

    role_filter = request.args.get("role", "").strip().lower() or None

    if role_filter and role_filter not in VALID_ROLES:

        return jsonify({
            "erro": "role deve ser user ou assistant"
        }), 400

    session_filter = request.args.get("session_id", "").strip() or None

    try:

        limite = min(
            int(request.args.get("limite", SEARCH_DEFAULT_LIMIT)),
            SEARCH_MAX_LIMIT
        )

        pagina = max(
            1,
            int(request.args.get("pagina", 1))
        )

    except ValueError:

        return jsonify({
            "erro": "pagina e limite devem ser inteiros"
        }), 400

    all_results = _collect_search_results(
        keyword,
        role_filter,
        session_filter
    )

    total = len(all_results)

    offset = (pagina - 1) * limite

    page_results = all_results[offset:offset + limite]

    total_paginas = max(1, -(-total // limite))

    return jsonify({
        "q": keyword,
        "pagina": pagina,
        "limite": limite,
        "total_resultados": total,
        "total_paginas": total_paginas,
        "resultados": page_results
    })

# =========================================================
# HEALTH
# =========================================================

@app.route(HEALTHZ_PATH, methods=["GET"])
def healthz():

    now = _now()

    uptime_secs = (
        now - _server_start
    ).total_seconds()

    with _lock:

        session_count = len(_sessions)

        total_messages = sum(
            len(s["messages"])
            for s in _sessions.values()
        )

    with _stats_lock:

        req_count = _stats["request_count"]

        avg_response_ms = 0

        if req_count:

            avg_response_ms = round(
                _stats["total_response_ms"] / req_count,
                2
            )

    return jsonify({
        "status": "ok",
        "timestamp": _iso(now),
        "uptime_segundos": round(uptime_secs, 1),
        "sessoes_ativas": session_count,
        "mensagens": total_messages,
        "requests": req_count,
        "tempo_medio_ms": avg_response_ms,
        "modelos": MODELOS
    })

# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():

    return {
        "status": "online",
        "ia": "Sexta-Feira",
        "mensagem": "Sistema operacional ativo."
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
        "erro": "Rota não encontrada"
    }), 404

@app.errorhandler(405)
def method_not_allowed(e):

    return jsonify({
        "erro": "Método não permitido"
    }), 405

@app.errorhandler(500)
def server_error(e):

    logger.exception("Erro interno do servidor")

    return jsonify({
        "erro": "Erro interno do servidor"
    }), 500

# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 5000))

    debug = os.environ.get("FLASK_ENV") == "development"

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
