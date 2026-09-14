"""Freio de tentativas de login.

Este módulo não existe para proteger o RAG. Existe para proteger o **diretório
da empresa**.

A tela de login responde da internet e faz bind no LDAP corporativo. Sem
freio, um script de fora consegue disparar milhares de tentativas contra
contas reais: dependendo da política de bloqueio do diretório, isso não vira
invasão, vira a empresa inteira com as contas travadas — por um servidor que a
maioria dessas pessoas nem sabe que existe.

Por isso a trava por usuário **para de encaminhar ao LDAP**, em vez de só
recusar depois. Passado o limite, o diretório nem fica sabendo da tentativa.

São duas travas, e elas respondem a ataques diferentes:

  - por **usuário**, contra alguém martelando uma conta específica (o risco de
    bloqueio). Vale mesmo que as tentativas venham de mil IPs diferentes;
  - por **IP**, contra alguém varrendo muitos usuários a partir de um ponto (o
    risco de descobrir uma senha fraca qualquer).

O estado é de memória e por processo. É o certo aqui: o servidor MCP é um
processo só, e reiniciar o serviço liberar as travas é aceitável — reiniciar
não é algo que um atacante de fora consiga provocar.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# Tentativas antes de travar, e por quanto tempo. O limite por usuário é o
# mais apertado dos dois de propósito: ele fica ABAIXO do limite de bloqueio
# de qualquer diretório razoável, para que a nossa trava sempre dispare antes
# da do LDAP. Errar a senha três vezes é humano; a quarta espera.
LIMITE_USUARIO = 5
JANELA_USUARIO_S = 15 * 60
CASTIGO_USUARIO_S = 15 * 60

# Por IP o limite é maior, porque um escritório inteiro pode sair pelo mesmo
# endereço e um NAT não pode virar negação de serviço para os colegas.
LIMITE_IP = 30
JANELA_IP_S = 15 * 60
CASTIGO_IP_S = 30 * 60

# Acima disto, a limpeza roda. Impede que uma varredura com nomes aleatórios
# faça o dicionário crescer sem teto — que seria trocar bloqueio de conta por
# consumo de memória.
_MAX_CHAVES = 20_000


@dataclass
class _Balde:
    falhas: list[float] = field(default_factory=list)
    travado_ate: float = 0.0


@dataclass(frozen=True)
class Veredito:
    permitido: bool
    espera_s: int = 0

    @property
    def travado(self) -> bool:
        return not self.permitido


class Freio:
    def __init__(self, agora=time.monotonic) -> None:
        self._agora = agora
        self._lock = threading.Lock()
        self._usuarios: dict[str, _Balde] = {}
        self._ips: dict[str, _Balde] = {}

    def checar(self, usuario: str, ip: str) -> Veredito:
        """Pode tentar? Consulta sem registrar nada."""
        with self._lock:
            agora = self._agora()
            for baldes, chave in ((self._usuarios, usuario.lower()), (self._ips, ip)):
                balde = baldes.get(chave)
                if balde and balde.travado_ate > agora:
                    return Veredito(False, int(balde.travado_ate - agora) + 1)
            return Veredito(True)

    def registrar_falha(self, usuario: str, ip: str) -> None:
        with self._lock:
            agora = self._agora()
            self._falha(self._usuarios, usuario.lower(), agora,
                        LIMITE_USUARIO, JANELA_USUARIO_S, CASTIGO_USUARIO_S)
            self._falha(self._ips, ip, agora, LIMITE_IP, JANELA_IP_S, CASTIGO_IP_S)
            self._limpar(agora)

    def registrar_sucesso(self, usuario: str, ip: str) -> None:
        """Login certo zera o balde do usuário — mas NÃO o do IP.

        Zerar o do IP daria a quem tem uma conta válida um jeito de varrer
        senha dos outros de graça: bastaria intercalar um login correto a cada
        punhado de tentativas para o contador nunca fechar.
        """
        with self._lock:
            self._usuarios.pop(usuario.lower(), None)

    def _falha(self, baldes, chave, agora, limite, janela, castigo) -> None:
        balde = baldes.setdefault(chave, _Balde())
        balde.falhas = [t for t in balde.falhas if agora - t < janela]
        balde.falhas.append(agora)
        if len(balde.falhas) >= limite:
            balde.travado_ate = agora + castigo
            balde.falhas.clear()

    def _limpar(self, agora: float) -> None:
        for baldes, janela in ((self._usuarios, JANELA_USUARIO_S), (self._ips, JANELA_IP_S)):
            if len(baldes) <= _MAX_CHAVES:
                continue
            mortos = [
                chave for chave, balde in baldes.items()
                if balde.travado_ate <= agora
                and not [t for t in balde.falhas if agora - t < janela]
            ]
            for chave in mortos:
                baldes.pop(chave, None)
