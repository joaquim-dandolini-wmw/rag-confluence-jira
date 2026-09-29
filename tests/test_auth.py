"""Autenticação: LDAP, freio, estado do OAuth e o fluxo de login inteiro.

Nenhum teste toca num diretório real. O LDAP é substituído por uma função que
responde o que o teste quiser — o que se está testando aqui é a nossa metade:
que senha vazia não passa, que o freio protege o diretório, que o código é de
uso único e que o MCP recusa quem não tem token.
"""

from __future__ import annotations

import time

import pytest

from auth import ldap as ldap_mod
from auth import pagina
from auth.provider import Assinador, PedidoInvalido
from auth.ratelimit import CASTIGO_USUARIO_S, LIMITE_USUARIO, Freio
from auth.store import AuthStore, CodigoGravado, TokenGravado, novo_segredo


class CfgLdap:
    url = "ldap://exemplo.invalido:3890"
    bind_dn_template = "uid={username},ou=people,dc=wmw,dc=com,dc=br"
    group_dn = "cn=rag,ou=groups,dc=wmw,dc=com,dc=br"
    member_attribute = "memberOf"


# --------------------------------------------------------------- LDAP ------
@pytest.mark.parametrize("nome", ["joaquim.paes", "ana_b", "user-1", "A1"])
def test_usuario_aceito(nome: str) -> None:
    assert ldap_mod.usuario_aceitavel(nome)


@pytest.mark.parametrize(
    "nome",
    [
        "",
        "a" * 65,
        "ana,ou=admin",            # fecharia o RDN e injetaria um DN
        "ana)(objectClass=*",      # injeção de filtro
        "ana bonita",              # espaço
        "ana\x00",                 # byte nulo
    ],
)
def test_usuario_recusado_antes_do_diretorio(nome: str) -> None:
    assert not ldap_mod.usuario_aceitavel(nome)


def test_senha_vazia_nao_chega_ao_diretorio(monkeypatch) -> None:
    """Bind com senha vazia é bind anônimo em vários servidores, e responde OK.

    Sem a recusa antecipada, qualquer usuário existente entraria em branco.
    O teste garante que nem o ldap3 é importado nesse caminho.
    """
    def explodir(*a, **k):  # pragma: no cover - só falha se for chamado
        raise AssertionError("o diretório foi consultado com senha vazia")

    monkeypatch.setattr("ldap3.Connection", explodir, raising=False)
    assert ldap_mod.autenticar(CfgLdap, "ana", "") is None


def test_injecao_no_dn_nao_chega_ao_diretorio(monkeypatch) -> None:
    def explodir(*a, **k):  # pragma: no cover
        raise AssertionError("DN adulterado chegou ao diretório")

    monkeypatch.setattr("ldap3.Connection", explodir, raising=False)
    assert ldap_mod.autenticar(CfgLdap, "ana,ou=admin", "senha") is None


def test_dn_compara_sem_ligar_para_caixa_e_espaco() -> None:
    a = ldap_mod._normalizar_dn("CN=rag, OU=groups, DC=wmw,DC=com,DC=br")
    b = ldap_mod._normalizar_dn("cn=rag,ou=groups,dc=wmw,dc=com,dc=br")
    assert a == b


# --------------------------------------------------------------- freio -----
def test_freio_trava_usuario_e_solta_depois() -> None:
    relogio = [0.0]
    freio = Freio(agora=lambda: relogio[0])
    for _ in range(LIMITE_USUARIO - 1):
        freio.registrar_falha("ana", "1.1.1.1")
    assert freio.checar("ana", "1.1.1.1").permitido
    freio.registrar_falha("ana", "1.1.1.1")

    veredito = freio.checar("ana", "1.1.1.1")
    assert veredito.travado and veredito.espera_s > 0

    relogio[0] += CASTIGO_USUARIO_S + 1
    assert freio.checar("ana", "1.1.1.1").permitido


def test_freio_nao_pega_quem_nao_errou() -> None:
    freio = Freio()
    for _ in range(LIMITE_USUARIO + 3):
        freio.registrar_falha("ana", "1.1.1.1")
    assert freio.checar("bruno", "2.2.2.2").permitido


def test_sucesso_nao_zera_o_balde_do_ip() -> None:
    """Senão bastaria intercalar um login válido para varrer senha de graça."""
    freio = Freio()
    for _ in range(LIMITE_USUARIO - 1):
        freio.registrar_falha("ana", "9.9.9.9")
    freio.registrar_sucesso("ana", "9.9.9.9")
    assert freio.checar("ana", "9.9.9.9").permitido
    assert freio._ips["9.9.9.9"].falhas, "o balde do IP foi zerado indevidamente"


# --------------------------------------------------------------- store -----
@pytest.fixture()
def store(tmp_path) -> AuthStore:
    return AuthStore(tmp_path / "auth.sqlite3")


def test_codigo_e_de_uso_unico(store: AuthStore) -> None:
    codigo = novo_segredo()
    store.gravar_codigo(codigo, CodigoGravado(
        "cli", "ana", "http://localhost:1/cb", True, "desafio", ["busca"], None,
        time.time() + 300))
    assert store.consumir_codigo(codigo) is not None
    assert store.consumir_codigo(codigo) is None


def test_codigo_expirado_nao_vale(store: AuthStore) -> None:
    codigo = novo_segredo()
    store.gravar_codigo(codigo, CodigoGravado(
        "cli", "ana", "http://localhost:1/cb", True, "desafio", ["busca"], None,
        time.time() - 1))
    assert store.consumir_codigo(codigo) is None


def test_revogar_usuario_corta_todos_os_clientes(store: AuthStore) -> None:
    tokens = [novo_segredo() for _ in range(3)]
    for i, tok in enumerate(tokens):
        store.gravar_token(tok, TokenGravado(
            "access", f"cli{i}", "ana", ["busca"], None, time.time() + 600))
    outro = novo_segredo()
    store.gravar_token(outro, TokenGravado(
        "access", "cli0", "bruno", ["busca"], None, time.time() + 600))

    assert store.revogar_do_usuario("ana") == 3
    assert all(store.ler_token(t, "access") is None for t in tokens)
    assert store.ler_token(outro, "access") is not None


def test_token_e_guardado_como_hash(store: AuthStore, tmp_path) -> None:
    """Um dump do arquivo não pode entregar credencial utilizável."""
    token = novo_segredo()
    store.gravar_token(token, TokenGravado(
        "access", "cli", "ana", ["busca"], None, time.time() + 600))
    bruto = (tmp_path / "auth.sqlite3").read_bytes()
    assert token.encode() not in bruto


def test_chave_do_servidor_e_estavel(store: AuthStore) -> None:
    assert store.chave("pedido_login") == store.chave("pedido_login")


# ------------------------------------------------------------ assinatura ---
def test_pedido_adulterado_e_recusado() -> None:
    assinador = Assinador("chave")
    token = assinador.assinar({"redirect_uri": "http://localhost:1/cb"})
    with pytest.raises(PedidoInvalido):
        assinador.abrir(token[:-4] + "AAAA")


def test_pedido_de_outra_chave_e_recusado() -> None:
    token = Assinador("chave-a").assinar({"x": 1})
    with pytest.raises(PedidoInvalido):
        Assinador("chave-b").abrir(token)


def test_pedido_expirado_e_recusado() -> None:
    assinador = Assinador("chave", validade_s=-1)
    with pytest.raises(PedidoInvalido):
        assinador.abrir(assinador.assinar({"x": 1}))


# ----------------------------------------------------------------- tela ----
def test_tela_escapa_o_que_vem_de_fora() -> None:
    """O nome do cliente vem do registro dinâmico, que é aberto: é texto de
    terceiro entrando numa página que recebe senha."""
    html = pagina.render(
        campos_ocultos={"req": '"><script>alert(1)</script>'},
        cliente="<b>Cliente</b>",
        erro="<img onerror=x>",
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<b>Cliente</b>" not in html
    assert "<img onerror=x>" not in html


def test_script_da_pagina_carrega_o_nonce() -> None:
    """Sem o nonce no <script>, o CSP da resposta bloquearia o próprio script."""
    html = pagina.render(campos_ocultos={}, nonce="abc123")
    assert '<script nonce="abc123">' in html
    assert "<style>" in html, "o <style> NÃO pode levar nonce: ver auth/login.py"


def test_tela_tem_os_dois_temas() -> None:
    html = pagina.render(campos_ocultos={})
    assert '[data-tema="escuro"]' in html
    assert "prefers-color-scheme: dark" in html
    # O texto da logo é azul-escuro no arquivo; sem esta regra ele some no preto.
    assert "--logo-texto" in html and ".logo .wmw-st4" in html


def test_erro_fatal_nao_mostra_formulario() -> None:
    """Sem pedido válido não há o que enviar: o formulário não deve existir."""
    html = pagina.render(campos_ocultos={}, erro="pedido inválido",
                         com_formulario=False)
    assert "<form" not in html
    assert 'name="senha"' not in html


def test_tela_traz_a_logo_embutida() -> None:
    """Embutida, e não buscada no site: a tela não pode depender dele."""
    html = pagina.render(campos_ocultos={})
    assert "<svg" in html and "viewBox" in html
    assert "wmw.com.br" not in html


# ------------------------------------------------------------------ CSP -----
def test_origem_do_retorno() -> None:
    from auth.login import _origem

    assert _origem("https://claude.ai/api/mcp/auth_callback") == "https://claude.ai"
    assert _origem("http://localhost:33418/cb") == "http://localhost:33418"
    # Aplicativo com esquema próprio: o Cursor volta por cursor://.
    assert _origem("cursor://anysphere.cursor-mcp/oauth/callback") == "cursor:"
    assert _origem("vscode://vscode.github-authentication/did-authenticate") == "vscode:"
    # Esquema que executa código ou lê o disco nunca entra num cabeçalho de CSP.
    assert _origem("javascript:alert(1)") is None
    assert _origem("javascript://x/%0Aalert(1)") is None
    assert _origem("data://x/text/html,oi") is None
    assert _origem("file://host/etc/passwd") is None
    assert _origem("nao-e-url") is None
    assert _origem("") is None


def test_form_action_inclui_a_origem_de_retorno() -> None:
    """O WebKit aplica form-action ao destino do REDIRECIONAMENTO pós-POST.

    Com 'self' sozinho o login dava certo no servidor e o Safari bloqueava a
    volta para o cliente, deixando a pessoa num laço de entrar sem retornar.
    """
    from auth.login import _sem_cache

    csp = _sem_cache("n1", "https://claude.ai")["Content-Security-Policy"]
    assert "form-action 'self' https://claude.ai;" in csp

    sozinho = _sem_cache("n1")["Content-Security-Policy"]
    assert "form-action 'self';" in sozinho

    cursor = _sem_cache("n1", "cursor:")["Content-Security-Policy"]
    assert "form-action 'self' cursor:;" in cursor


def test_csp_continua_apertado_no_resto() -> None:
    from auth.login import _sem_cache

    csp = _sem_cache("n1", "https://claude.ai")["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'nonce-n1'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp
