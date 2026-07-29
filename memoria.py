from database import *
from datetime import datetime

class Memoria:

    def buscar_historico(self, session_id):
        return carregar_historico(session_id)

    def salvar(self, session_id, pergunta, resposta):
        agora = datetime.now().isoformat()

        salvar_mensagem(
            session_id,
            "user",
            pergunta,
            agora
        )

        salvar_mensagem(
            session_id,
            "assistant",
            resposta,
            agora
        )
