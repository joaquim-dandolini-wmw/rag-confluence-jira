"""Autenticação contra o LDAP da empresa, por bind direto.

Não existe tabela de usuários neste projeto, nem senha guardada — nem em hash.
A pessoa digita usuário e senha, e nós tentamos abrir uma conexão LDAP *como
ela*. Se o diretório aceita, ela é quem diz ser; se recusa, acabou. Quem sabe
quem é funcionário é o diretório, e ele continua sendo a única fonte da
verdade.

A consequência boa é que **não existe conta de serviço aqui**: nenhuma
credencial do diretório fica no .env. A única que circula é a da própria
pessoa, durante o bind, e não é guardada em lugar nenhum.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

LOG = logging.getLogger("auth.ldap")

# Quanto esperamos pelo diretório. Curto de propósito: esta chamada acontece
# com uma pessoa olhando para a tela, e um diretório lento não pode virar uma
# página pendurada.
TIMEOUT_S = 8

# Caracteres aceitos num nome de usuário. Lista de permissão, e não escape de
# caracteres perigosos, porque o nome é interpolado num DN: qualquer coisa
# fora disto é recusada antes de chegar perto do diretório. É mais curto de
# auditar do que uma lista do que é proibido, e não tem caso esquecido.
_USUARIO_VALIDO = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")


class LdapIndisponivel(RuntimeError):
    """O diretório não respondeu. NÃO é senha errada.

    A diferença importa na tela: dizer "usuário ou senha inválidos" quando o
    servidor está fora faz a pessoa trocar a senha à toa, e some com o
    incidente de verdade. São mensagens diferentes e alertas diferentes.
    """


@dataclass(frozen=True)
class Identidade:
    """Quem entrou, do jeito que o diretório respondeu."""

    uid: str
    nome: str
    dn: str


def usuario_aceitavel(usuario: str) -> bool:
    return bool(_USUARIO_VALIDO.match(usuario or ""))


def _normalizar_dn(dn: str) -> str:
    """DN comparável: minúsculas e sem espaço em volta dos separadores.

    O diretório pode devolver `CN=rag, OU=groups, DC=wmw,...` e a configuração
    trazer `cn=rag,ou=groups,dc=wmw,...`. São o mesmo DN, e comparar as duas
    strings cruas diria que não são — o que na prática barraria todo mundo.
    """
    partes = [p.strip() for p in dn.split(",")]
    return ",".join(p.lower() for p in partes if p)


def autenticar(cfg, usuario: str, senha: str) -> Identidade | None:
    """Devolve a identidade, ou None se as credenciais não servem.

    Levanta LdapIndisponivel se o diretório não pôde ser consultado.
    """
    # Senha vazia é recusada ANTES do bind, e este é o cuidado menos óbvio do
    # módulo. Vários servidores LDAP tratam bind com senha vazia como bind
    # anônimo e respondem SUCESSO: sem esta linha, qualquer usuário existente
    # entraria com a senha em branco.
    if not senha:
        return None
    if not usuario_aceitavel(usuario):
        # Nem chega ao diretório. Recusar aqui também impede que a tela vire
        # um canal para mandar DN arbitrário para dentro da rede.
        LOG.info("usuário recusado pelo formato", extra={"usuario": usuario[:64]})
        return None

    from ldap3 import ALL_ATTRIBUTES, Connection, Server
    from ldap3.core.exceptions import LDAPException
    from ldap3.utils.conv import escape_filter_chars

    dn = cfg.bind_dn_template.format(username=usuario)
    servidor = Server(cfg.url, connect_timeout=TIMEOUT_S)

    try:
        conexao = Connection(
            servidor,
            user=dn,
            password=senha,
            receive_timeout=TIMEOUT_S,
            raise_exceptions=False,
            auto_bind=False,
        )
        aberta = conexao.bind()
    except LDAPException as exc:
        # Servidor fora, DNS, TLS, rede. Não é credencial.
        raise LdapIndisponivel(str(exc)) from exc

    if not aberta:
        resultado = (conexao.result or {}).get("description", "?")
        LOG.info(
            "bind recusado", extra={"usuario": usuario, "resultado": resultado}
        )
        return None

    try:
        # Lê a própria entrada, já autenticado como a pessoa: nenhuma conta de
        # serviço é necessária.
        #
        # O `memberOf` é pedido PELO NOME, e isso não é detalhe. Em vários
        # diretórios — o lldap desta instalação entre eles — ele é atributo
        # OPERACIONAL: não aparece quando se pede `*` (ALL_ATTRIBUTES), só
        # quando é pedido explicitamente. Trocar esta lista por ALL_ATTRIBUTES
        # "para simplificar" faz todo login passar no bind e ser recusado por
        # grupo, sem erro nenhum que explique.
        achou = conexao.search(
            search_base=dn,
            search_scope="BASE",
            search_filter=f"({escape_filter_chars(cfg.member_attribute)}=*)",
            attributes=[cfg.member_attribute, "cn", "displayName", "uid"],
        )
        if not achou:
            achou = conexao.search(
                search_base=dn,
                search_scope="BASE",
                search_filter="(objectClass=*)",
                attributes=ALL_ATTRIBUTES,
            )
        entrada = conexao.entries[0] if conexao.entries else None
        grupos = _grupos_de(entrada, cfg.member_attribute)
        alvo = _normalizar_dn(cfg.group_dn)
        if alvo not in {_normalizar_dn(g) for g in grupos}:
            LOG.info(
                "usuário autenticou mas não está no grupo exigido",
                extra={"usuario": usuario, "grupo": cfg.group_dn, "grupos": len(grupos)},
            )
            return None
        return Identidade(uid=usuario, nome=_nome_de(entrada, usuario), dn=dn)
    except LDAPException as exc:
        raise LdapIndisponivel(str(exc)) from exc
    finally:
        try:
            conexao.unbind()
        except Exception:  # noqa: BLE001 - fechar conexão nunca derruba o login
            pass


def _grupos_de(entrada, atributo: str) -> list[str]:
    if entrada is None:
        return []
    valores = entrada[atributo].values if atributo in entrada else []
    return [str(v) for v in valores]


def _nome_de(entrada, padrao: str) -> str:
    if entrada is None:
        return padrao
    for atributo in ("displayName", "cn"):
        if atributo in entrada and entrada[atributo].value:
            return str(entrada[atributo].value)
    return padrao
