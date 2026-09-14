"""As rotas da tela de login: GET mostra, POST decide.

É o único lugar do projeto que toca em senha, e ela não é guardada, logada
nem repassada: entra no POST, vai para o bind, e o objeto morre com a função.
"""

from __future__ import annotations

import logging
import secrets

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from auth import ldap as ldap_mod
from auth import pagina
from auth.provider import PedidoInvalido
from auth.ratelimit import Freio

LOG = logging.getLogger("auth.login")

COOKIE_CSRF = "rag_csrf"

# Uma mensagem só para senha errada, usuário inexistente e usuário fora do
# grupo. A tela responde da internet: distinguir os casos a transformaria num
# jeito de descobrir quem trabalha aqui e quem tem acesso a quê.
ERRO_CREDENCIAL = "Usuário ou senha inválidos, ou você não tem acesso a esta busca."
ERRO_INDISPONIVEL = (
    "O diretório da empresa não respondeu. Isto não é problema da sua senha — "
    "tente de novo em alguns minutos."
)


def _ip(request: Request) -> str:
    """IP do cliente, atrás do proxy.

    O X-Forwarded-For pode vir com uma lista; o primeiro é o cliente. Só é
    confiável porque o único caminho até aqui passa pelo nosso proxy — se um
    dia a porta ficar exposta direto, este valor vira palpite do atacante e o
    freio por IP deixa de valer.
    """
    encaminhado = request.headers.get("x-forwarded-for", "")
    if encaminhado:
        return encaminhado.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _erro_fatal(mensagem: str) -> HTMLResponse:
    # Sem pedido válido não há para onde redirecionar: qualquer destino aqui
    # seria escolhido por quem montou a URL. Então a página termina aqui, e sem
    # formulário — não há o que enviar.
    nonce = secrets.token_urlsafe(16)
    corpo = pagina.render(campos_ocultos={}, erro=mensagem, nonce=nonce,
                          com_formulario=False)
    return HTMLResponse(corpo, status_code=400, headers=_sem_cache(nonce))


def _sem_cache(nonce: str) -> dict[str, str]:
    """Cabeçalhos da tela de login.

    O script do tema e do olho da senha é inline e roda com **nonce**, não com
    `script-src 'unsafe-inline'`: numa página que recebe senha, permitir
    qualquer script inline seria transformar qualquer falha de escape em roubo
    de credencial. O nonce muda a cada resposta, então script injetado não
    executa.

    O `style-src` continua com 'unsafe-inline' porque o SVG da logo traz um
    <style> próprio, que não tem como receber nonce — e pôr nonce em style-src
    faria o CSP ignorar o 'unsafe-inline' e bloquear justamente ele.
    """
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate, private",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; "
            "style-src 'unsafe-inline'; "
            "img-src data:; "
            "form-action 'self'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'"
        ),
    }


def montar_rotas(provider, cfg, freio: Freio | None = None) -> list[Route]:
    freio = freio or Freio()
    ldap_cfg = cfg.ldap

    async def get_login(request: Request) -> Response:
        bruto = request.query_params.get("req", "")
        try:
            pedido = provider.assinador.abrir(bruto)
        except PedidoInvalido as exc:
            return _erro_fatal(f"Pedido de login inválido ou expirado ({exc}). "
                               "Volte ao aplicativo e tente conectar de novo.")
        nonce = secrets.token_urlsafe(24)
        nonce_csp = secrets.token_urlsafe(16)
        resposta = HTMLResponse(
            pagina.render(
                campos_ocultos={"req": bruto, "csrf": nonce},
                cliente=pedido.get("nome_cliente") or None,
                nonce=nonce_csp,
            ),
            headers=_sem_cache(nonce_csp),
        )
        # Dupla submissão: o mesmo valor no cookie e no formulário. Impede que
        # um site de terceiro poste este formulário com credencial que ele
        # controla e deixe a pessoa logada como outra conta sem perceber.
        resposta.set_cookie(
            COOKIE_CSRF, nonce, max_age=900, httponly=True,
            samesite="lax", secure=cfg.public_url.startswith("https://"), path="/login",
        )
        return resposta

    async def post_login(request: Request) -> Response:
        formulario = await request.form()
        bruto = str(formulario.get("req", ""))
        try:
            pedido = provider.assinador.abrir(bruto)
        except PedidoInvalido as exc:
            return _erro_fatal(f"Pedido de login inválido ou expirado ({exc}).")

        enviado = str(formulario.get("csrf", ""))
        do_cookie = request.cookies.get(COOKIE_CSRF, "")
        if not enviado or not do_cookie or not secrets.compare_digest(enviado, do_cookie):
            return _erro_fatal("Sessão de login expirada. Tente conectar de novo.")

        usuario = str(formulario.get("usuario", "")).strip()
        senha = str(formulario.get("senha", ""))
        ip = _ip(request)
        cliente = pedido.get("nome_cliente") or None

        def refazer(mensagem: str, status: int = 401) -> Response:
            nonce_csp = secrets.token_urlsafe(16)
            return HTMLResponse(
                pagina.render(
                    campos_ocultos={"req": bruto, "csrf": enviado},
                    cliente=cliente, erro=mensagem, usuario=usuario,
                    nonce=nonce_csp,
                ),
                status_code=status, headers=_sem_cache(nonce_csp),
            )

        veredito = freio.checar(usuario, ip)
        if veredito.travado:
            # Aqui está o ponto do módulo inteiro: travado, o LDAP NÃO é
            # consultado. É o que impede que esta tela, aberta na internet,
            # sirva para bloquear contas da empresa em massa.
            provider.store.registrar_login(usuario, pedido.get("client_id"), ip, "bloqueado")
            minutos = max(1, veredito.espera_s // 60)
            return refazer(
                f"Muitas tentativas. Tente de novo em {minutos} minuto(s).", 429
            )

        try:
            identidade = await anyio.to_thread.run_sync(
                ldap_mod.autenticar, ldap_cfg, usuario, senha
            )
        except ldap_mod.LdapIndisponivel as exc:
            LOG.error("diretório indisponível", extra={"erro": str(exc), "ip": ip})
            provider.store.registrar_login(usuario, pedido.get("client_id"), ip, "indisponivel")
            return refazer(ERRO_INDISPONIVEL, 503)

        if identidade is None:
            freio.registrar_falha(usuario, ip)
            provider.store.registrar_login(usuario, pedido.get("client_id"), ip, "negado")
            LOG.info("login negado", extra={"usuario": usuario, "ip": ip})
            return refazer(ERRO_CREDENCIAL)

        freio.registrar_sucesso(usuario, ip)
        provider.store.registrar_login(identidade.uid, pedido.get("client_id"), ip, "ok")
        LOG.info("login aceito", extra={"usuario": identidade.uid, "ip": ip,
                                        "client_id": pedido.get("client_id")})
        destino = provider.emitir_codigo(pedido, identidade.uid)
        resposta = RedirectResponse(destino, status_code=302,
                                    headers=_sem_cache(secrets.token_urlsafe(16)))
        resposta.delete_cookie(COOKIE_CSRF, path="/login")
        return resposta

    return [
        Route("/login", get_login, methods=["GET"]),
        Route("/login", post_login, methods=["POST"]),
    ]
