"""Testes do fatiamento."""

from __future__ import annotations

import uuid

import pytest

from indexer.chunking import (
    DEFAULT_MAX_CHARS,
    DEFAULT_OVERLAP,
    MAX_TITLE_CHARS,
    Chunk,
    chunk_document,
    chunk_id_for,
)

TITULO = "Runbook do gateway de pagamentos"


def _texts(chunks: list[Chunk]) -> list[str]:
    return [c.text for c in chunks]


# --------------------------------------------------------------------------
# Título prefixado em toda fatia
# --------------------------------------------------------------------------

def test_titulo_prefixado_em_toda_fatia() -> None:
    body = "\n\n".join(f"Parágrafo número {i} com bastante conteúdo. " * 12 for i in range(12))
    chunks = chunk_document("doc:1", TITULO, body)
    assert len(chunks) > 3
    assert all(c.text.startswith(f"{TITULO}\n\n") for c in chunks)


def test_titulo_prefixado_tambem_no_documento_de_uma_fatia() -> None:
    chunks = chunk_document("doc:1", TITULO, "Texto curto.")
    assert len(chunks) == 1
    assert chunks[0].text == f"{TITULO}\n\nTexto curto."


def test_titulo_muito_longo_e_truncado_mas_continua_presente() -> None:
    titulo = "T" * 500
    chunks = chunk_document("doc:1", titulo, "corpo " * 500)
    assert chunks
    for chunk in chunks:
        assert chunk.text.startswith("T" * MAX_TITLE_CHARS)
        assert len(chunk.text) <= DEFAULT_MAX_CHARS


def test_documento_sem_corpo_gera_fatia_com_o_titulo() -> None:
    chunks = chunk_document("doc:1", TITULO, "")
    assert len(chunks) == 1
    assert chunks[0].text == TITULO


def test_documento_sem_titulo_e_sem_corpo_nao_gera_fatia() -> None:
    assert chunk_document("doc:1", "", "") == []


def test_acentuacao_preservada() -> None:
    chunks = chunk_document("doc:1", "Configuração", "Certificado expirado na produção.")
    assert "Configuração" in chunks[0].text
    assert "produção" in chunks[0].text


# --------------------------------------------------------------------------
# Documento curto
# --------------------------------------------------------------------------

def test_documento_curto_gera_uma_unica_fatia() -> None:
    chunks = chunk_document("doc:1", TITULO, "Uma frase só, bem curta.")
    assert len(chunks) == 1
    assert chunks[0].index == 0


def test_documento_no_limite_exato_nao_estoura() -> None:
    corpo = "a" * (DEFAULT_MAX_CHARS - len(TITULO) - 2)
    chunks = chunk_document("doc:1", TITULO, corpo)
    assert len(chunks) == 1
    assert len(chunks[0].text) <= DEFAULT_MAX_CHARS


# --------------------------------------------------------------------------
# Parágrafo único gigante
# --------------------------------------------------------------------------

def test_paragrafo_unico_gigante_e_fatiado() -> None:
    corpo = "palavra " * 3000
    chunks = chunk_document("doc:1", TITULO, corpo)
    assert len(chunks) > 5
    assert all(len(c.text) <= DEFAULT_MAX_CHARS for c in chunks)
    assert all(c.text.startswith(TITULO) for c in chunks)


def test_paragrafo_gigante_sem_espaco_nenhum() -> None:
    corpo = "x" * 9000
    chunks = chunk_document("doc:1", TITULO, corpo)
    assert len(chunks) > 5
    assert all(len(c.text) <= DEFAULT_MAX_CHARS for c in chunks)
    assert "".join(c.text.replace(f"{TITULO}\n\n", "") for c in chunks).count("x") >= 9000


def test_frase_unica_gigante_e_cortada_em_limite_de_palavra() -> None:
    corpo = " ".join("token%03d" % i for i in range(600))
    chunks = chunk_document("doc:1", TITULO, corpo)
    conteudo = " ".join(c.text.replace(f"{TITULO}\n\n", "") for c in chunks)
    assert "token000" in conteudo and "token599" in conteudo
    assert all(len(c.text) <= DEFAULT_MAX_CHARS for c in chunks)


def test_nenhuma_fatia_vazia() -> None:
    corpo = "\n\n\n\n".join(["", "  ", "conteúdo real", "", "outro conteúdo", "   "])
    chunks = chunk_document("doc:1", TITULO, corpo)
    assert all(c.text.strip() != TITULO.strip() or len(chunks) == 1 for c in chunks)
    assert all(c.text.strip() for c in chunks)


# --------------------------------------------------------------------------
# Fronteira de cabeçalho e de parágrafo
# --------------------------------------------------------------------------

def test_fatia_nao_atravessa_cabecalho() -> None:
    corpo = (
        "## Instalação\n\nPasso um da instalação.\n\n"
        "## Rollback\n\nPasso um do rollback.\n"
    )
    chunks = chunk_document("doc:1", TITULO, corpo)
    assert len(chunks) == 2
    assert "Instalação" in chunks[0].text and "Rollback" not in chunks[0].text
    assert "Rollback" in chunks[1].text and "Instalação" not in chunks[1].text


def test_cabecalho_vira_metadado_da_fatia() -> None:
    corpo = "## Diagnóstico\n\nVerifique o log.\n\n### Passo dois\n\nReinicie."
    chunks = chunk_document("doc:1", TITULO, corpo)
    assert [c.heading for c in chunks] == ["Diagnóstico", "Passo dois"]


def test_preambulo_antes_do_primeiro_cabecalho_e_preservado() -> None:
    corpo = "Introdução solta.\n\n## Seção\n\nCorpo da seção."
    chunks = chunk_document("doc:1", TITULO, corpo)
    assert chunks[0].heading is None
    assert "Introdução solta." in chunks[0].text


def test_paragrafos_pequenos_sao_agrupados_na_mesma_fatia() -> None:
    corpo = "\n\n".join(f"Parágrafo {i}." for i in range(10))
    chunks = chunk_document("doc:1", TITULO, corpo)
    assert len(chunks) == 1
    assert "Parágrafo 0." in chunks[0].text and "Parágrafo 9." in chunks[0].text


# --------------------------------------------------------------------------
# Sobreposição
# --------------------------------------------------------------------------

def test_ha_sobreposicao_entre_fatias_consecutivas() -> None:
    corpo = "\n\n".join(f"Bloco {i}: " + "conteúdo relevante. " * 20 for i in range(8))
    chunks = chunk_document("doc:1", TITULO, corpo)
    assert len(chunks) > 2
    corpo_1 = chunks[0].text.replace(f"{TITULO}\n\n", "")
    corpo_2 = chunks[1].text.replace(f"{TITULO}\n\n", "")
    sufixo = corpo_1[-DEFAULT_OVERLAP:]
    assert any(palavra in corpo_2 for palavra in sufixo.split()[-4:])


def test_sobreposicao_configuravel_em_zero() -> None:
    corpo = "\n\n".join("bloco " * 60 for _ in range(6))
    sem = chunk_document("doc:1", TITULO, corpo, overlap=0)
    com = chunk_document("doc:1", TITULO, corpo, overlap=400)
    assert len(com) >= len(sem)


# --------------------------------------------------------------------------
# IDs determinísticos
# --------------------------------------------------------------------------

def test_ids_estaveis_entre_execucoes() -> None:
    corpo = "\n\n".join(f"Parágrafo {i} com texto suficiente. " * 8 for i in range(10))
    primeira = chunk_document("confluence:page:123", TITULO, corpo)
    segunda = chunk_document("confluence:page:123", TITULO, corpo)
    assert [c.chunk_id for c in primeira] == [c.chunk_id for c in segunda]
    assert len(primeira) > 1


def test_id_segue_a_formula_uuid5() -> None:
    esperado = str(uuid.uuid5(uuid.NAMESPACE_URL, "jira:OPS-42#3"))
    assert chunk_id_for("jira:OPS-42", 3) == esperado


def test_ids_diferentes_para_documentos_diferentes() -> None:
    assert chunk_id_for("doc:a", 0) != chunk_id_for("doc:b", 0)
    assert chunk_id_for("doc:a", 0) != chunk_id_for("doc:a", 1)


def test_indices_sao_sequenciais_a_partir_de_zero() -> None:
    corpo = "\n\n".join("texto " * 100 for _ in range(10))
    chunks = chunk_document("doc:1", TITULO, corpo)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_ids_mudam_quando_o_documento_encurta_gerando_fatias_orfas() -> None:
    # Justifica o delete-antes-de-inserir do indexador: a fatia 7 do documento
    # longo continuaria no Qdrant se não fosse removida explicitamente.
    longo = chunk_document("doc:1", TITULO, "\n\n".join("texto " * 100 for _ in range(10)))
    curto = chunk_document("doc:1", TITULO, "texto curto")
    assert len(longo) > len(curto)
    assert longo[0].chunk_id == curto[0].chunk_id
