"""O contrato que o SDK pede, com o LDAP no lugar da tabela de usuários.

O SDK do MCP já traz /authorize, /token, /register, /revoke e os documentos de
metadados. O que ele não tem — porque não pode ter — é opinião sobre quem é
gente. Esta classe é esse pedaço.

O encaixe está no `authorize()`: em vez de devolver o redirect com o código
pronto, ele devolve um redirect para a **nossa tela de login**, levando o
pedido assinado. O código só é emitido depois que o LDAP aceitou o bind.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from auth.store import AuthStore, CodigoGravado, TokenGravado, novo_segredo

LOG = logging.getLogger("auth.provider")

ESCOPO = "busca"
CAMINHO_LOGIN = "/login"


def _b64(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).decode("ascii").rstrip("=")


def _deb64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


class PedidoInvalido(ValueError):
    """O pedido de login não confere: expirado, adulterado ou de outro servidor."""


class Assinador:
    """Assina o pedido de autorização para ele atravessar a tela de login.

    Alternativa a guardar o pedido numa tabela: assinado, ele viaja na própria
    URL e volta intacto, e um restart no meio do login não perde nada. O que
    vai aí NÃO é segredo — são os parâmetros que o cliente já mandou —, então
    o objetivo da assinatura é só impedir adulteração: trocar o redirect_uri
    no meio do caminho é exatamente como se rouba um código de autorização.
    """

    def __init__(self, chave: str, validade_s: int = 600) -> None:
        self._chave = chave.encode("utf-8")
        self._validade = validade_s

    def assinar(self, dados: dict[str, Any]) -> str:
        corpo = dict(dados, _exp=time.time() + self._validade)
        bruto = json.dumps(corpo, separators=(",", ":"), sort_keys=True).encode("utf-8")
        assinatura = hmac.new(self._chave, bruto, hashlib.sha256).digest()
        return f"{_b64(bruto)}.{_b64(assinatura)}"

    def abrir(self, token: str) -> dict[str, Any]:
        try:
            parte_dados, parte_assinatura = token.split(".", 1)
            bruto = _deb64(parte_dados)
            esperada = hmac.new(self._chave, bruto, hashlib.sha256).digest()
            # compare_digest e não ==: comparação normal vaza, pelo tempo, quantos
            # bytes do início batem, e isso é suficiente para forjar a assinatura.
            if not hmac.compare_digest(esperada, _deb64(parte_assinatura)):
                raise PedidoInvalido("assinatura não confere")
            dados = json.loads(bruto)
        except PedidoInvalido:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PedidoInvalido("pedido ilegível") from exc
        if dados.get("_exp", 0) < time.time():
            raise PedidoInvalido("pedido expirado")
        return dados


class LdapOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(self, store: AuthStore, cfg) -> None:
        self.store = store
        self.cfg = cfg
        self.assinador = Assinador(store.chave("pedido_login"))

    # --- clientes ---------------------------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        bruto = self.store.ler_cliente(client_id)
        if bruto is None:
            return None
        return OAuthClientInformationFull.model_validate_json(bruto)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Registro dinâmico fica aberto de propósito: é o que faz o Claude
        # Desktop e o ChatGPT conectarem sem ninguém digitar client_id. Ele não
        # dá acesso a nada — registrar só permite PEDIR autorização, e quem
        # decide é o LDAP na tela seguinte.
        self.store.salvar_cliente(
            client_info.client_id, client_info.model_dump_json(exclude_none=True)
        )
        LOG.info(
            "cliente registrado",
            extra={"client_id": client_info.client_id,
                   "nome": client_info.client_name or "?"},
        )

    # --- autorização ------------------------------------------------------
    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Não emite código: manda o navegador para a nossa tela de login."""
        pedido = self.assinador.assinar({
            "client_id": client.client_id,
            "nome_cliente": client.client_name or "",
            "state": params.state,
            "scopes": params.scopes or [ESCOPO],
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "explicito": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
        })
        return f"{self.cfg.public_url}{CAMINHO_LOGIN}?req={pedido}"

    def emitir_codigo(self, pedido: dict[str, Any], usuario: str) -> str:
        """Chamado pela tela de login, depois que o LDAP aceitou."""
        codigo = novo_segredo()
        self.store.gravar_codigo(codigo, CodigoGravado(
            client_id=pedido["client_id"],
            usuario=usuario,
            redirect_uri=pedido["redirect_uri"],
            redirect_explicito=bool(pedido["explicito"]),
            code_challenge=pedido["code_challenge"],
            scopes=list(pedido["scopes"]),
            resource=pedido.get("resource"),
            expira_em=time.time() + self.cfg.code_ttl_s,
        ))
        return construct_redirect_uri(
            pedido["redirect_uri"], code=codigo, state=pedido.get("state")
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        # O código é consumido aqui, e não no exchange, porque é este o ponto
        # que o SDK chama uma vez por troca. Consumir na leitura é o que torna
        # o uso único de verdade mesmo com dois pedidos simultâneos.
        gravado = self.store.consumir_codigo(authorization_code)
        if gravado is None or gravado.client_id != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=gravado.scopes,
            expires_at=gravado.expira_em,
            client_id=gravado.client_id,
            code_challenge=gravado.code_challenge,
            redirect_uri=gravado.redirect_uri,  # type: ignore[arg-type]
            redirect_uri_provided_explicitly=gravado.redirect_explicito,
            resource=gravado.resource,
            subject=gravado.usuario,
        )

    # --- tokens -----------------------------------------------------------
    def _emitir_par(self, client_id: str, usuario: str, scopes: list[str],
                    resource: str | None) -> OAuthToken:
        acesso = novo_segredo()
        refresh = novo_segredo()
        expira = time.time() + self.cfg.token_ttl_s
        self.store.gravar_token(acesso, TokenGravado(
            "access", client_id, usuario, scopes, resource, expira))
        self.store.gravar_token(refresh, TokenGravado(
            "refresh", client_id, usuario, scopes, resource,
            time.time() + self.cfg.refresh_ttl_s))
        return OAuthToken(
            access_token=acesso,
            token_type="Bearer",
            expires_in=self.cfg.token_ttl_s,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if authorization_code.subject is None:
            raise TokenError("invalid_grant", "código sem usuário associado")
        LOG.info("token emitido", extra={"usuario": authorization_code.subject,
                                         "client_id": client.client_id})
        return self._emitir_par(client.client_id, authorization_code.subject,
                                authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        gravado = self.store.ler_token(refresh_token, "refresh")
        if gravado is None or gravado.client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=gravado.client_id,
            scopes=gravado.scopes,
            expires_at=int(gravado.expira_em) if gravado.expira_em else None,
            resource=gravado.resource,
            subject=gravado.usuario,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if refresh_token.subject is None:
            raise TokenError("invalid_grant", "refresh sem usuário associado")
        # Rotação: o refresh usado morre aqui. Sem isto, um refresh vazado vale
        # para sempre — e o roubo não deixa rastro nenhum.
        self.store.revogar_token(refresh_token.token)
        pedidos = scopes or refresh_token.scopes
        if not set(pedidos) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope", "escopo maior do que o concedido")
        return self._emitir_par(client.client_id, refresh_token.subject,
                                list(pedidos), refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        gravado = self.store.ler_token(token, "access")
        if gravado is None:
            return None
        return AccessToken(
            token=token,
            client_id=gravado.client_id,
            scopes=gravado.scopes,
            expires_at=int(gravado.expira_em) if gravado.expira_em else None,
            resource=gravado.resource,
            subject=gravado.usuario,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.revogar_token(token.token)
