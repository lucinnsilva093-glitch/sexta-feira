from database import *

class Memoria:

    def buscar_historico(self, session_id):

        return carregar_historico(session_id)

    def salvar(
        self,
        session_id,
        pergunta,
        resposta
    ):

        salvar_mensagem(
            session_id,
            "user",
            pergunta
        )

        salvar_mensagem(
            session_id,
            "assistant",
            resposta
        )
