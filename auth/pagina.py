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


_CSS = """
*,*::before,*::after { box-sizing: border-box; }
:root {
  --fundo: #f6f7f9; --cartao: #fff; --texto: #10182a; --fraco: #5b6b84;
  --borda: #dfe4ec; --verde: %(verde)s; --erro: #b3261e; --erro-fundo: #fdeceb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --fundo: #0d1117; --cartao: #161b22; --texto: #e6edf3; --fraco: #9aa7b6;
    --borda: #2b3440; --erro: #ff8f86; --erro-fundo: #2d1614;
  }
}
body {
  margin: 0; min-height: 100vh; display: flex; align-items: center;
  justify-content: center; padding: 24px; background: var(--fundo);
  color: var(--texto);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.cartao {
  width: 100%%; max-width: 400px; background: var(--cartao);
  border: 1px solid var(--borda); border-radius: 14px; padding: 32px 28px;
  box-shadow: 0 1px 2px rgba(16,24,42,.04), 0 8px 24px rgba(16,24,42,.06);
}
.logo { display: block; width: 132px; height: auto; margin: 0 auto 22px; }
.marca { display:block; text-align:center; font-size:28px; font-weight:700;
         letter-spacing:.06em; margin-bottom:22px; }
h1 { font-size: 17px; margin: 0 0 4px; text-align: center; font-weight: 600; }
.sub { margin: 0 0 22px; text-align: center; color: var(--fraco); font-size: 13px; }
.sub b { color: var(--texto); font-weight: 600; }
label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 6px; }
input[type=text], input[type=password] {
  width: 100%%; padding: 10px 12px; margin-bottom: 16px; font-size: 15px;
  color: var(--texto); background: var(--fundo);
  border: 1px solid var(--borda); border-radius: 8px;
}
input:focus-visible {
  outline: 2px solid var(--verde); outline-offset: 1px; border-color: var(--verde);
}
button {
  width: 100%%; padding: 11px; font-size: 15px; font-weight: 600; cursor: pointer;
  color: #06231d; background: var(--verde); border: 0; border-radius: 8px;
}
button:hover { filter: brightness(1.06); }
.erro {
  background: var(--erro-fundo); color: var(--erro); border-radius: 8px;
  padding: 10px 12px; margin-bottom: 18px; font-size: 13px;
}
.rodape {
  margin: 20px 0 0; text-align: center; color: var(--fraco); font-size: 12px;
  line-height: 1.6;
}
""" % {"verde": VERDE}


def _svg_logo() -> str:
    svg = _logo()
    if svg.startswith("<svg"):
        return svg.replace("<svg", '<svg class="logo" role="img" aria-label="WMW"', 1)
    return svg


def render(
    *,
    campos_ocultos: dict[str, str],
    cliente: str | None = None,
    erro: str | None = None,
    usuario: str = "",
) -> str:
    """Monta a página. Tudo que vem de fora passa por escape."""
    ocultos = "\n      ".join(
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
        else '<p class="sub">Acesso à busca do Jira e do Confluence.</p>'
    )
    bloco_erro = f'<div class="erro" role="alert">{html.escape(erro)}</div>' if erro else ""
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Entrar — Busca WMW</title>
<style>{_CSS}</style>
</head>
<body>
  <main class="cartao">
    {_svg_logo()}
    <h1>Busca do Jira e Confluence</h1>
    {quem}
    {bloco_erro}
    <form method="post" autocomplete="on">
      {ocultos}
      <label for="usuario">Usuário da rede</label>
      <input id="usuario" name="usuario" type="text" value="{html.escape(usuario)}"
             autocomplete="username" autocapitalize="none" autocorrect="off"
             spellcheck="false" required autofocus>
      <label for="senha">Senha</label>
      <input id="senha" name="senha" type="password" autocomplete="current-password" required>
      <button type="submit">Entrar</button>
    </form>
    <p class="rodape">Use o mesmo usuário e senha da rede WMW.<br>
      O acesso é somente leitura.</p>
  </main>
</body>
</html>"""
