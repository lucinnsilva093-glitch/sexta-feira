```python
import os
import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL")


def conectar():

    return psycopg2.connect(
        DATABASE_URL
    )


def criar_tabelas():

    conn = conectar()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id SERIAL PRIMARY KEY,
        session_id TEXT UNIQUE,
        nome TEXT,
        plano TEXT DEFAULT 'free'
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS mensagens (
        id SERIAL PRIMARY KEY,
        session_id TEXT,
        role TEXT,
        content TEXT,
        timestamp TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS memoria_importante (
        id SERIAL PRIMARY KEY,
        session_id TEXT,
        chave TEXT,
        valor TEXT,
        UNIQUE(session_id, chave)
    )
    """)

    conn.commit()
    conn.close()


# ==================================================
# MENSAGENS
# ==================================================

def salvar_mensagem(
    session_id,
    role,
    content,
    timestamp
):

    conn = conectar()
    cursor = conn.cursor()

    cursor.execute(
    """
    INSERT INTO mensagens
    (
        session_id,
        role,
        content,
        timestamp
    )
    VALUES (%s, %s, %s, %s)
    """,
    (
        session_id,
        role,
        content,
        timestamp
    )
    )

    conn.commit()
    conn.close()


def carregar_historico(
    session_id,
    limite=20
):

    conn = conectar()
    cursor = conn.cursor()

    cursor.execute(
    """
    SELECT role, content
    FROM mensagens
    WHERE session_id = %s
    ORDER BY id DESC
    LIMIT %s
    """,
    (
        session_id,
        limite
    )
    )

    dados = cursor.fetchall()

    conn.close()

    dados.reverse()

    return [
        {
            "role": role,
            "content": content
        }
        for role, content in dados
    ]


# ==================================================
# MEMÓRIA IMPORTANTE
# ==================================================

def salvar_fato(
    session_id,
    chave,
    valor
):

    conn = conectar()
    cursor = conn.cursor()

    cursor.execute(
    """
    INSERT INTO memoria_importante
    (
        session_id,
        chave,
        valor
    )
    VALUES (%s, %s, %s)

    ON CONFLICT
    (
        session_id,
        chave
    )

    DO UPDATE SET

    valor = EXCLUDED.valor
    """,
    (
        session_id,
        chave,
        valor
    )
    )

    conn.commit()
    conn.close()


def buscar_fatos(session_id):

    conn = conectar()
    cursor = conn.cursor()

    cursor.execute(
    """
    SELECT chave, valor
    FROM memoria_importante
    WHERE session_id = %s
    """,
    (
        session_id,
    )
    )

    dados = cursor.fetchall()

    conn.close()

    return {
        chave: valor
        for chave, valor in dados
    }


def limpar_memoria(session_id):

    conn = conectar()
    cursor = conn.cursor()

    cursor.execute(
    """
    DELETE FROM mensagens
    WHERE session_id = %s
    """,
    (
        session_id,
    )
    )

    cursor.execute(
    """
    DELETE FROM memoria_importante
    WHERE session_id = %s
    """,
    (
        session_id,
    )
    )

    conn.commit()
    conn.close()
```
