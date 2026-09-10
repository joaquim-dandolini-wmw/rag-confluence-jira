"""O que mudou: consultas de leitura sobre o store, para a tela de mudanças.

Por que `extracted_at` responde a pergunta: o `upsert` do store SAI ANTES de
escrever quando o hash do conteúdo é igual ao que já está lá. Ou seja, essa
coluna só avança quando o documento realmente mudou — uma rodada que passa por
40 mil páginas sem alteração não move nenhuma linha daqui.

O que ela NÃO distingue é documento novo de documento alterado: os dois gravam
`extracted_at`. A tela diz "mudou", que é o que o dado sustenta.

Abre conexão SOMENTE LEITURA de propósito. O DocumentStore roda DDL na
construção, e uma tela que atualiza sozinha não deve escrever nada no banco que
a extração está usando.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Colunas leves. body_text fica de fora: são os 10 mil caracteres de cada
# página, e a tela nunca mostra o corpo.
_CAMPOS = (
    "doc_id, source, content_type, title, url, space_key, project, status, "
    "issue_type, updated, extracted_at, content_hash, indexed_hash, embedded_hash"
)

FONTES = ("confluence", "jira")
JANELAS = {"24h": 1, "7d": 7, "30d": 30, "tudo": None}


class StoreUnavailable(RuntimeError):
    """O arquivo do store não existe ou não abre."""


def connect(path: str | Path) -> sqlite3.Connection:
    caminho = Path(path)
    if not caminho.exists():
        raise StoreUnavailable(f"store não encontrado em {caminho}")
    conn = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _limite_iso(dias: int | None) -> str | None:
    """Fronteira da janela, no mesmo formato que o store grava.

    O store usa ISO em UTC com segundos, sempre com o mesmo offset, então a
    comparação de texto é válida e usa o índice como qualquer string.
    """
    if dias is None:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat(timespec="seconds")


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
    saida: dict[str, Any] = {"documentos": total, "janelas": {}, "por_fonte": {}}

    for rotulo, dias in (("24h", 1), ("7d", 7), ("30d", 30)):
        corte = _limite_iso(dias)
        linha = conn.execute(
            "SELECT count(*) AS n,"
            " sum(source = 'confluence') AS conf,"
            " sum(source = 'jira') AS jira"
            " FROM documents WHERE extracted_at >= ?",
            (corte,),
        ).fetchone()
        saida["janelas"][rotulo] = {
            "total": linha["n"] or 0,
            "confluence": linha["conf"] or 0,
            "jira": linha["jira"] or 0,
        }

    for linha in conn.execute(
        "SELECT source, count(*) AS n, max(extracted_at) AS ultimo"
        " FROM documents GROUP BY source"
    ):
        saida["por_fonte"][linha["source"]] = {
            "documentos": linha["n"],
            "ultima_mudanca": linha["ultimo"],
        }
    return saida


def per_day(conn: sqlite3.Connection, dias: int = 14) -> list[dict[str, Any]]:
    """Uma linha por dia com mudança, do mais antigo para o mais novo."""
    corte = _limite_iso(dias)
    linhas = conn.execute(
        "SELECT substr(extracted_at, 1, 10) AS dia, count(*) AS total,"
        " sum(source = 'confluence') AS confluence,"
        " sum(source = 'jira') AS jira"
        " FROM documents WHERE extracted_at >= ?"
        " GROUP BY dia ORDER BY dia",
        (corte,),
    ).fetchall()
    return [
        {
            "dia": l["dia"],
            "total": l["total"],
            "confluence": l["confluence"] or 0,
            "jira": l["jira"] or 0,
        }
        for l in linhas
    ]


def recent(
    conn: sqlite3.Connection,
    *,
    limite: int = 100,
    fonte: str | None = None,
    janela: str = "7d",
    texto: str | None = None,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []

    corte = _limite_iso(JANELAS.get(janela, 7))
    if corte is not None:
        where.append("extracted_at >= ?")
        params.append(corte)
    if fonte in FONTES:
        where.append("source = ?")
        params.append(fonte)
    if texto:
        # Título, espaço, projeto e id. Não busca no corpo: para isso existe a
        # busca de verdade, com índice.
        where.append(
            "(title LIKE ? OR ifnull(space_key,'') LIKE ? OR"
            " ifnull(project,'') LIKE ? OR doc_id LIKE ?)"
        )
        params += [f"%{texto}%"] * 4

    sql = f"SELECT {_CAMPOS} FROM documents"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY extracted_at DESC LIMIT ?"
    params.append(max(1, min(500, limite)))

    itens = []
    for l in conn.execute(sql, params):
        itens.append({
            "doc_id": l["doc_id"],
            "fonte": l["source"],
            "tipo": l["content_type"],
            "titulo": l["title"],
            "url": l["url"],
            "onde": l["space_key"] or l["project"] or "",
            "status": l["status"],
            "atualizado_na_origem": l["updated"],
            "mudou_em": l["extracted_at"],
            "pendente_index": l["indexed_hash"] != l["content_hash"],
            "pendente_denso": l["embedded_hash"] != l["content_hash"],
        })
    return itens


__all__ = [
    "FONTES", "JANELAS", "StoreUnavailable",
    "connect", "per_day", "recent", "summary",
]
