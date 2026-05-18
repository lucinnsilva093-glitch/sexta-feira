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

MODELOS = [

    "google/gemini-2.0-flash-exp:free",
    "meta-llama/llama-3-8b-instruct:free",
    "deepseek/deepseek-chat:free",
    "mistralai/mistral-7b-instruct:free"

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

VALID_ROLES = {"user", "assistant"}

ROLE_LABELS = {
    "user": "USUÁRIO",
    "assistant": "SEXTA FEIRA"
}

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
# REQUEST TIMER
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
# OPENROUTER
# =========================================================

def gerar_resposta(messages_for_api):

    ultimo_erro = None

    for modelo in MODELOS:

        try:

            logger.info("Tentando modelo: %s", modelo)

            response = requests.post(

                OPENROUTER_URL,

                headers={

                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",

                },

                json={

                    "model": modelo,
                    "messages": messages_for_api

                },

                timeout=REQUEST_TIMEOUT,

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

            return texto, modelo

        except Exception as e:

            logger.error("Erro no modelo %s: %s", modelo, e)

            ultimo_erro = str(e)

            continue

    raise Exception(f"Todos os modelos falharam: {ultimo_erro}")

# =========================================================
# RATE LIMIT
# =========================================================

def _check_rate_limit(ip):

    window_start = _now() - timedelta(
        seconds=SEARCH_RATE_WINDOW
    )

    with _rate_lock:

        _search_rate[ip] = [

            ts for ts in _search_rate[ip]

            if ts > window_start

        ]

        if len(_search_rate[ip]) >= SEARCH_RATE_LIMIT:
            return False

        _search_rate[ip].append(_now())

        return True

# =========================================================
# SEARCH
# =========================================================

def _highlight_snippet(content, keyword):

    snippets = []

    pattern = re.compile(
        re.escape(keyword),
        re.IGNORECASE
    )

    for m in pattern.finditer(content):

        start, end = m.start(), m.end()

        ctx_start = max(0, start - CONTEXT_CHARS)

        ctx_end = min(
            len(content),
            end + CONTEXT_CHARS
        )

        prefix = (
            ("..." if ctx_start > 0 else "")
            + content[ctx_start:start]
        )

        match = f"<<{content[start:end]}>>"

        suffix = (
            content[end:ctx_end]
            + ("..." if ctx_end < len(content) else "")
        )

        snippets.append({

            "trecho": prefix + match + suffix,
            "inicio": start,
            "fim": end,

        })

    return snippets


def _collect_search_results(
    keyword,
    role_filter,
    session_filter
):

    with _lock:

        snapshot = {

            sid: {
                "messages": list(s["messages"]),
                "last_activity": s["last_activity"],
            }

            for sid, s in _sessions.items()

            if session_filter is None or sid == session_filter

        }

    results = []

    for sid, s in snapshot.items():

        last_act = _iso(s["last_activity"])

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
                "session_last_activity": last_act,
                "mensagem_index": idx,
                "role": msg["role"],
                "timestamp": msg.get("timestamp", ""),
                "trechos": snippets,
                "total_ocorrencias": len(snippets),
                "conteudo_completo": msg["content"],

            })

    results.sort(

        key=lambda r: (
            r["session_last_activity"],
            r["timestamp"]
        ),

        reverse=True,

    )

    return results

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

    return {"pong": True}, 200

# =========================================================
# IA PRINCIPAL
# =========================================================

@app.route("/flask-api/perguntar", methods=["POST"])
def perguntar_api():

    dados = request.get_json(silent=True)

    if not dados:

        return jsonify({
            "erro": "JSON inválido"
        }), 400

    pergunta = dados.get("pergunta", "").strip()

    if not pergunta:

        return jsonify({
            "erro": "Campo 'pergunta' obrigatório"
        }), 400

    session_id = dados.get("session_id")

    if not session_id:
        session_id = str(uuid.uuid4())

    logger.info(
        "Sessão %s | Usuário: %s",
        session_id,
        pergunta
    )

    with _lock:

        session = _get_or_create(session_id)

        session["messages"].append({

            "role": "user",
            "content": pergunta,
            "timestamp": _iso(_now())

        })

        messages_for_api = [

            {
                "role": "system",
                "content": SYSTEM_PROMPT
            }

        ] + _history_for_api(session)

    try:

        texto, modelo = gerar_resposta(messages_for_api)

    except Exception as erro:

        with _lock:

            _sessions[session_id]["messages"].pop()

        logger.error("Erro IA: %s", erro)

        return jsonify({

            "erro": str(erro)

        }), 500

    with _lock:

        session = _sessions[session_id]

        session["messages"].append({

            "role": "assistant",
            "content": texto,
            "timestamp": _iso(_now())

        })

        _trim(session)

        _touch(session)

        total = len(session["messages"])

    return jsonify({

        "resposta": texto,
        "modelo": modelo,
        "session_id": session_id,
        "total_mensagens": total

    })

# =========================================================
# HISTÓRICO
# =========================================================

@app.route("/flask-api/historico", methods=["GET"])
def historico():

    session_id = request.args.get(
        "session_id",
        ""
    ).strip()

    if not session_id:

        return jsonify({
            "erro": "session_id obrigatório"
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

    session_id = dados.get(
        "session_id",
        ""
    ).strip()

    if not session_id:

        return jsonify({
            "erro": "session_id obrigatório"
        }), 400

    with _lock:

        session = _sessions.pop(
            session_id,
            None
        )

        count = len(session["messages"]) if session else 0

    return jsonify({

        "session_id": session_id,
        "mensagens_removidas": count,
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

            msgs = s["messages"]

            result.append({

                "session_id": sid,
                "total_mensagens": len(msgs),
                "created_at": _iso(s["created_at"]),
                "last_activity": _iso(s["last_activity"]),

            })

    return jsonify({

        "total_sessoes": len(result),
        "sessoes": result

    })

# =========================================================
# BUSCAR
# =========================================================

@app.route("/flask-api/sessoes/buscar", methods=["GET"])
def buscar():

    ip = request.headers.get(
        "X-Forwarded-For",
        request.remote_addr or "unknown"
    ).split(",")[0].strip()

    if not _check_rate_limit(ip):

        return jsonify({

            "erro": "Muitas buscas"

        }), 429

    keyword = request.args.get("q", "").strip()

    if not keyword:

        return jsonify({
            "erro": "q obrigatório"
        }), 400

    role_filter = request.args.get(
        "role",
        ""
    ).strip().lower() or None

    session_filter = request.args.get(
        "session_id",
        ""
    ).strip() or None

    all_results = _collect_search_results(

        keyword,
        role_filter,
        session_filter

    )

    return jsonify({

        "q": keyword,
        "total_resultados": len(all_results),
        "resultados": all_results

    })

# =========================================================
# HEALTHZ
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

        total_ms = _stats["total_response_ms"]

        search_count = _stats["search_count"]

    avg_response_ms = (

        round(total_ms / req_count, 2)

        if req_count else 0

    )

    return jsonify({

        "status": "ok",

        "uptime": {

            "segundos": uptime_secs,
            "inicio_utc": _iso(_server_start)

        },

        "sessoes": {

            "ativas": session_count,
            "mensagens": total_messages

        },

        "requisicoes": {

            "total": req_count,
            "buscas": search_count,
            "tempo_medio_ms": avg_response_ms

        },

        "modelos": MODELOS

    })

# =========================================================
# EXPORTAR
# =========================================================

@app.route("/flask-api/sessoes/exportar", methods=["POST"])
def exportar_sessao():

    dados = request.get_json(silent=True) or {}

    session_id = dados.get(
        "session_id",
        ""
    ).strip()

    if not session_id:

        return jsonify({
            "erro": "session_id obrigatório"
        }), 400

    with _lock:

        session = _sessions.get(session_id)

        if not session:

            return jsonify({
                "erro": "Sessão não encontrada"
            }), 404

        payload = {

            "session_id": session_id,
            "mensagens": session["messages"]

        }

    body = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2
    )

    resp = make_response(body.encode("utf-8"))

    resp.headers["Content-Type"] = (
        "application/json; charset=utf-8"
    )

    return resp

# =========================================================
# IMPORTAR
# =========================================================

@app.route("/flask-api/sessoes/importar", methods=["POST"])
def importar_sessao():

    dados = request.get_json(silent=True)

    if not dados:

        return jsonify({
            "erro": "JSON inválido"
        }), 400

    session_id = dados.get(
        "session_id",
        str(uuid.uuid4())
    )

    mensagens = dados.get("mensagens")

    if not isinstance(mensagens, list):

        return jsonify({
            "erro": "mensagens deve ser lista"
        }), 400

    with _lock:

        _sessions[session_id] = {

            "messages": mensagens,
            "created_at": _now(),
            "last_activity": _now()

        }

    return jsonify({

        "mensagem": "Sessão importada",
        "session_id": session_id

    }), 201

# =========================================================
# ERROR HANDLERS
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

    logger.exception("Erro interno")

    return jsonify({
        "erro": "Erro interno"
    }), 500

# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", 5000)
    )

    debug = (
        os.environ.get("FLASK_ENV")
        == "development"
    )

    if not OPENROUTER_API_KEY:

        logger.warning(
            "OPENROUTER_API_KEY não definida"
        )

    logger.info(
        "Servidor iniciado na porta %d",
        port
    )

    app.run(

        host="0.0.0.0",
        port=port,
        debug=debug

    )
