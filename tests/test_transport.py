"""Faxina na resposta XML-RPC antes do parse.

Nenhum teste aqui toca a instância real, mas as amostras vieram dela: as
sequências de surrogate são exatamente as que apareceram nas cinco páginas
que o expat recusava.
"""

from __future__ import annotations

import gzip
import xml.parsers.expat
import xmlrpc.client

import pytest

from connectors.confluence_legacy import (
    _RobustTransport,
    repair_char_refs,
)


def _envelope(conteudo: bytes) -> bytes:
    """Uma resposta getPage reduzida ao que importa, com o corpo escapado."""
    return (
        b'<?xml version="1.0"?><methodResponse><params><param><value><struct>'
        b"<member><name>id</name><value>940277779</value></member>"
        b"<member><name>content</name><value>" + conteudo + b"</value></member>"
        b"</struct></value></param></params></methodResponse>"
    )


class _RespostaFalsa:
    """O mínimo que parse_response consome de um HTTPResponse."""

    def __init__(self, corpo: bytes, *, encoding: str = "") -> None:
        self._corpo = corpo
        self._encoding = encoding

    def read(self, *args: object) -> bytes:
        corpo, self._corpo = self._corpo, b""
        return corpo

    def getheader(self, nome: str, padrao: str = "") -> str:
        if nome == "Content-Encoding":
            return self._encoding
        return padrao


# --------------------------------------------------------------------------
# Armadilha 8: emoji como par surrogate na referência de caractere
# --------------------------------------------------------------------------

# Os oito pares vistos nas cinco páginas reais que nunca entravam no índice.
PARES_REAIS = [
    (b"&#55357;&#57314;", "\U0001f7e2"),  # 🟢 "estado de Executando"
    (b"&#55357;&#57003;", "\U0001f6ab"),  # 🚫 "Não é permitido"
    (b"&#55358;&#56825;", "\U0001f9f9"),  # 🧹
    (b"&#55357;&#56524;", "\U0001f4cc"),  # 📌
    (b"&#55357;&#56770;", "\U0001f5c2"),  # 🗂
    (b"&#55358;&#56605;", "\U0001f91d"),  # 🤝
    (b"&#55357;&#56541;", "\U0001f4dd"),  # 📝 "Instrução de Trabalho"
    (b"&#55357;&#56600;", "\U0001f518"),  # 🔘 "Untestable"
]


@pytest.mark.parametrize("bruto,esperado", PARES_REAIS)
def test_par_surrogate_volta_a_ser_o_emoji(bruto: bytes, esperado: str) -> None:
    corrigido, reparo = repair_char_refs(bruto)
    assert reparo.surrogate_pairs == 1
    assert reparo.dropped == 0
    # A saída é uma referência numérica, não os bytes do caractere.
    assert corrigido == f"&#x{ord(esperado):X};".encode()


def test_resposta_real_deixa_de_ser_recusada_pelo_expat() -> None:
    """O caso completo: sem a faxina, a página inteira se perde.

    Trecho literal da página confluence:page:940277779, com o &#32; que o
    Confluence emite no lugar de espaço e o &amp;ccedil; do acento.
    """
    conteudo = (
        b"&lt;h1&gt;&#55357;&#56541;&#32;Instru&amp;ccedil;&amp;atilde;o&#32;"
        b"de&#32;Trabalho&lt;/h1&gt;"
    )
    bruto = _envelope(conteudo)

    with pytest.raises(xml.parsers.expat.ExpatError):
        xmlrpc.client.loads(bruto)

    corrigido, reparo = repair_char_refs(bruto)
    assert reparo.surrogate_pairs == 1
    (params,), _ = xmlrpc.client.loads(corrigido)
    assert params["content"] == "<h1>\U0001f4dd Instru&ccedil;&atilde;o de Trabalho</h1>"


def test_referencias_validas_ficam_byte_a_byte_iguais() -> None:
    bruto = b"a&#32;b&#233;c&#x41;d&#X61;e&#x1F4DD;f&#9;g"
    corrigido, reparo = repair_char_refs(bruto)
    assert corrigido == bruto
    assert not reparo


def test_texto_sem_referencia_nenhuma_passa_intacto() -> None:
    bruto = "instrução de trabalho \U0001f4dd".encode()
    corrigido, reparo = repair_char_refs(bruto)
    assert corrigido == bruto
    assert not reparo


def test_surrogate_alto_solto_e_removido() -> None:
    corrigido, reparo = repair_char_refs(b"antes&#55357;depois")
    assert corrigido == b"antesdepois"
    assert (reparo.surrogate_pairs, reparo.dropped) == (0, 1)


def test_surrogate_baixo_solto_e_removido() -> None:
    corrigido, reparo = repair_char_refs(b"antes&#56541;depois")
    assert corrigido == b"antesdepois"
    assert (reparo.surrogate_pairs, reparo.dropped) == (0, 1)


def test_par_separado_por_texto_nao_e_recombinado() -> None:
    """Só conta como par o que está colado; senão seria adivinhação."""
    corrigido, reparo = repair_char_refs(b"&#55357;x&#56541;")
    assert corrigido == b"x"
    assert (reparo.surrogate_pairs, reparo.dropped) == (0, 2)


def test_caractere_de_controle_e_removido() -> None:
    corrigido, reparo = repair_char_refs(b"a&#8;b&#0;c&#x1F;d")
    assert corrigido == b"abcd"
    assert (reparo.surrogate_pairs, reparo.dropped) == (0, 3)


def test_valor_fora_do_unicode_e_removido() -> None:
    corrigido, reparo = repair_char_refs(b"a&#1114112;b&#65535;c")
    assert corrigido == b"abc"
    assert (reparo.surrogate_pairs, reparo.dropped) == (0, 2)


def test_pares_seguidos_no_mesmo_documento() -> None:
    corrigido, reparo = repair_char_refs(
        b"&#55357;&#56541;&#32;e&#32;&#55357;&#57003;"
    )
    assert reparo.surrogate_pairs == 2
    assert corrigido == b"&#x1F4DD;&#32;e&#32;&#x1F6AB;"


# --------------------------------------------------------------------------
# a faxina está no caminho de toda chamada, não só no teste da função pura
# --------------------------------------------------------------------------

def test_parse_response_entrega_a_pagina_que_antes_falhava() -> None:
    bruto = _envelope(b"&lt;p&gt;&#55357;&#56541;&#32;ok&lt;/p&gt;")
    transport = _RobustTransport(timeout=1)
    (resultado,) = transport.parse_response(_RespostaFalsa(bruto))
    assert resultado["content"] == "<p>\U0001f4dd ok</p>"
    assert resultado["id"] == "940277779"


def test_parse_response_descompacta_gzip() -> None:
    bruto = _envelope(b"&lt;p&gt;&#55357;&#56541;&lt;/p&gt;")
    resposta = _RespostaFalsa(gzip.compress(bruto), encoding="gzip")
    transport = _RobustTransport(timeout=1)
    (resultado,) = transport.parse_response(resposta)
    assert resultado["content"] == "<p>\U0001f4dd</p>"


def test_parse_response_ainda_propaga_fault() -> None:
    """Faxina não pode engolir o Fault: o cliente decide em cima dele."""
    bruto = xmlrpc.client.dumps(
        xmlrpc.client.Fault(2, "NotPermitted"), methodresponse=True
    ).encode()
    transport = _RobustTransport(timeout=1)
    with pytest.raises(xmlrpc.client.Fault):
        transport.parse_response(_RespostaFalsa(bruto))


def test_parse_response_com_corpo_vazio_continua_estourando_no_expat() -> None:
    """O caso do proxy sem context path: 200 com corpo vazio."""
    transport = _RobustTransport(timeout=1)
    with pytest.raises(xml.parsers.expat.ExpatError):
        transport.parse_response(_RespostaFalsa(b""))
