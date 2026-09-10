"""Conector do Confluence 4.2.4 via XML-RPC (serviço confluence2).

Esta versão não tem REST: /rest/api/content simplesmente não responde. Todo
acesso passa por /rpc/xmlrpc.

O arquivo tem duas camadas propositalmente separadas:

  1. o parser de storage format -> texto limpo, funções puras, sem rede,
     integralmente coberto por tests/test_parser.py;
  2. o cliente XML-RPC e a extração incremental.

A separação existe porque o parser é onde estão os bugs difíceis e ele
precisa ser testável sem instância real.
"""

from __future__ import annotations

import gzip
import html
import logging
import re
import ssl
import time
import xml.parsers.expat
import xmlrpc.client
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Sequence

from bs4 import BeautifulSoup, NavigableString
from bs4.element import Tag
from lxml import etree

from config import ConfluenceConfig
from store.documents import Document

LOG = logging.getLogger("connectors.confluence")

AC_NS = "http://www.atlassian.com/schema/confluence/4/ac/"
RI_NS = "http://www.atlassian.com/schema/confluence/4/ri/"

# --------------------------------------------------------------------------
# camada 1: parser de storage format
# --------------------------------------------------------------------------

_CODE_MACROS = frozenset({"code", "noformat"})
_WIKI_PASSTHROUGH_MACROS = frozenset({"unmigrated-wiki-markup"})

# Só estes cinco são entidades XML de verdade. Todo o resto (&ecirc;, &nbsp;,
# &ccedil;) é entidade HTML nomeada, que o parser XML não conhece: ele apaga a
# entidade JUNTO COM A LETRA e "Você" vira "Voc". Por isso desescapamos antes
# de parsear - mas só as nomeadas, porque desfazer &lt;/&gt; transformaria um
# exemplo de código escapado em tag de verdade e apagaria o conteúdo.
_XML_BUILTIN_ENTITIES = frozenset({"lt", "gt", "amp", "quot", "apos"})
_NAMED_ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]{1,31});")

_PLACEHOLDER_FMT = "\x00BLK{}\x00"
_PLACEHOLDER_RE = re.compile(r"\x00BLK(\d+)\x00")
# A indentação de lista aninhada precisa sobreviver ao strip por linha da
# normalização de espaços; um marcador não-branco atravessa e vira espaço no fim.
_INDENT_TOKEN = "\x00IND\x00"

# Tags de bloco: precisam de separador nos limites, senão "<p>a<p>b" (comum em
# base antiga, com tag não fechada) vira "ab".
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "table", "tr", "ul", "ol", "dl", "dt", "dd",
        "blockquote", "pre", "hr", "section", "article", "aside",
        "header", "footer", "figure", "figcaption", "form", "fieldset",
    }
)

_WIKI_CODE_RE = re.compile(r"\{(code|noformat)(?::([^}\n]*))?\}(.*?)\{\1\}", re.DOTALL)
_WIKI_HEADING_RE = re.compile(r"(?m)^h([1-6])\.[ \t]+(.+?)[ \t]*$")
_WIKI_MACRO_NAMES = (
    "quote|panel|note|info|tip|warning|toc|anchor|section|column|color|align"
    "|expand|cite|excerpt|children|span|sub|sup|table-plus"
)
_WIKI_MACRO_RE = re.compile(r"\{(?:" + _WIKI_MACRO_NAMES + r")(?::[^}\n]*)?\}")


class ConfluenceAuthError(RuntimeError):
    """Falha de autenticação: sempre aborta a execução."""


def _is_tag(node: Any, prefix: str, name: str) -> bool:
    """Casa uma tag com namespace nos dois modos de parse.

    lxml-xml separa prefixo e nome local (name='macro', prefix='ac'); o
    html.parser mantém tudo junto (name='ac:macro'). O parser precisa
    funcionar nos dois porque o fallback é obrigatório.
    """
    if not isinstance(node, Tag):
        return False
    if node.name == name and node.prefix == prefix:
        return True
    return node.name == f"{prefix}:{name}"


def _find_ns(soup: BeautifulSoup | Tag, prefix: str, name: str) -> list[Tag]:
    return [t for t in soup.find_all(True) if _is_tag(t, prefix, name)]


def _child_ns(node: Tag, prefix: str, name: str) -> Tag | None:
    for child in node.find_all(True):
        if _is_tag(child, prefix, name):
            return child
    return None


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _unescape_named_entities(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        if match.group(1) in _XML_BUILTIN_ENTITIES:
            return match.group(0)
        return html.unescape(match.group(0))

    return _NAMED_ENTITY_RE.sub(repl, text)


def _make_soup(content: str, doc_hint: str) -> tuple[BeautifulSoup, str]:
    """Parseia como XML quando possível, com fallback tolerante a erro.

    Duas armadilhas ao mesmo tempo:

    - o storage format é um FRAGMENTO, vários irmãos sem raiz única. Sem o
      envelope <root> o parser para no primeiro nó e perde o resto da página;
    - o lxml em modo recuperação NÃO levanta exceção em XML inválido: ele
      conserta em silêncio e descarta pedaços. Por isso a boa-formação é
      testada antes, com recover desligado, em vez de confiar em try/except
      em volta do BeautifulSoup.
    """
    wrapped = f'<root xmlns:ac="{AC_NS}" xmlns:ri="{RI_NS}">{content}</root>'
    parser = etree.XMLParser(recover=False, resolve_entities=False, huge_tree=True)
    try:
        etree.fromstring(wrapped.encode("utf-8"), parser)
    except etree.XMLSyntaxError as exc:
        LOG.warning(
            "storage format mal formado, usando fallback html.parser",
            extra={"doc": doc_hint, "erro": str(exc).split("\n")[0]},
        )
        return BeautifulSoup(content, "html.parser"), "html.parser"
    return BeautifulSoup(wrapped, "lxml-xml"), "lxml-xml"


def _macro_name(node: Tag) -> str:
    return (node.get("ac:name") or node.get("name") or "").strip().lower()


def _macro_param(node: Tag, param_name: str) -> str:
    for child in node.find_all(True):
        if _is_tag(child, "ac", "parameter") and (child.get("ac:name") or "") == param_name:
            return _collapse(child.get_text())
    return ""


def _plain_text_body(node: Tag) -> str | None:
    body = _child_ns(node, "ac", "plain-text-body")
    return body.get_text() if body is not None else None


def _extract_code_blocks(soup: BeautifulSoup, blocks: list[str]) -> None:
    """Trata macros com corpo em CDATA ANTES de qualquer unwrap.

    Um unwrap() genérico das macros descarta o CDATA inteiro; sem este passo
    todo snippet de código da base desaparece do índice sem deixar rastro.

    Aceita <ac:macro> e <ac:structured-macro>: a 4.2 usa a primeira forma,
    <ac:structured-macro> só apareceu na 4.3, e bases migradas têm as duas.
    """
    macros = [
        t
        for t in soup.find_all(True)
        if _is_tag(t, "ac", "macro") or _is_tag(t, "ac", "structured-macro")
    ]
    for macro in macros:
        if macro.decomposed or macro.parent is None:
            continue
        name = _macro_name(macro)
        body = _plain_text_body(macro)
        if body is None:
            continue
        if name in _WIKI_PASSTHROUGH_MACROS:
            # Conteúdo pré-4.0 nunca migrado: é wiki markup, não código.
            # Devolve como texto para o passe de wiki markup adiante.
            macro.replace_with(NavigableString("\n" + body + "\n"))
            continue
        if name in _CODE_MACROS:
            language = _macro_param(macro, "language") or _macro_param(macro, "lang")
            rendered = f"```{language}\n{body.strip('\n')}\n```"
        else:
            rendered = body.strip("\n")
        blocks.append(rendered)
        macro.replace_with(NavigableString("\n" + _PLACEHOLDER_FMT.format(len(blocks) - 1) + "\n"))


def _rewrite_links(soup: BeautifulSoup) -> None:
    """Preserva o título da página referenciada em ac:link / ri:page."""
    for link in _find_ns(soup, "ac", "link"):
        if link.decomposed or link.parent is None:
            continue
        target = ""
        page = _child_ns(link, "ri", "page")
        if page is not None:
            target = (page.get("ri:content-title") or "").strip()
        if not target:
            attachment = _child_ns(link, "ri", "attachment")
            if attachment is not None:
                target = (attachment.get("ri:filename") or "").strip()
        if not target:
            user = _child_ns(link, "ri", "user")
            if user is not None:
                target = (user.get("ri:username") or user.get("ri:userkey") or "").strip()
        body = ""
        for body_name in ("plain-text-link-body", "link-body"):
            node = _child_ns(link, "ac", body_name)
            if node is not None:
                body = _collapse(node.get_text())
                break
        if body and target and body != target:
            rendered = f"{body} ({target})"
        else:
            rendered = body or target
        link.replace_with(NavigableString(f" {rendered} " if rendered else " "))


def _rewrite_images(soup: BeautifulSoup) -> None:
    for image in _find_ns(soup, "ac", "image"):
        if image.decomposed or image.parent is None:
            continue
        attachment = _child_ns(image, "ri", "attachment")
        url = _child_ns(image, "ri", "url")
        name = ""
        if attachment is not None:
            name = (attachment.get("ri:filename") or "").strip()
        elif url is not None:
            name = (url.get("ri:value") or "").strip()
        image.replace_with(NavigableString(f" [imagem: {name}] " if name else " "))


def _rewrite_headings(soup: BeautifulSoup) -> None:
    """Substitui a tag inteira pelo cabeçalho já montado em UMA linha.

    Inserir marcador antes e depois da tag produz "# \\nTítulo", que arruína o
    chunking por seção: a fatia começa num "#" órfão e o título vai para a
    fatia seguinte.
    """
    for level in range(1, 7):
        for heading in soup.find_all(f"h{level}"):
            if heading.decomposed or heading.parent is None:
                continue
            text = _collapse(heading.get_text())
            marker = "#" * level
            heading.replace_with(NavigableString(f"\n{marker} {text}\n" if text else "\n"))


def _cell_text(cell: Tag) -> str:
    return _collapse(cell.get_text(" ")).replace("|", "\\|")


def _rewrite_tables(soup: BeautifulSoup) -> None:
    """Converte tabelas em linhas markdown, tolerando ausência de <tbody>.

    Processa de dentro para fora para que tabela aninhada não seja consumida
    duas vezes.
    """
    while True:
        innermost = [t for t in soup.find_all("table") if t.find("table") is None]
        if not innermost:
            return
        for table in innermost:
            if table.decomposed or table.parent is None:
                continue
            lines: list[str] = []
            for position, row in enumerate(table.find_all("tr")):
                cells = row.find_all(["th", "td"], recursive=False) or row.find_all(["th", "td"])
                if not cells:
                    continue
                lines.append("| " + " | ".join(_cell_text(c) for c in cells) + " |")
                if position == 0 and any(c.name.endswith("th") for c in cells):
                    lines.append("| " + " | ".join("---" for _ in cells) + " |")
            table.replace_with(NavigableString("\n" + "\n".join(lines) + "\n" if lines else "\n"))


def _rewrite_lists(soup: BeautifulSoup) -> None:
    for item in soup.find_all("li"):
        if item.decomposed or item.parent is None:
            continue
        parent = item.find_parent(["ul", "ol"])
        depth = max(0, len(item.find_parents(["ul", "ol"])) - 1)
        if parent is not None and parent.name == "ol":
            siblings = [c for c in parent.find_all("li", recursive=False)]
            try:
                number = siblings.index(item) + 1
            except ValueError:
                number = 1
            marker = f"{number}. "
        else:
            marker = "- "
        item.insert_before(NavigableString("\n" + _INDENT_TOKEN * depth + marker))
        item.insert_after(NavigableString("\n"))


def _rewrite_remaining_macros(soup: BeautifulSoup) -> None:
    """Preserva o título das macros de painel e descarta os parâmetros.

    Parâmetro de macro é configuração de renderização (cor, largura, ícone):
    indexá-lo só adiciona ruído ao BM25.
    """
    macros = [
        t
        for t in soup.find_all(True)
        if _is_tag(t, "ac", "macro") or _is_tag(t, "ac", "structured-macro")
    ]
    for macro in macros:
        if macro.decomposed or macro.parent is None:
            continue
        title = _macro_param(macro, "title")
        if title:
            macro.insert_before(NavigableString(f"\n{title}\n"))
    for node in list(soup.find_all(True)):
        if node.decomposed or node.parent is None:
            continue
        if _is_tag(node, "ac", "parameter") or _is_tag(node, "ac", "default-parameter"):
            node.decompose()


def _unwrap_confluence_tags(soup: BeautifulSoup) -> None:
    for node in list(soup.find_all(True)):
        if node.decomposed or node.parent is None:
            continue
        if node.prefix in {"ac", "ri"} or node.name.startswith(("ac:", "ri:")):
            node.unwrap()


def _insert_block_breaks(soup: BeautifulSoup) -> None:
    for node in list(soup.find_all("br")):
        if not node.decomposed and node.parent is not None:
            node.replace_with(NavigableString("\n"))
    for node in list(soup.find_all(_BLOCK_TAGS)):
        if node.decomposed or node.parent is None:
            continue
        node.insert_before(NavigableString("\n"))
        node.insert_after(NavigableString("\n"))


def _protect_wiki_code(text: str, blocks: list[str]) -> str:
    """Isola {code}/{noformat} residuais antes da normalização de espaços."""

    def repl(match: re.Match[str]) -> str:
        params = (match.group(2) or "").strip()
        language = ""
        if params:
            first = params.split("|")[0].strip()
            language = first.split("=", 1)[1].strip() if "=" in first else first
        body = match.group(3).strip("\n")
        blocks.append(f"```{language}\n{body}\n```")
        return "\n" + _PLACEHOLDER_FMT.format(len(blocks) - 1) + "\n"

    return _WIKI_CODE_RE.sub(repl, text)


def _normalize_whitespace(text: str) -> str:
    text = text.replace("\xa0", " ").replace("​", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[^\S\n]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text)


def _convert_wiki_markup(text: str) -> str:
    text = _WIKI_HEADING_RE.sub(lambda m: "#" * int(m.group(1)) + " " + m.group(2), text)
    text = _WIKI_MACRO_RE.sub("", text)
    return _normalize_whitespace(text)


def _restore_blocks(text: str, blocks: Sequence[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return blocks[index] if 0 <= index < len(blocks) else ""

    return _PLACEHOLDER_RE.sub(repl, text)


def wiki_to_text(text: str | None) -> str:
    """Converte wiki markup pré-4.0 (e descrição de issue do Jira) em texto.

    O Jira 8.14 devolve description e comment em wiki markup, não em ADF, e
    páginas do Confluence migradas da 3.x guardam trechos não convertidos.
    """
    if not text or not text.strip():
        return ""
    blocks: list[str] = []
    out = _protect_wiki_code(text, blocks)
    out = _normalize_whitespace(out)
    out = _convert_wiki_markup(out)
    return _restore_blocks(out, blocks).strip()


def storage_to_text(storage: str | None, *, doc_hint: str = "") -> str:
    """Converte o storage format do Confluence em texto limpo.

    Preserva cabeçalhos como markdown em linha única, blocos de código,
    tabelas como linhas markdown e o título das páginas referenciadas.
    """
    if not storage or not storage.strip():
        return ""

    content = _unescape_named_entities(storage)
    soup, _parser = _make_soup(content, doc_hint)

    blocks: list[str] = []
    _extract_code_blocks(soup, blocks)
    _rewrite_links(soup)
    _rewrite_images(soup)
    _rewrite_headings(soup)
    _rewrite_tables(soup)
    _rewrite_lists(soup)
    _rewrite_remaining_macros(soup)
    _unwrap_confluence_tags(soup)
    _insert_block_breaks(soup)

    text = soup.get_text()
    text = _protect_wiki_code(text, blocks)
    text = _normalize_whitespace(text)
    text = _convert_wiki_markup(text)
    return _restore_blocks(text, blocks).replace(_INDENT_TOKEN, "  ").strip()


# --------------------------------------------------------------------------
# camada 2: cliente XML-RPC e extração incremental
# --------------------------------------------------------------------------

_RETRYABLE_NETWORK = (OSError, xmlrpc.client.ProtocolError)
# Resposta vazia ou em HTML chega aqui como erro de parse, não como Fault.
# O caso real: proxy reverso na frente e a URL sem o context path -
# o endpoint devolve 200 com corpo vazio e o cliente estoura no expat.
_MALFORMED_RESPONSE = (xml.parsers.expat.ExpatError, xmlrpc.client.ResponseError)
_PROGRESS_EVERY = 250
_SESSION_FAULT_MARKERS = ("InvalidSessionException", "session", "token")
_AUTH_FAULT_MARKERS = ("AuthenticationFailed", "NotPermitted", "Permission")


# Referência numérica de caractere, decimal ou hexadecimal: &#233; ou &#xE9;.
_CHAR_REF_RE = re.compile(rb"&#([Xx][0-9a-fA-F]+|[0-9]+);")


def _char_ref_value(token: bytes) -> int | None:
    try:
        if token[:1] in (b"x", b"X"):
            return int(token[1:], 16)
        return int(token)
    except ValueError:  # pragma: no cover - o regex já garante os dígitos
        return None


def _is_valid_xml_char(code: int) -> bool:
    """Faixas permitidas pela produção Char da XML 1.0."""
    return (
        code in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


@dataclass(frozen=True)
class CharRefRepair:
    """O que a faxina fez numa resposta XML-RPC."""

    surrogate_pairs: int = 0
    dropped: int = 0

    def __bool__(self) -> bool:
        return bool(self.surrogate_pairs or self.dropped)


def repair_char_refs(raw: bytes) -> tuple[bytes, CharRefRepair]:
    """Conserta referências numéricas que o expat recusa, ANTES do parse.

    O Confluence 4.2.4 serializa emoji como o PAR SURROGATE da UTF-16, uma
    referência para cada metade: 📝 sai como `&#55357;&#56541;`. Só que
    surrogate não é caractere válido em XML 1.0, então o expat rejeita a
    resposta INTEIRA com "reference to invalid character number" e a página
    nunca é indexada. É determinístico: falha em toda rodada.

    O par é recombinado no code point de verdade em vez de descartado, porque
    o emoji é conteúdo - nestas páginas ele abre título e item de lista. A
    substituição é por referência numérica (`&#x1F4DD;`), não pelos bytes do
    caractere, para não depender do encoding declarado na resposta.

    Referência inválida sem par - surrogate solto, caractere de controle,
    valor fora do plano Unicode - é removida: não existe caractere para pôr
    no lugar, e perder um byte de controle é melhor que perder a página.

    Trabalha em bytes, antes de qualquer decodificação. É seguro: referência
    numérica é ASCII puro, e byte de continuação de UTF-8 nunca colide com
    ela.
    """
    matches = list(_CHAR_REF_RE.finditer(raw))
    if not matches:
        return raw, CharRefRepair()

    out = bytearray()
    cursor = 0
    pairs = dropped = 0
    index = 0
    while index < len(matches):
        match = matches[index]
        code = _char_ref_value(match.group(1))
        if code is None or _is_valid_xml_char(code):
            index += 1
            continue

        out += raw[cursor : match.start()]
        seguinte = matches[index + 1] if index + 1 < len(matches) else None
        if 0xD800 <= code <= 0xDBFF and seguinte is not None and seguinte.start() == match.end():
            baixo = _char_ref_value(seguinte.group(1))
            if baixo is not None and 0xDC00 <= baixo <= 0xDFFF:
                code_point = 0x10000 + (code - 0xD800) * 0x400 + (baixo - 0xDC00)
                out += b"&#x%X;" % code_point
                cursor = seguinte.end()
                pairs += 1
                index += 2
                continue

        cursor = match.end()
        dropped += 1
        index += 1

    out += raw[cursor:]
    return bytes(out), CharRefRepair(surrogate_pairs=pairs, dropped=dropped)


class _RobustTransportMixin:
    """Duas correções que o xmlrpc.client não oferece.

    1. timeout: a biblioteca não o expõe, e sem ele uma chamada travada
       pendura a rodada inteira;
    2. faxina na resposta bruta antes do parse, para as referências de
       caractere inválidas que o Confluence 4.2.4 emite (ver
       repair_char_refs).
    """

    def __init__(self, timeout: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._timeout = timeout

    def make_connection(self, host: Any) -> Any:
        connection = super().make_connection(host)
        connection.timeout = self._timeout
        return connection

    def parse_response(self, response: Any) -> Any:
        # O corpo é lido de uma vez, e não em blocos como faz a implementação
        # original: uma referência de caractere pode cair na fronteira entre
        # dois blocos, e aí nenhuma faxina por bloco a enxergaria inteira.
        raw = response.read()
        getheader = getattr(response, "getheader", None)
        if getheader is not None and getheader("Content-Encoding", "") == "gzip":
            raw = gzip.decompress(raw)

        raw, repair = repair_char_refs(raw)
        if repair:
            LOG.info(
                "referências de caractere inválidas corrigidas na resposta",
                extra={
                    "pares_surrogate": repair.surrogate_pairs,
                    "removidas": repair.dropped,
                },
            )

        parser, unmarshaller = self.getparser()
        parser.feed(raw)
        parser.close()
        return unmarshaller.close()


class _RobustTransport(_RobustTransportMixin, xmlrpc.client.Transport):
    pass


class _RobustSafeTransport(_RobustTransportMixin, xmlrpc.client.SafeTransport):
    pass


def _to_iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, xmlrpc.client.DateTime):
        try:
            return datetime.strptime(str(value), "%Y%m%dT%H:%M:%S").isoformat(timespec="seconds")
        except ValueError:
            return str(value)
    return str(value)


class ConfluenceClient:
    """Cliente XML-RPC do serviço confluence2."""

    def __init__(self, cfg: ConfluenceConfig) -> None:
        self._cfg = cfg
        self._token: str | None = None

        kwargs: dict[str, Any] = {}
        if cfg.rpc_endpoint.lower().startswith("https"):
            context = ssl.create_default_context()
            if not cfg.verify_ssl:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            transport: xmlrpc.client.Transport = _RobustSafeTransport(
                cfg.timeout, context=context
            )
        else:
            transport = _RobustTransport(cfg.timeout)

        # allow_none=True faria o cliente emitir <nil/>, que o Confluence 4.x
        # rejeita: a chamada inteira falha com fault de parsing.
        self._proxy = xmlrpc.client.ServerProxy(
            cfg.rpc_endpoint,
            transport=transport,
            allow_none=False,
            use_datetime=True,
            **kwargs,
        )

    # -- sessão ------------------------------------------------------------

    def login(self) -> None:
        try:
            # Esta versão não suporta Personal Access Token: é usuário e senha.
            self._token = self._proxy.confluence2.login(self._cfg.user, self._cfg.password)
        except xmlrpc.client.Fault as fault:
            raise ConfluenceAuthError(
                f"login no Confluence recusado: {fault.faultString}"
            ) from fault
        except _MALFORMED_RESPONSE as exc:
            raise ConfluenceAuthError(
                f"{self._cfg.rpc_endpoint} respondeu algo que não é XML-RPC ({exc}). "
                "Confira se CONFLUENCE_URL inclui o context path da instalação "
                "(ex.: https://host/confluence, não https://host) e se a Remote API "
                "está habilitada."
            ) from exc
        except _RETRYABLE_NETWORK as exc:
            raise ConfluenceAuthError(
                f"não foi possível alcançar {self._cfg.rpc_endpoint}: {exc}"
            ) from exc
        LOG.info("sessão Confluence aberta", extra={"user": self._cfg.user})

    def logout(self) -> None:
        if self._token is None:
            return
        try:
            self._proxy.confluence2.logout(self._token)
        except (xmlrpc.client.Fault, *_RETRYABLE_NETWORK) as exc:
            LOG.warning("logout do Confluence falhou", extra={"erro": str(exc)})
        finally:
            self._token = None

    def __enter__(self) -> ConfluenceClient:
        self.login()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.logout()

    # -- chamadas ----------------------------------------------------------

    def _call(self, method: str, *args: Any, retries: int = 2) -> Any:
        if self._token is None:
            raise ConfluenceAuthError("chamada XML-RPC sem sessão aberta")
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return getattr(self._proxy.confluence2, method)(self._token, *args)
            except xmlrpc.client.Fault as fault:
                message = fault.faultString or ""
                if any(marker in message for marker in _AUTH_FAULT_MARKERS):
                    raise
                if any(marker in message for marker in _SESSION_FAULT_MARKERS) and attempt < retries:
                    # Token de sessão expira em rodada longa; reabrir é barato.
                    LOG.info("sessão expirada, reautenticando", extra={"metodo": method})
                    self._token = None
                    self.login()
                    last_error = fault
                    continue
                raise
            except (*_RETRYABLE_NETWORK, *_MALFORMED_RESPONSE) as exc:
                last_error = exc
                if attempt >= retries:
                    break
                delay = 2**attempt
                LOG.warning(
                    "falha de rede no XML-RPC, tentando de novo",
                    extra={"metodo": method, "tentativa": attempt + 1, "espera_s": delay},
                )
                time.sleep(delay)
        raise RuntimeError(f"chamada {method} falhou: {last_error}") from last_error

    def get_spaces(self) -> list[dict[str, Any]]:
        return list(self._call("getSpaces"))

    def get_page_summaries(self, space_key: str) -> list[dict[str, Any]]:
        """getPages devolve TODAS as páginas do espaço de uma vez.

        Não há paginação nem filtro por data nesta API. O que dá para evitar é
        segurar o conteúdo: aqui vem só o resumo (id, título, url), e o corpo
        de cada página é buscado um a um em seguida.
        """
        return list(self._call("getPages", space_key))

    def get_blog_summaries(self, space_key: str) -> list[dict[str, Any]]:
        return list(self._call("getBlogEntries", space_key))

    def get_page(self, page_id: str) -> dict[str, Any]:
        return dict(self._call("getPage", str(page_id)))

    def get_blog_entry(self, entry_id: str) -> dict[str, Any]:
        return dict(self._call("getBlogEntry", str(entry_id)))

    def get_labels(self, object_id: str) -> tuple[str, ...]:
        try:
            raw = self._call("getLabelsById", str(object_id))
        except xmlrpc.client.Fault as fault:
            LOG.warning(
                "getLabelsById falhou", extra={"object_id": object_id, "erro": fault.faultString}
            )
            return ()
        return tuple(str(item.get("name", "")).strip() for item in raw if item.get("name"))

    def count_attachments(self, content_id: str) -> int | None:
        try:
            return len(list(self._call("getAttachments", str(content_id))))
        except xmlrpc.client.Fault as fault:
            LOG.debug(
                "getAttachments indisponível", extra={"content_id": content_id, "erro": fault.faultString}
            )
            return None


# --------------------------------------------------------------------------
# extração
# --------------------------------------------------------------------------

@dataclass
class SpaceExtraction:
    """Resultado e métricas de um espaço. Preenchido durante a iteração."""

    space_key: str
    listing_complete: bool = False
    seen_doc_ids: set[str] = field(default_factory=set)
    pages_listed: int = 0
    blogs_listed: int = 0
    fetched: int = 0
    skipped_unchanged: int = 0
    failed: int = 0
    attachments: int = 0
    attachments_unavailable: int = 0
    fetch_seconds: float = 0.0
    attachment_seconds: float = 0.0

    @property
    def avg_fetch_ms(self) -> float:
        return (self.fetch_seconds / self.fetched * 1000) if self.fetched else 0.0

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "space_key": self.space_key,
            "paginas_listadas": self.pages_listed,
            "blogs_listados": self.blogs_listed,
            "buscados": self.fetched,
            "pulados_sem_mudanca": self.skipped_unchanged,
            "falhas": self.failed,
            "anexos": self.attachments,
            "anexos_indisponiveis": self.attachments_unavailable,
            "tempo_busca_s": round(self.fetch_seconds, 2),
            "tempo_anexos_s": round(self.attachment_seconds, 2),
            "media_por_getpage_ms": round(self.avg_fetch_ms, 1),
            "listagem_completa": self.listing_complete,
        }


class ConfluenceExtractor:
    """Estratégia full_scan: lista o espaço e busca cada conteúdo.

    getPages() devolve PageSummary, que nesta versão NÃO traz version nem
    modified - só o getPage() completo traz. Ou seja, o incremental não
    consegue evitar o round-trip; ele evita o parse, o chunking e o upsert.
    A escolha está registrada em CONFLUENCE_INCREMENTAL_STRATEGY para que uma
    estratégia mais barata possa entrar sem tocar neste código.
    """

    def __init__(self, client: ConfluenceClient, cfg: ConfluenceConfig) -> None:
        self._client = client
        self._cfg = cfg

    def iter_space(
        self,
        space_key: str,
        known_versions: dict[str, int],
        result: SpaceExtraction,
        *,
        force: bool = False,
        count_attachments: bool = True,
        fetch_labels: bool = True,
    ) -> Iterator[tuple[Document, int]]:
        try:
            page_summaries = self._client.get_page_summaries(space_key)
            blog_summaries = self._client.get_blog_summaries(space_key)
        except xmlrpc.client.Fault as fault:
            LOG.error(
                "não foi possível listar o espaço",
                extra={"space_key": space_key, "erro": fault.faultString},
            )
            return

        result.pages_listed = len(page_summaries)
        result.blogs_listed = len(blog_summaries)
        LOG.info(
            "espaço listado",
            extra={
                "space_key": space_key,
                "paginas": len(page_summaries),
                "blogs": len(blog_summaries),
            },
        )

        items: list[tuple[str, dict[str, Any]]] = [("page", s) for s in page_summaries]
        items += [("blog", s) for s in blog_summaries]

        # Um espaço grande fica ~30 min sem emitir nada entre "listado" e
        # "concluído". Sem esta linha periódica não há como saber, de fora, se
        # a rodada está progredindo ou pendurada.
        started_at = time.monotonic()
        for position, (content_type, summary) in enumerate(items, start=1):
            if position % _PROGRESS_EVERY == 0:
                elapsed = time.monotonic() - started_at
                rate = position / elapsed if elapsed else 0.0
                LOG.info(
                    "progresso do espaço",
                    extra={
                        "space_key": space_key,
                        "processados": position,
                        "total": len(items),
                        "pct": round(position / len(items) * 100, 1),
                        "docs_por_s": round(rate, 2),
                        "eta_min": round((len(items) - position) / rate / 60, 1) if rate else None,
                    },
                )
            content_id = str(summary.get("id", "")).strip()
            if not content_id:
                continue
            doc_id = f"confluence:{content_type}:{content_id}"
            result.seen_doc_ids.add(doc_id)

            started = time.monotonic()
            try:
                if content_type == "page":
                    full = self._client.get_page(content_id)
                else:
                    full = self._client.get_blog_entry(content_id)
            except (xmlrpc.client.Fault, RuntimeError) as exc:
                result.failed += 1
                LOG.warning(
                    "conteúdo não pôde ser buscado, seguindo",
                    extra={"doc_id": doc_id, "space_key": space_key, "erro": str(exc)},
                )
                continue
            finally:
                result.fetch_seconds += time.monotonic() - started
            result.fetched += 1

            version = int(full.get("version") or 0)
            if not force and known_versions.get(doc_id) == version:
                result.skipped_unchanged += 1
                continue

            if count_attachments:
                attachment_started = time.monotonic()
                count = self._client.count_attachments(content_id)
                result.attachment_seconds += time.monotonic() - attachment_started
                if count is None:
                    result.attachments_unavailable += 1
                else:
                    result.attachments += count

            title = str(full.get("title") or "").strip()
            body = storage_to_text(full.get("content"), doc_hint=doc_id)
            if not title and not body:
                LOG.debug("conteúdo vazio, ignorado", extra={"doc_id": doc_id})
                continue

            document = Document(
                doc_id=doc_id,
                source="confluence",
                content_type="page" if content_type == "page" else "blogpost",
                title=title or f"(sem título) {content_id}",
                body_text=body,
                url=str(full.get("url") or summary.get("url") or ""),
                updated=_to_iso(full.get("modified")) or _to_iso(full.get("created")),
                space_key=str(full.get("space") or space_key),
                labels=self._client.get_labels(content_id) if fetch_labels else (),
            )
            yield document, version

        result.listing_complete = True
