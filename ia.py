import os
import requests

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

MODEL = "openrouter/free"

SYSTEM_PROMPT = """
Você é Sexta-Feira.

Uma assistente pessoal inteligente.

Natural.

Humana.

Feminina.

Lembra do usuário.

Conversa como uma pessoa.
"""

class IA:

    def __init__(self, memoria):

        self.memoria = memoria

    def conversar(self, mensagem, session_id):

        historico = self.memoria.buscar_historico(session_id)

        mensagens = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            }
        ]

        mensagens.extend(historico)

        mensagens.append({
            "role":"user",
            "content":mensagem
        })

        resposta = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization":
                f"Bearer {OPENROUTER_API_KEY}",

                "Content-Type":
                "application/json"
            },
            json={
                "model":MODEL,
                "messages":mensagens
            },
            timeout=60
        )
        if resposta.status_code != 200:
            raise Exception(resposta.text)
        texto = resposta.json()["choices"][0]["message"]["content"]

        self.memoria.salvar(
            session_id,
            mensagem,
            texto
        )

        return texto
