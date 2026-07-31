import re
from datetime import datetime
from database import *

class Memoria:

    def extrair_fatos(self, session_id, texto):

        texto_lower = texto.lower()

        nome = re.search(
            r"meu nome é (.+)",
            texto_lower
        )

        if nome:
            salvar_fato(
                session_id,
                "nome",
                nome.group(1).strip()
            )

    def buscar_historico(self, session_id):
        return carregar_historico(session_id)

    def salvar(self, session_id, pergunta, resposta):

        agora = datetime.now().isoformat()

        self.extrair_fatos(
            session_id,
            pergunta
        )

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

    def buscar_fatos_usuario(
        self,
        session_id
    ):

        return buscar_fatos(
            session_id
        )
