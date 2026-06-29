import sqlite3

DB_NAME = "sexta_feira.db"


def conectar():
    return sqlite3.connect(DB_NAME)


def criar_tabelas():

    conn = conectar()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT UNIQUE,
        nome TEXT,
        plano TEXT DEFAULT 'free'
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS mensagens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        role TEXT,
        content TEXT,
        timestamp TEXT
    )
    """)

    conn.commit()
    conn.close()
    
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
    VALUES (?, ?, ?, ?)
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
    WHERE session_id = ?
    ORDER BY id DESC
    LIMIT ?
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
