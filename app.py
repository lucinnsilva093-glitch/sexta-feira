import io
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "nvidia/nemotron-nano-9b-v2:free"
REQUEST_TIMEOUT = 30
MAX_EXCHANGES = 10
SESSION_TTL_SECONDS = 3600       # expire sessions inactive for 1 h
CLEANUP_INTERVAL_SECONDS = 300   # run cleanup every 5 min

SEARCH_RATE_LIMIT = 10           # max search requests per window per IP
SEARCH_RATE_WINDOW = 60          # sliding window in seconds
SEARCH_DEFAULT_LIMIT = 20        # default page size
SEARCH_MAX_LIMIT = 100           # hard cap on page size
CONTEXT_CHARS = 80               # characters of surrounding context per match

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

# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------
# sessions: session_id -> {
#   "messages":      list[{"role", "content", "timestamp"}],
#   "created_at":    datetime (UTC),
#   "last_activity": datetime (UTC),
# }
_sessions: dict[str, dict] = {}
_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Search rate-limiter state
# ---------------------------------------------------------------------------
# Sliding-window per client IP: maps IP -> list of request datetimes
_search_rate: dict[str, list[datetime]] = defaultdict(list)
_rate_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Server stats
# ---------------------------------------------------------------------------
_server_start: datetime = datetime.now(timezone.utc)
_stats: dict = {
    "request_count": 0,
    "total_response_ms": 0.0,
    "search_count": 0,
}
_stats_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _get_or_create(session_id: str) -> dict:
    """Return the session dict, creating it if missing. Caller must hold _lock."""
    if session_id not in _sessions:
        ts = _now()
        _sessions[session_id] = {
            "messages": [],
            "created_at": ts,
            "last_activity": ts,
        }
    return _sessions[session_id]


def _touch(session: dict) -> None:
    """Update last_activity to now. Caller must hold _lock."""
    session["last_activity"] = _now()


def _trim(session: dict) -> None:
    """Keep only the last MAX_EXCHANGES user+assistant pairs."""
    max_msgs = MAX_EXCHANGES * 2
    if len(session["messages"]) > max_msgs:
        session["messages"] = session["messages"][-max_msgs:]


def _memory_bytes(session: dict) -> int:
    """Rough estimate: JSON-encode the messages list and count bytes."""
    return len(json.dumps(session["messages"], ensure_ascii=False).encode("utf-8"))


def _history_for_api(session: dict) -> list[dict]:
    return [{"role": m["role"], "content": m["content"]} for m in session["messages"]]


# ---------------------------------------------------------------------------
# Background cleanup
# ---------------------------------------------------------------------------

def _cleanup_stale_sessions() -> None:
    cutoff = _now() - timedelta(seconds=SESSION_TTL_SECONDS)
    with _lock:
        stale = [sid for sid, s in _sessions.items() if s["last_activity"] < cutoff]
        for sid in stale:
            del _sessions[sid]
    if stale:
        logger.info("Limpeza automática: %d sessão(ões) expirada(s) removida(s)", len(stale))


def _schedule_cleanup() -> None:
    _cleanup_stale_sessions()
    t = threading.Timer(CLEANUP_INTERVAL_SECONDS, _schedule_cleanup)
    t.daemon = True
    t.start()


# Start background cleanup on first import (not on Werkzeug reloader child processes)
if not os.environ.get("WERKZEUG_RUN_MAIN"):
    _schedule_cleanup()


# ---------------------------------------------------------------------------
# Request instrumentation
# ---------------------------------------------------------------------------

HEALTHZ_PATH = "/flask-api/healthz"

@app.before_request
def _before():
    if request.path != HEALTHZ_PATH:
        g._req_start = time.perf_counter()


@app.after_request
def _after(response):
    if request.path != HEALTHZ_PATH and hasattr(g, "_req_start"):
        elapsed_ms = (time.perf_counter() - g._req_start) * 1000
        with _stats_lock:
            _stats["request_count"] += 1
            _stats["total_response_ms"] += elapsed_ms
    return response


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/flask-api/perguntar", methods=["POST"])
def perguntar():
    if not OPENROUTER_API_KEY:
        logger.error("OPENROUTER_API_KEY não está configurada")
        return jsonify({"erro": "Chave da API não configurada no servidor"}), 500

    dados = request.get_json(silent=True)
    if not dados:
        return jsonify({"erro": "Corpo da requisição deve ser JSON válido"}), 400

    pergunta = dados.get("pergunta", "").strip()
    if not pergunta:
        return jsonify({"erro": "Campo 'pergunta' é obrigatório e não pode estar vazio"}), 400

    session_id = dados.get("session_id") or str(uuid.uuid4())
    logger.info("Sessão %s | USUÁRIO: %s", session_id, pergunta)

    with _lock:
        session = _get_or_create(session_id)
        session["messages"].append({
            "role": "user",
            "content": pergunta,
            "timestamp": _iso(_now()),
        })
        messages_for_api = [{"role": "system", "content": SYSTEM_PROMPT}] + _history_for_api(session)

    try:
        response = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": OPENROUTER_MODEL, "messages": messages_for_api},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        with _lock:
            _sessions[session_id]["messages"].pop()
        logger.error("Sessão %s: timeout após %ds", session_id, REQUEST_TIMEOUT)
        return jsonify({"erro": "A IA demorou demais para responder. Tente novamente."}), 504
    except requests.exceptions.ConnectionError as exc:
        with _lock:
            _sessions[session_id]["messages"].pop()
        logger.error("Sessão %s: erro de conexão — %s", session_id, exc)
        return jsonify({"erro": "Não foi possível conectar à IA. Verifique sua conexão."}), 502
    except requests.exceptions.RequestException as exc:
        with _lock:
            _sessions[session_id]["messages"].pop()
        logger.error("Sessão %s: erro inesperado — %s", session_id, exc)
        return jsonify({"erro": "Erro ao comunicar com a IA"}), 502

    if not response.ok:
        with _lock:
            _sessions[session_id]["messages"].pop()
        logger.error(
            "Sessão %s: OpenRouter retornou status %d — %s",
            session_id, response.status_code, response.text[:200],
        )
        return jsonify({"erro": "A IA retornou um erro", "status": response.status_code}), 502

    try:
        resposta = response.json()
        texto = resposta["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        with _lock:
            _sessions[session_id]["messages"].pop()
        logger.error(
            "Sessão %s: resposta inesperada — %s | corpo: %s",
            session_id, exc, response.text[:200],
        )
        return jsonify({"erro": "Resposta da IA em formato inesperado"}), 502

    with _lock:
        session = _sessions[session_id]
        session["messages"].append({
            "role": "assistant",
            "content": texto,
            "timestamp": _iso(_now()),
        })
        _trim(session)
        _touch(session)
        total = len(session["messages"])

    logger.info("Sessão %s | SEXTA FEIRA: %s", session_id, texto)
    return jsonify({
        "resposta": texto,
        "session_id": session_id,
        "total_mensagens": total,
    })


@app.route("/flask-api/historico", methods=["GET"])
def historico():
    session_id = request.args.get("session_id", "").strip()
    if not session_id:
        return jsonify({"erro": "Parâmetro 'session_id' é obrigatório"}), 400

    with _lock:
        session = _sessions.get(session_id)
        if not session:
            return jsonify({"session_id": session_id, "historico": [], "total_mensagens": 0})
        msgs = list(session["messages"])
        total = len(msgs)

    return jsonify({
        "session_id": session_id,
        "historico": msgs,
        "total_mensagens": total,
    })


@app.route("/flask-api/limpar-historico", methods=["POST"])
def limpar_historico():
    dados = request.get_json(silent=True) or {}
    session_id = dados.get("session_id", "").strip()
    if not session_id:
        return jsonify({"erro": "Campo 'session_id' é obrigatório"}), 400

    with _lock:
        session = _sessions.pop(session_id, None)
        count = len(session["messages"]) if session else 0

    logger.info("Sessão %s: histórico limpo (%d mensagens removidas)", session_id, count)
    return jsonify({
        "session_id": session_id,
        "mensagens_removidas": count,
        "mensagem": "Histórico limpo com sucesso",
    })


@app.route("/flask-api/sessoes", methods=["GET"])
def sessoes():
    with _lock:
        snapshot = {
            sid: {
                "messages": list(s["messages"]),
                "created_at": s["created_at"],
                "last_activity": s["last_activity"],
            }
            for sid, s in _sessions.items()
        }

    result = []
    for sid, s in snapshot.items():
        msgs = s["messages"]
        result.append({
            "session_id": sid,
            "total_mensagens": len(msgs),
            "created_at": _iso(s["created_at"]),
            "last_activity": _iso(s["last_activity"]),
            "memoria_bytes": len(
                json.dumps(msgs, ensure_ascii=False).encode("utf-8")
            ),
        })

    result.sort(key=lambda x: x["last_activity"], reverse=True)

    return jsonify({
        "total_sessoes": len(result),
        "sessoes": result,
    })


# ---------------------------------------------------------------------------
# Export / Import helpers
# ---------------------------------------------------------------------------

VALID_ROLES = {"user", "assistant"}
ROLE_LABELS = {"user": "USUÁRIO", "assistant": "SEXTA FEIRA"}


def _build_export_payload(session_id: str, session: dict) -> dict:
    return {
        "session_id": session_id,
        "exported_at": _iso(_now()),
        "created_at": _iso(session["created_at"]),
        "last_activity": _iso(session["last_activity"]),
        "total_mensagens": len(session["messages"]),
        "mensagens": session["messages"],
    }


def _build_txt(payload: dict, pretty: bool) -> str:
    lines: list[str] = []
    sep = "=" * 60

    lines.append(sep)
    lines.append(f"SESSÃO: {payload['session_id']}")
    lines.append(f"Exportado em:      {payload['exported_at']}")
    lines.append(f"Criada em:         {payload['created_at']}")
    lines.append(f"Última atividade:  {payload['last_activity']}")
    lines.append(f"Total mensagens:   {payload['total_mensagens']}")
    lines.append(sep)

    for i, msg in enumerate(payload["mensagens"], 1):
        role_label = ROLE_LABELS.get(msg["role"], msg["role"].upper())
        ts = msg.get("timestamp", "")
        if pretty:
            lines.append("")
            lines.append(f"--- Mensagem {i} ---")
            lines.append(f"[{ts}] {role_label}:")
            lines.append(msg["content"])
        else:
            lines.append(f"[{ts}] {role_label}: {msg['content']}")

    lines.append("")
    return "\n".join(lines)


def _validate_import(dados: dict) -> tuple[list[dict] | None, str]:
    """
    Validate the import payload.
    Returns (messages, error_string). error_string is empty on success.
    """
    mensagens = dados.get("mensagens")
    if not isinstance(mensagens, list):
        return None, "Campo 'mensagens' deve ser uma lista"
    if not mensagens:
        return None, "A lista 'mensagens' não pode estar vazia"

    cleaned: list[dict] = []
    for idx, msg in enumerate(mensagens):
        if not isinstance(msg, dict):
            return None, f"Mensagem {idx} não é um objeto JSON válido"
        role = msg.get("role", "")
        if role not in VALID_ROLES:
            return None, (
                f"Mensagem {idx}: 'role' inválido '{role}'. "
                f"Valores aceitos: {sorted(VALID_ROLES)}"
            )
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            return None, f"Mensagem {idx}: 'content' deve ser uma string não vazia"

        ts = msg.get("timestamp")
        if not isinstance(ts, str) or not ts:
            ts = _iso(_now())

        cleaned.append({"role": role, "content": content.strip(), "timestamp": ts})

    return cleaned, ""


# ---------------------------------------------------------------------------
# Export / Import routes
# ---------------------------------------------------------------------------

@app.route("/flask-api/sessoes/exportar", methods=["POST"])
def exportar_sessao():
    dados = request.get_json(silent=True) or {}

    session_id = dados.get("session_id", "").strip()
    if not session_id:
        return jsonify({"erro": "Campo 'session_id' é obrigatório"}), 400

    formato = dados.get("formato", "json").lower()
    if formato not in ("json", "txt"):
        return jsonify({"erro": "Campo 'formato' deve ser 'json' ou 'txt'"}), 400

    pretty = bool(dados.get("pretty", True))

    with _lock:
        session = _sessions.get(session_id)
        if not session:
            return jsonify({"erro": f"Sessão '{session_id}' não encontrada"}), 404
        payload = _build_export_payload(session_id, {
            "messages": list(session["messages"]),
            "created_at": session["created_at"],
            "last_activity": session["last_activity"],
        })

    filename_base = f"sessao_{session_id}"

    if formato == "json":
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        resp = make_response(body.encode("utf-8"))
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="{filename_base}.json"'
        )
    else:
        body = _build_txt(payload, pretty=pretty)
        resp = make_response(body.encode("utf-8"))
        resp.headers["Content-Type"] = "text/plain; charset=utf-8"
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="{filename_base}.txt"'
        )

    logger.info(
        "Sessão %s: exportada como %s (%d bytes)", session_id, formato, len(body)
    )
    return resp


@app.route("/flask-api/sessoes/importar", methods=["POST"])
def importar_sessao():
    dados = request.get_json(silent=True)
    if not dados:
        return jsonify({"erro": "Corpo da requisição deve ser JSON válido"}), 400

    session_id = dados.get("session_id", "").strip() or str(uuid.uuid4())

    mensagens, err = _validate_import(dados)
    if err:
        return jsonify({"erro": err}), 400

    ts_now = _now()

    created_at_raw = dados.get("created_at", "")
    try:
        created_at = datetime.fromisoformat(created_at_raw) if created_at_raw else ts_now
    except ValueError:
        created_at = ts_now

    last_activity_raw = dados.get("last_activity", "")
    try:
        last_activity = datetime.fromisoformat(last_activity_raw) if last_activity_raw else ts_now
    except ValueError:
        last_activity = ts_now

    with _lock:
        overwriting = session_id in _sessions
        _sessions[session_id] = {
            "messages": mensagens,
            "created_at": created_at,
            "last_activity": last_activity,
        }

    logger.info(
        "Sessão %s: importada (%d mensagens%s)",
        session_id,
        len(mensagens),
        ", sobrescrevendo sessão existente" if overwriting else "",
    )

    return jsonify({
        "session_id": session_id,
        "total_mensagens": len(mensagens),
        "sobrescrita": overwriting,
        "mensagem": "Sessão importada com sucesso",
    }), 201


# ---------------------------------------------------------------------------
# Search helpers
# ---------------------------------------------------------------------------

def _check_rate_limit(ip: str) -> bool:
    """Sliding-window rate limiter. Returns True if request is allowed."""
    window_start = _now() - timedelta(seconds=SEARCH_RATE_WINDOW)
    with _rate_lock:
        _search_rate[ip] = [ts for ts in _search_rate[ip] if ts > window_start]
        if len(_search_rate[ip]) >= SEARCH_RATE_LIMIT:
            return False
        _search_rate[ip].append(_now())
        return True


def _highlight_snippet(content: str, keyword: str) -> list[dict]:
    """
    Find every case-insensitive occurrence of keyword in content.
    Returns a list of snippet dicts, one per match:
      {
        "trecho":  "...before <<keyword>> after...",
        "inicio":  char offset of match start,
        "fim":     char offset of match end,
      }
    """
    snippets = []
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    for m in pattern.finditer(content):
        start, end = m.start(), m.end()
        ctx_start = max(0, start - CONTEXT_CHARS)
        ctx_end   = min(len(content), end + CONTEXT_CHARS)

        prefix = ("..." if ctx_start > 0 else "") + content[ctx_start:start]
        match  = f"<<{content[start:end]}>>"
        suffix = content[end:ctx_end] + ("..." if ctx_end < len(content) else "")

        snippets.append({
            "trecho": prefix + match + suffix,
            "inicio": start,
            "fim": end,
        })
    return snippets


def _collect_search_results(
    keyword: str,
    role_filter: str | None,
    session_filter: str | None,
) -> list[dict]:
    """
    Scan the session store and return one result dict per matching message.
    Results are sorted newest-session-first, then by message timestamp descending.
    """
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
            snippets = _highlight_snippet(msg["content"], keyword)
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
        key=lambda r: (r["session_last_activity"], r["timestamp"]),
        reverse=True,
    )
    return results


# ---------------------------------------------------------------------------
# Search route
# ---------------------------------------------------------------------------

@app.route("/flask-api/sessoes/buscar", methods=["GET"])
def buscar():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    if not _check_rate_limit(ip):
        logger.warning("Busca: rate limit atingido para IP %s", ip)
        return jsonify({
            "erro": f"Limite de {SEARCH_RATE_LIMIT} buscas por {SEARCH_RATE_WINDOW}s atingido. Tente novamente em breve.",
        }), 429

    keyword = request.args.get("q", "").strip()
    if not keyword:
        return jsonify({"erro": "Parâmetro 'q' (palavra-chave) é obrigatório"}), 400

    role_filter = request.args.get("role", "").strip().lower() or None
    if role_filter and role_filter not in VALID_ROLES:
        return jsonify({"erro": f"'role' deve ser 'user' ou 'assistant'"}), 400

    session_filter = request.args.get("session_id", "").strip() or None

    try:
        limite = min(int(request.args.get("limite", SEARCH_DEFAULT_LIMIT)), SEARCH_MAX_LIMIT)
        pagina = max(1, int(request.args.get("pagina", 1)))
    except ValueError:
        return jsonify({"erro": "'limite' e 'pagina' devem ser inteiros positivos"}), 400

    with _stats_lock:
        _stats["search_count"] += 1

    t_start = time.perf_counter()
    all_results = _collect_search_results(keyword, role_filter, session_filter)
    elapsed_ms = round((time.perf_counter() - t_start) * 1000, 2)

    total = len(all_results)
    offset = (pagina - 1) * limite
    page_results = all_results[offset : offset + limite]
    total_paginas = max(1, -(-total // limite))  # ceiling division

    logger.info(
        "Busca: q=%r role=%s session=%s → %d resultado(s) em %sms (pág %d/%d, IP %s)",
        keyword, role_filter or "*", session_filter or "*",
        total, elapsed_ms, pagina, total_paginas, ip,
    )

    return jsonify({
        "q": keyword,
        "filtros": {
            "role": role_filter,
            "session_id": session_filter,
        },
        "total_resultados": total,
        "total_paginas": total_paginas,
        "pagina": pagina,
        "limite": limite,
        "tempo_ms": elapsed_ms,
        "resultados": page_results,
    })


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def _check_openrouter() -> dict:
    """Attempt a lightweight connectivity probe to OpenRouter. Returns a status dict."""
    if not OPENROUTER_API_KEY:
        return {"reachable": False, "reason": "API key not configured", "latency_ms": None}
    try:
        t0 = time.perf_counter()
        r = requests.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            timeout=4,
        )
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        reachable = r.status_code < 500
        return {
            "reachable": reachable,
            "http_status": r.status_code,
            "latency_ms": latency_ms,
            "reason": None if reachable else f"HTTP {r.status_code}",
        }
    except requests.exceptions.Timeout:
        return {"reachable": False, "reason": "timeout (4s)", "latency_ms": None}
    except requests.exceptions.ConnectionError as exc:
        return {"reachable": False, "reason": f"connection error: {exc}", "latency_ms": None}
    except Exception as exc:
        return {"reachable": False, "reason": str(exc), "latency_ms": None}


def _run_self_tests() -> list[dict]:
    """Run lightweight diagnostics. Returns a list of test result dicts."""
    tests = []

    # 1. API key present
    tests.append({
        "nome": "api_key_configurada",
        "passou": bool(OPENROUTER_API_KEY),
        "detalhe": "OK" if OPENROUTER_API_KEY else "OPENROUTER_API_KEY não definida",
    })

    # 2. Session store read/write
    try:
        _probe_id = "__healthz_probe__"
        with _lock:
            _sessions[_probe_id] = {
                "messages": [],
                "created_at": _now(),
                "last_activity": _now(),
            }
            found = _probe_id in _sessions
            del _sessions[_probe_id]
        tests.append({"nome": "session_store", "passou": found, "detalhe": "OK" if found else "falha na escrita"})
    except Exception as exc:
        tests.append({"nome": "session_store", "passou": False, "detalhe": str(exc)})

    # 3. Highlight / search logic
    try:
        snips = _highlight_snippet("Olá mundo cruel", "mundo")
        ok = len(snips) == 1 and "<<mundo>>" in snips[0]["trecho"]
        tests.append({"nome": "busca_highlight", "passou": ok, "detalhe": "OK" if ok else "resultado inesperado"})
    except Exception as exc:
        tests.append({"nome": "busca_highlight", "passou": False, "detalhe": str(exc)})

    # 4. Rate-limiter in-memory state readable
    try:
        with _rate_lock:
            _ = len(_search_rate)
        tests.append({"nome": "rate_limiter", "passou": True, "detalhe": "OK"})
    except Exception as exc:
        tests.append({"nome": "rate_limiter", "passou": False, "detalhe": str(exc)})

    return tests


def _compute_status(api_key_ok: bool, or_reachable: bool, mem_bytes: int, tests: list[dict]) -> str:
    failed_tests = sum(1 for t in tests if not t["passou"])
    if failed_tests >= 2 or not api_key_ok:
        return "critical"
    if not or_reachable or mem_bytes > 5 * 1024 * 1024 or failed_tests == 1:
        return "warning"
    return "ok"


@app.route(HEALTHZ_PATH, methods=["GET"])
def healthz():
    now = _now()

    # --- uptime ---
    uptime_secs = (now - _server_start).total_seconds()
    uptime_str = (
        f"{int(uptime_secs // 3600)}h "
        f"{int((uptime_secs % 3600) // 60)}m "
        f"{int(uptime_secs % 60)}s"
    )

    # --- session stats (snapshot under lock) ---
    with _lock:
        session_count = len(_sessions)
        all_msgs = [m for s in _sessions.values() for m in s["messages"]]
        mem_bytes = len(json.dumps(
            [{k: v for k, v in s.items() if k != "created_at" and k != "last_activity"}
             for s in _sessions.values()],
            ensure_ascii=False, default=str,
        ).encode("utf-8"))

    total_messages = len(all_msgs)

    # --- request stats ---
    with _stats_lock:
        req_count = _stats["request_count"]
        total_ms  = _stats["total_response_ms"]
        search_count = _stats["search_count"]

    avg_response_ms = round(total_ms / req_count, 2) if req_count else 0.0

    # --- rate-limiter state ---
    window_start = now - timedelta(seconds=SEARCH_RATE_WINDOW)
    with _rate_lock:
        active_ips = {ip: ts for ip, ts in _search_rate.items() if any(t > window_start for t in ts)}
        throttled_ips = sum(
            1 for ts_list in active_ips.values()
            if len([t for t in ts_list if t > window_start]) >= SEARCH_RATE_LIMIT
        )

    # --- connectivity ---
    or_status = _check_openrouter()

    # --- self-tests ---
    self_tests = _run_self_tests()
    api_key_ok = bool(OPENROUTER_API_KEY)

    # --- overall status ---
    status = _compute_status(api_key_ok, or_status["reachable"], mem_bytes, self_tests)
    http_code = 200 if status == "ok" else (207 if status == "warning" else 503)

    logger.info("Healthz: status=%s uptime=%s sessões=%d requisições=%d", status, uptime_str, session_count, req_count)

    return jsonify({
        "status": status,
        "timestamp_utc": _iso(now),
        "uptime": {
            "legivel": uptime_str,
            "segundos": round(uptime_secs, 1),
            "inicio_utc": _iso(_server_start),
        },
        "sessoes": {
            "ativas": session_count,
            "total_mensagens": total_messages,
            "memoria_estimada_bytes": mem_bytes,
            "memoria_estimada_kb": round(mem_bytes / 1024, 2),
            "ttl_segundos": SESSION_TTL_SECONDS,
            "max_trocas_por_sessao": MAX_EXCHANGES,
        },
        "requisicoes": {
            "total_servidas": req_count,
            "buscas_realizadas": search_count,
            "tempo_medio_ms": avg_response_ms,
        },
        "rate_limiter": {
            "janela_segundos": SEARCH_RATE_WINDOW,
            "limite_por_janela": SEARCH_RATE_LIMIT,
            "ips_ativos": len(active_ips),
            "ips_bloqueados": throttled_ips,
        },
        "openrouter": or_status,
        "diagnosticos": self_tests,
    }), http_code


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

def _parse_ts_for_sort(ts: str) -> datetime:
    """Parse an ISO timestamp for sorting; falls back to epoch on failure."""
    try:
        return datetime.fromisoformat(ts) if ts else datetime.min.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _merge_messages(msgs_a: list[dict], msgs_b: list[dict]) -> tuple[list[dict], int, int]:
    """
    Combine, sort chronologically, and deduplicate two message lists.
    Deduplication key: (role, content) — the earlier timestamp wins.
    Returns (merged_messages, total_before_dedup, duplicates_removed).
    """
    combined = sorted(msgs_a + msgs_b, key=lambda m: _parse_ts_for_sort(m.get("timestamp", "")))
    total_before = len(combined)

    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for msg in combined:
        key = (msg["role"], msg["content"])
        if key not in seen:
            seen.add(key)
            deduped.append(msg)

    return deduped, total_before, total_before - len(deduped)


def _session_deep_copy(session: dict) -> dict:
    return {
        "messages": [dict(m) for m in session["messages"]],
        "created_at": session["created_at"],
        "last_activity": session["last_activity"],
    }


# ---------------------------------------------------------------------------
# Merge route
# ---------------------------------------------------------------------------

ESTRATEGIAS_VALIDAS = {"novo", "sobrescrever_a", "sobrescrever_b"}


@app.route("/flask-api/sessoes/mesclar", methods=["POST"])
def mesclar_sessoes():
    dados = request.get_json(silent=True)
    if not dados:
        return jsonify({"erro": "Corpo da requisição deve ser JSON válido"}), 400

    sid_a = dados.get("session_id_a", "").strip()
    sid_b = dados.get("session_id_b", "").strip()
    if not sid_a or not sid_b:
        return jsonify({"erro": "Campos 'session_id_a' e 'session_id_b' são obrigatórios"}), 400
    if sid_a == sid_b:
        return jsonify({"erro": "'session_id_a' e 'session_id_b' devem ser diferentes"}), 400

    estrategia = dados.get("estrategia", "novo").strip()
    if estrategia not in ESTRATEGIAS_VALIDAS:
        return jsonify({
            "erro": f"'estrategia' deve ser um de: {sorted(ESTRATEGIAS_VALIDAS)}"
        }), 400

    session_destino_input = dados.get("session_destino", "").strip()
    backup_antes = dados.get("backup_antes_de_mesclar", True)

    # Determine destination session ID
    if estrategia == "sobrescrever_a":
        destino_id = sid_a
    elif estrategia == "sobrescrever_b":
        destino_id = sid_b
    else:
        destino_id = session_destino_input or str(uuid.uuid4())

    # --- Step 1: read both sessions (snapshot under lock) ---
    with _lock:
        session_a = _sessions.get(sid_a)
        session_b = _sessions.get(sid_b)

        if not session_a:
            return jsonify({"erro": f"Sessão '{sid_a}' não encontrada"}), 404
        if not session_b:
            return jsonify({"erro": f"Sessão '{sid_b}' não encontrada"}), 404

        snap_a = _session_deep_copy(session_a)
        snap_b = _session_deep_copy(session_b)

    # --- Step 2: compute merge (pure, outside lock) ---
    try:
        merged_msgs, total_before, dupes_removed = _merge_messages(
            snap_a["messages"], snap_b["messages"]
        )
    except Exception as exc:
        logger.error("Falha ao calcular mesclagem de %s + %s: %s", sid_a, sid_b, exc)
        return jsonify({"erro": f"Falha ao calcular mesclagem: {exc}"}), 500

    earliest_created = min(snap_a["created_at"], snap_b["created_at"])
    latest_activity  = max(snap_a["last_activity"], snap_b["last_activity"])

    merged_session: dict = {
        "messages": merged_msgs,
        "created_at": earliest_created,
        "last_activity": _now(),
    }

    mem_bytes = len(json.dumps(
        [m for m in merged_msgs], ensure_ascii=False
    ).encode("utf-8"))

    # --- Step 3: commit under lock (with optional backup + rollback) ---
    backup_ids: dict[str, str] = {}
    pre_merge_snapshots: dict[str, dict] = {}

    with _lock:
        # Re-validate: sessions might have changed between Step 1 and now
        if sid_a not in _sessions:
            return jsonify({"erro": f"Sessão '{sid_a}' foi removida durante a mesclagem"}), 409
        if sid_b not in _sessions:
            return jsonify({"erro": f"Sessão '{sid_b}' foi removida durante a mesclagem"}), 409

        # Create backups
        if backup_antes:
            bid_a = f"__backup__{sid_a}__{str(uuid.uuid4())[:8]}"
            bid_b = f"__backup__{sid_b}__{str(uuid.uuid4())[:8]}"
            _sessions[bid_a] = _session_deep_copy(_sessions[sid_a])
            _sessions[bid_b] = _session_deep_copy(_sessions[sid_b])
            backup_ids = {"session_a_backup": bid_a, "session_b_backup": bid_b}
            # Save originals for rollback
            pre_merge_snapshots[sid_a] = _session_deep_copy(_sessions[sid_a])
            pre_merge_snapshots[sid_b] = _session_deep_copy(_sessions[sid_b])
            logger.info("Mesclar: backups criados — %s, %s", bid_a, bid_b)

        try:
            _sessions[destino_id] = merged_session
        except Exception as exc:
            # Rollback backups written to _sessions
            for bid in backup_ids.values():
                _sessions.pop(bid, None)
            # Restore originals if they were overwritten
            for sid, snap in pre_merge_snapshots.items():
                if sid == destino_id:
                    _sessions[sid] = snap
            logger.error("Mesclar: rollback executado após falha: %s", exc)
            return jsonify({"erro": f"Mesclagem falhou e foi revertida: {exc}"}), 500

    logger.info(
        "Mesclar: %s + %s → %s | %d msgs (%d dupl. removidas) | backup=%s",
        sid_a, sid_b, destino_id, len(merged_msgs), dupes_removed, backup_antes,
    )

    return jsonify({
        "session_destino": destino_id,
        "estrategia": estrategia,
        "estatisticas": {
            "mensagens_sessao_a": len(snap_a["messages"]),
            "mensagens_sessao_b": len(snap_b["messages"]),
            "total_antes_dedup": total_before,
            "duplicatas_removidas": dupes_removed,
            "total_mescladas": len(merged_msgs),
            "memoria_resultante_bytes": mem_bytes,
            "memoria_resultante_kb": round(mem_bytes / 1024, 2),
        },
        "backups": backup_ids if backup_antes else {},
        "mensagem": "Sessões mescladas com sucesso",
    }), 201


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return jsonify({"erro": "Rota não encontrada"}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"erro": "Método não permitido"}), 405


@app.errorhandler(500)
def server_error(e):
    logger.exception("Erro interno do servidor")
    return jsonify({"erro": "Erro interno do servidor"}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") == "development"
    if not OPENROUTER_API_KEY:
        logger.warning(
            "OPENROUTER_API_KEY não está definida — o endpoint /perguntar não funcionará"
        )
    logger.info(
        "Iniciando servidor na porta %d (max %d trocas/sessão, TTL %ds)",
        port, MAX_EXCHANGES, SESSION_TTL_SECONDS,
    )
    if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
