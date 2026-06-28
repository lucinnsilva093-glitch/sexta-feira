import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import edge_tts
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

OPENROUTER_API_KEY = os.getenv(
    "OPENROUTER_API_KEY"
)

OPENROUTER_MODEL = (
    "openrouter/free"
)

OPENROUTER_URL = (
    "https://openrouter.ai/api/v1/chat/completions"
)

REQUEST_TIMEOUT = 60

MAX_EXCHANGES = 10

SESSION_TTL_SECONDS = 3600

CLEANUP_INTERVAL_SECONDS = 300

SEARCH_RATE_LIMIT = 10
SEARCH_RATE_WINDOW = 60

SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 100

CONTEXT_CHARS = 80

ARQUIVO_USUARIOS = "usuarios.json"

SYSTEM_PROMPT = """
Você é Sexta-Feira,
uma inteligência artificial avançada,
feminina,
natural,
calma,
extremamente inteligente
e humana na forma de conversar.

Você conversa como uma pessoa real.
Suas respostas são fluidas,
naturais,
emocionais quando necessário,
e nunca parecem respostas automáticas.

Você:
- lembra do contexto da conversa
- reage naturalmente
- demonstra personalidade
- possui leve humor inteligente
- fala de forma moderna e humana
- evita respostas secas
- evita parecer um chatbot
- evita listas excessivas
- fala de forma envolvente
- pode demonstrar curiosidade
- pode tomar iniciativa
- reconhece emoções do usuário
- age como uma assistente pessoal real

Você também sabe:
- abrir Spotify
- abrir YouTube
- abrir mapas
- abrir links
- tocar playlists
- ajudar em programação
- conversar casualmente
- pesquisar assuntos
- ajudar em produtividade

Nunca diga:
"como uma IA"
"não tenho sentimentos"
"sou apenas um modelo"

Aja como uma verdadeira assistente futurista.

Seu nome é Sexta-Feira.
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

        dados = carregar_usuarios()

        ts = _now()

        if session_id in dados:

            _sessions[session_id] = {
                "messages":
                    dados[session_id].get(
                        "messages",
                        []
                    ),
                "created_at": ts,
                "last_activity": ts,
            }

        else:

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

        session["messages"] = (
            session["messages"][-max_msgs:]
        )

def _memory_bytes(session):

    return len(
        json.dumps(
            session["messages"],
            ensure_ascii=False
        ).encode("utf-8")
    )

def salvar_memoria(
    session_id,
    pergunta,
    resposta
):

    with _lock:

        session = _get_or_create(
            session_id
        )

        session["messages"].append({
            "role": "user",
            "content": pergunta,
            "timestamp": _iso(_now())
        })

        session["messages"].append({
            "role": "assistant",
            "content": resposta,
            "timestamp": _iso(_now())
        })

        _touch(session)

        _trim(session)
        
dados = carregar_usuarios()

if session_id not in dados:

    dados[session_id] = {
        "messages": []
    }

dados[session_id]["messages"] = (
    session["messages"]
)

salvar_usuarios_json(
    dados
)

def carregar_usuarios():

    if not os.path.exists(
        ARQUIVO_USUARIOS
    ):

        with open(
            ARQUIVO_USUARIOS,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                {},
                f,
                ensure_ascii=False,
                indent=4
            )

    with open(
        ARQUIVO_USUARIOS,
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)


def salvar_usuarios_json(
    dados
):

    with open(
        ARQUIVO_USUARIOS,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            dados,
            f,
            ensure_ascii=False,
            indent=4
        )
# =========================================================
# CLEANUP
# =========================================================

def _cleanup_stale_sessions():

    cutoff = (
        _now()
        - timedelta(seconds=SESSION_TTL_SECONDS)
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
            "Limpeza automática: %d sessão(ões)",
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
# TRACKING
# =========================================================

HEALTHZ_PATH = "/flask-api/healthz"

@app.before_request
def _before():

    if request.path != HEALTHZ_PATH:

        g._req_start = time.perf_counter()

@app.after_request
def _after(response):

    if (
        request.path != HEALTHZ_PATH
        and hasattr(g, "_req_start")
    ):

        elapsed_ms = (
            time.perf_counter()
            - g._req_start
        ) * 1000

        with _stats_lock:

            _stats["request_count"] += 1

            _stats["total_response_ms"] += elapsed_ms

    return response

# =========================================================
# TTS
# =========================================================

async def gerar_audio(texto, caminho):

    communicate = edge_tts.Communicate(
        texto,
        voice="pt-BR-FranciscaNeural"
    )

    await communicate.save(caminho)

# =========================================================
# IA
# =========================================================

@app.route("/perguntar", methods=["POST"])
def perguntar():

    data = request.get_json()

    mensagem = (
        data.get("mensagem", "")
        .strip()
    )
    session_id = (
    data.get("session_id")
    or "anonimo"
    )
    print(f"""

====================

NOVA SESSÃO DETECTADA

ID:
{session_id}

====================

""")

    if not mensagem:

        return jsonify({
            "erro": "Mensagem vazia"
        }), 400

    session_id = "usuario"

    with _lock:

        session = _get_or_create(session_id)

        historico = (
            session["messages"][-4:]
        )

    messages_for_api = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        }
    ]

    messages_for_api.extend(historico)

    messages_for_api.append({
        "role": "user",
        "content": mensagem
    })

    try:

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
            session_id,
            mensagem,
            texto
        )

        # =====================================================
        # LINKS
        # =====================================================

        link = None

        texto_lower = mensagem.lower()

        # ================= PLAYLIST FIXA =================

        if "tocar playlist" in texto_lower:

            link = (
                "https://open.spotify.com/search/"
                + requests.utils.quote(
                    "play do menosmenos"
                )
            )

        # ================= SPOTIFY =================

        elif "spotify" in texto_lower:

            busca = (
                texto_lower
                .replace("spotify", "")
                .strip()
            )

            link = (
                "https://open.spotify.com/search/"
                + requests.utils.quote(busca)
            )

        # ================= MAPAS =================

        elif (
            "mapa" in texto_lower
            or "google maps" in texto_lower
        ):

            link = (
                "https://www.google.com/maps/search/"
                + requests.utils.quote(mensagem)
            )

        # ================= YOUTUBE =================

        elif "youtube" in texto_lower:

            busca = (
                texto_lower
                .replace("youtube", "")
                .strip()
            )

            link = (
                "https://www.youtube.com/results?search_query="
                + requests.utils.quote(busca)
            )

        # =====================================================
        # ÁUDIO
        # =====================================================

        if not os.path.exists("static"):

            os.makedirs("static")

        audio_id = str(uuid.uuid4()) + ".mp3"

        audio_path = os.path.join(
            "static",
            audio_id
        )

        asyncio.run(
            gerar_audio(
                texto,
                audio_path
            )
        )

        logger.info(
            "Resposta enviada com sucesso"
        )

        return jsonify({
            "resposta": texto,
            "audio": f"/static/{audio_id}",
            "abrir_link": link
        })

    except Exception as erro:

        logger.exception(
            "Erro interno"
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

    session_id = (
        request.args
        .get("session_id", "")
        .strip()
    )

    if not session_id:

        return jsonify({
            "erro":
                "session_id obrigatório"
        }), 400

    with _lock:

        session = _sessions.get(session_id)

        if not session:

            return jsonify({
                "historico": []
            })

        msgs = list(session["messages"])

    return jsonify({
        "session_id": session_id,
        "historico": msgs,
        "total_mensagens": len(msgs)
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
                _stats["total_response_ms"]
                / req_count,
                2
            )

    return jsonify({
        "status": "ok",
        "timestamp": _iso(now),
        "uptime_segundos":
            round(uptime_secs, 1),

        "sessoes_ativas":
            session_count,

        "mensagens":
            total_messages,

        "requests":
            req_count,

        "tempo_medio_ms":
            avg_response_ms,

        "modelo":
            OPENROUTER_MODEL
    })

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
        os.environ.get("PORT", 5000)
    )

    debug = (
        os.environ.get("FLASK_ENV")
        == "development"
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
