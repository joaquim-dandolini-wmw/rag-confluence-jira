"""A tela de login: HTML, e nada além de HTML.

Separada da lógica de propósito — quem mexe no visual não precisa entender
OAuth, e quem mexe no OAuth não precisa abrir isto.

A logo vai **embutida** no HTML, lida do arquivo ao lado, e não puxada do
site da WMW. Dois motivos: a tela de login não pode depender de o site
institucional estar no ar, e buscar a imagem de lá entregaria ao servidor
deles o registro de quem está entrando aqui e quando.
"""

from __future__ import annotations

import html
from functools import lru_cache
from pathlib import Path

# Cor da marca, tirada do próprio SVG da logo.
VERDE = "#00e5bb"


@lru_cache(maxsize=1)
def _logo() -> str:
    caminho = Path(__file__).with_name("logo.svg")
    try:
        return caminho.read_text(encoding="utf-8")
    except OSError:
        # Sem a logo a tela continua funcionando. Um login que não abre porque
        # faltou um arquivo de imagem seria uma troca péssima.
        return '<span class="marca">WMW</span>'


def _svg_logo() -> str:
    svg = _logo()
    if svg.startswith("<svg"):
        return svg.replace("<svg", '<svg class="logo" role="img" aria-label="WMW"', 1)
    return svg


# O tema tem três estados: claro, escuro e "o que o sistema disser". O padrão é
# o do sistema — quem já configurou o computador não devia ter que configurar
# de novo aqui. O botão passa pelos três.
_CSS = """
*, *::before, *::after { box-sizing: border-box; }

:root {
  color-scheme: light;
  --fundo:        #eef1f5;
  --fundo-brilho: rgba(0, 229, 187, .22);
  --cartao:       #ffffff;
  --texto:        #0f1729;
  --fraco:        #62718a;
  --borda:        #dde3ec;
  --campo:        #f7f9fc;
  --campo-borda:  #d3dbe6;
  --verde:        #00c8a4;
  --verde-forte:  #00a98b;
  --verde-texto:  #00261f;
  --logo-texto:   #2f324d;
  --erro:         #a8231c;
  --erro-fundo:   #fdecea;
  --erro-borda:   #f6cdc9;
  --sombra:       0 1px 2px rgba(15,23,41,.05), 0 12px 32px -8px rgba(15,23,41,.14);
  --anel:         rgba(0, 200, 164, .28);
}

:root[data-tema="escuro"] {
  color-scheme: dark;
  --fundo:        #080b11;
  --fundo-brilho: rgba(0, 229, 187, .16);
  --cartao:       #11161f;
  --texto:        #e8eef6;
  --fraco:        #8b9bb0;
  --borda:        #222b38;
  --campo:        #0c1119;
  --campo-borda:  #2a3545;
  --verde:        #00e5bb;
  --verde-forte:  #4ff0d0;
  --verde-texto:  #04241e;
  --logo-texto:   #eef3f9;
  --erro:         #ff9b93;
  --erro-fundo:   #2a1310;
  --erro-borda:   #52231e;
  --sombra:       0 1px 2px rgba(0,0,0,.4), 0 16px 40px -12px rgba(0,0,0,.6);
  --anel:         rgba(0, 229, 187, .3);
}

@media (prefers-color-scheme: dark) {
  :root:not([data-tema="claro"]) {
    color-scheme: dark;
    --fundo:        #080b11;
    --fundo-brilho: rgba(0, 229, 187, .16);
    --cartao:       #11161f;
    --texto:        #e8eef6;
    --fraco:        #8b9bb0;
    --borda:        #222b38;
    --campo:        #0c1119;
    --campo-borda:  #2a3545;
    --verde:        #00e5bb;
    --verde-forte:  #4ff0d0;
    --verde-texto:  #04241e;
    --logo-texto:   #eef3f9;
    --erro:         #ff9b93;
    --erro-fundo:   #2a1310;
    --erro-borda:   #52231e;
    --sombra:       0 1px 2px rgba(0,0,0,.4), 0 16px 40px -12px rgba(0,0,0,.6);
    --anel:         rgba(0, 229, 187, .3);
  }
}

html { height: 100%; }
body {
  margin: 0; min-height: 100%; padding: 32px 20px;
  display: flex; align-items: center; justify-content: center;
  background: var(--fundo); color: var(--texto);
  font: 400 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI",
        Roboto, "Helvetica Neue", Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}

/* Dois brilhos na cor da marca atrás do cartão. Fixos e sem interação para
   não roubarem clique nem rolagem. */
body::before {
  content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 0;
  background:
    radial-gradient(60vw 48vw at 18% 8%,  var(--fundo-brilho), transparent 62%),
    radial-gradient(52vw 42vw at 88% 96%, var(--fundo-brilho), transparent 60%);
}

.cartao {
  position: relative; z-index: 1;
  width: 100%; max-width: 408px;
  background: var(--cartao);
  border: 1px solid var(--borda);
  border-radius: 18px;
  box-shadow: var(--sombra);
  padding: 38px 34px 30px;
}

.tema {
  position: absolute; top: 14px; right: 14px;
  width: 34px; height: 34px; display: grid; place-items: center;
  background: transparent; color: var(--fraco);
  border: 1px solid transparent; border-radius: 9px;
  cursor: pointer; padding: 0; line-height: 0;
}
.tema:hover { background: var(--campo); border-color: var(--borda); color: var(--texto); }
.tema svg { width: 17px; height: 17px; }
/* Um ícone por estado; o script troca o atributo no <html>. */
.tema .i-auto, .tema .i-claro, .tema .i-escuro { display: none; }
:root:not([data-tema]) .tema .i-auto { display: block; }
:root[data-tema="claro"]  .tema .i-claro  { display: block; }
:root[data-tema="escuro"] .tema .i-escuro { display: block; }
/* Sem JavaScript o botão não faz nada, então não aparece. */
html:not(.js) .tema { display: none; }

.logo { display: block; width: 116px; height: auto; margin: 0 auto 26px; }
/* O texto da logo é azul-escuro no arquivo e sumiria no tema escuro. A
   especificidade tem que ser maior que a do <style> de dentro do próprio SVG. */
.logo .wmw-st4, .logo .wmw-st5 { fill: var(--logo-texto); transition: fill .15s; }
.marca {
  display: block; text-align: center; font-size: 26px; font-weight: 700;
  letter-spacing: .08em; margin-bottom: 26px;
}

h1 {
  margin: 0 0 6px; text-align: center;
  font-size: 18px; font-weight: 650; letter-spacing: -.01em;
}
.sub {
  margin: 0 0 26px; text-align: center;
  color: var(--fraco); font-size: 13.5px;
}
.sub b { color: var(--texto); font-weight: 600; }

.erro {
  display: flex; gap: 9px; align-items: flex-start;
  background: var(--erro-fundo); color: var(--erro);
  border: 1px solid var(--erro-borda); border-radius: 10px;
  padding: 11px 13px; margin-bottom: 20px; font-size: 13px; line-height: 1.45;
}
.erro svg { flex: none; width: 15px; height: 15px; margin-top: 1px; }

label {
  display: block; margin-bottom: 7px;
  font-size: 12.5px; font-weight: 550; color: var(--fraco);
  letter-spacing: .01em;
}

.campo { position: relative; margin-bottom: 17px; }
input[type=text], input[type=password] {
  width: 100%; height: 44px; padding: 0 14px;
  font: inherit; font-size: 15px; color: var(--texto);
  background: var(--campo); border: 1px solid var(--campo-borda);
  border-radius: 10px; transition: border-color .15s, box-shadow .15s;
}
input::placeholder { color: var(--fraco); opacity: .65; }
input:hover { border-color: var(--borda); }
input:focus {
  outline: none; border-color: var(--verde);
  box-shadow: 0 0 0 3.5px var(--anel);
}
/* O olho fica dentro do campo, então a senha precisa de espaço à direita. */
.campo.senha input { padding-right: 46px; }
.olho {
  position: absolute; right: 5px; top: 5px;
  width: 34px; height: 34px; display: grid; place-items: center;
  background: transparent; border: 0; border-radius: 8px;
  color: var(--fraco); cursor: pointer; padding: 0; line-height: 0;
}
.olho:hover { color: var(--texto); background: var(--cartao); }
.olho svg { width: 17px; height: 17px; }
.olho .i-fechado { display: none; }
.olho[aria-pressed="true"] .i-aberto  { display: none; }
.olho[aria-pressed="true"] .i-fechado { display: block; }
html:not(.js) .olho { display: none; }
html:not(.js) .campo.senha input { padding-right: 14px; }

button[type=submit] {
  width: 100%; height: 45px; margin-top: 7px;
  font: inherit; font-size: 15px; font-weight: 650;
  color: var(--verde-texto); background: var(--verde);
  border: 0; border-radius: 10px; cursor: pointer;
  transition: background .15s, transform .06s;
}
button[type=submit]:hover  { background: var(--verde-forte); }
button[type=submit]:active { transform: translateY(1px); }
button[type=submit][disabled] { opacity: .6; cursor: progress; }

:focus-visible { outline: 2px solid var(--verde); outline-offset: 2px; }

.rodape {
  margin: 24px 0 0; padding-top: 18px; border-top: 1px solid var(--borda);
  text-align: center; color: var(--fraco); font-size: 12px; line-height: 1.65;
}

@media (max-width: 420px) {
  body { padding: 18px 14px; }
  .cartao { padding: 30px 22px 24px; border-radius: 15px; }
}
@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; }
}
"""

# Sol, lua e meia-lua (automático). Traçado, para herdarem currentColor.
_ICONES_TEMA = """
<svg class="i-auto" viewBox="0 0 24 24" fill="none" stroke="currentColor"
     stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
  <circle cx="12" cy="12" r="8.5"/><path d="M12 3.5v17" /><path d="M12 5.5a6.5 6.5 0 0 1 0 13" fill="currentColor" stroke="none"/>
</svg>
<svg class="i-claro" viewBox="0 0 24 24" fill="none" stroke="currentColor"
     stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
  <circle cx="12" cy="12" r="4.2"/>
  <path d="M12 2.5v2.2M12 19.3v2.2M4.2 4.2l1.6 1.6M18.2 18.2l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.2 19.8l1.6-1.6M18.2 5.8l1.6-1.6"/>
</svg>
<svg class="i-escuro" viewBox="0 0 24 24" fill="none" stroke="currentColor"
     stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d="M20.5 14.3A8.8 8.8 0 1 1 9.7 3.5a7 7 0 0 0 10.8 10.8z"/>
</svg>
"""

_ICONE_ERRO = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9.2"/>'
    '<path d="M12 7.5v5.2M12 16.3v.1"/></svg>'
)

_ICONES_OLHO = """
<svg class="i-aberto" viewBox="0 0 24 24" fill="none" stroke="currentColor"
     stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d="M2 12s3.7-6.5 10-6.5S22 12 22 12s-3.7 6.5-10 6.5S2 12 2 12z"/>
  <circle cx="12" cy="12" r="2.8"/>
</svg>
<svg class="i-fechado" viewBox="0 0 24 24" fill="none" stroke="currentColor"
     stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d="M3 3l18 18"/>
  <path d="M10.6 6.2A9.9 9.9 0 0 1 12 5.5c6.3 0 10 6.5 10 6.5a18 18 0 0 1-3.3 4.1"/>
  <path d="M6.6 7.9A17.6 17.6 0 0 0 2 12s3.7 6.5 10 6.5c1.3 0 2.5-.2 3.5-.6"/>
  <path d="M9.6 9.8a2.8 2.8 0 0 0 3.8 3.9"/>
</svg>
"""

# Roda ANTES da pintura, no <head>: sem isto, o tema escuro aparece depois de
# um piscar branco, que é justamente o que incomoda em tela de login.
_JS = """
(function () {
  var r = document.documentElement;
  r.classList.add('js');
  try {
    var t = localStorage.getItem('rag-tema');
    if (t === 'claro' || t === 'escuro') r.setAttribute('data-tema', t);
  } catch (e) {}
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest && ev.target.closest('[data-acao]');
    if (!b) return;
    if (b.dataset.acao === 'tema') {
      var atual = r.getAttribute('data-tema');
      var proximo = atual === 'claro' ? 'escuro' : atual === 'escuro' ? null : 'claro';
      if (proximo) { r.setAttribute('data-tema', proximo); }
      else { r.removeAttribute('data-tema'); }
      try {
        if (proximo) localStorage.setItem('rag-tema', proximo);
        else localStorage.removeItem('rag-tema');
      } catch (e) {}
    }
    if (b.dataset.acao === 'olho') {
      var campo = document.getElementById('senha');
      var vendo = b.getAttribute('aria-pressed') === 'true';
      b.setAttribute('aria-pressed', vendo ? 'false' : 'true');
      campo.type = vendo ? 'password' : 'text';
      campo.focus();
    }
  });
  document.addEventListener('submit', function (ev) {
    var b = ev.target.querySelector('button[type=submit]');
    // Só desabilita depois do envio: desabilitar antes cancelaria o próprio POST.
    if (b) setTimeout(function () { b.disabled = true; b.textContent = 'Entrando...'; }, 0);
  });
})();
"""


def render(
    *,
    campos_ocultos: dict[str, str],
    cliente: str | None = None,
    erro: str | None = None,
    usuario: str = "",
    nonce: str = "",
    com_formulario: bool = True,
) -> str:
    """Monta a página. Tudo que vem de fora passa por escape."""
    # Só o <script> leva nonce. Ver a nota no <head> abaixo.
    atr_nonce = f' nonce="{html.escape(nonce)}"' if nonce else ""
    ocultos = "\n        ".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in campos_ocultos.items()
    )
    # Dizer QUAL cliente pediu acesso não é enfeite: consentimento cego é
    # exatamente como o phishing de OAuth funciona. Quem vê "o Cursor quer
    # acessar" e não abriu o Cursor tem como desconfiar.
    quem = (
        f'<p class="sub">O aplicativo <b>{html.escape(cliente)}</b> quer acessar a busca '
        "do Jira e do Confluence em seu nome.</p>"
        if cliente
        else '<p class="sub">Busca interna do Jira e do Confluence.</p>'
    )
    bloco_erro = (
        f'<div class="erro" role="alert">{_ICONE_ERRO}<span>{html.escape(erro)}</span></div>'
        if erro else ""
    )
    formulario = f"""<form method="post" autocomplete="on" novalidate>
        {ocultos}
        <label for="usuario">Usuário da rede</label>
        <div class="campo">
          <input id="usuario" name="usuario" type="text" value="{html.escape(usuario)}"
                 placeholder="nome.sobrenome" autocomplete="username"
                 autocapitalize="none" autocorrect="off" spellcheck="false"
                 required autofocus>
        </div>
        <label for="senha">Senha</label>
        <div class="campo senha">
          <input id="senha" name="senha" type="password" placeholder="••••••••"
                 autocomplete="current-password" required>
          <button type="button" class="olho" data-acao="olho" aria-pressed="false"
                  aria-label="Mostrar ou ocultar a senha">{_ICONES_OLHO}</button>
        </div>
        <button type="submit">Entrar</button>
      </form>""" if com_formulario else ""

    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#080b11" media="(prefers-color-scheme: dark)">
<title>Entrar — Busca WMW</title>
<style>{_CSS}</style>
<script{atr_nonce}>{_JS}</script>
</head>
<body>
  <main class="cartao">
    <button type="button" class="tema" data-acao="tema"
            aria-label="Alternar entre tema claro, escuro e automático"
            title="Tema: claro / escuro / automático">{_ICONES_TEMA}</button>
    {_svg_logo()}
    <h1>Busca do Jira e Confluence</h1>
    {quem}
    {bloco_erro}
    {formulario}
    <p class="rodape">Use o mesmo usuário e senha da rede WMW.<br>
      O acesso é <b>somente leitura</b>.</p>
  </main>
</body>
</html>"""
