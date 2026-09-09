"""Document store local em SQLite: texto já limpo, mais o estado do sync.

Esta é a peça central da arquitetura, não um cache. A extração é a parte cara
e frágil: o XML-RPC do Confluence é lento, não pagina e não filtra por data.
Reextrair tudo a cada mudança de estratégia de chunking ou de modelo levaria
horas. Gravando o texto normalizado aqui, reconstruir o índice vira operação
de minutos e nenhuma instância Atlassian é tocada.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

LOG = logging.getLogger("store.documents")

SOURCE_CONFLUENCE = "confluence"
SOURCE_JIRA = "jira"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    content_type  TEXT,
    title         TEXT NOT NULL,
    body_text     TEXT NOT NULL,
    url           TEXT NOT NULL,
    updated       TEXT,
    project       TEXT,
    status        TEXT,
    issue_type    TEXT,
    space_key     TEXT,
    labels_json   TEXT NOT NULL DEFAULT '[]',
    content_hash  TEXT NOT NULL,
    extracted_at  TEXT NOT NULL,
    indexed_hash  TEXT,
    embedded_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_source     ON documents(source);
CREATE INDEX IF NOT EXISTS idx_documents_space      ON documents(space_key);
CREATE INDEX IF NOT EXISTS idx_documents_project    ON documents(project);
CREATE INDEX IF NOT EXISTS idx_documents_pending    ON documents(indexed_hash);
-- O índice de embedded_hash NÃO fica aqui: este script roda antes de _migrate,
-- e num store que ainda não tem a coluna ele falharia com "no such column".
-- Quem o cria é _migrate, depois de garantir a coluna.

-- Tabela própria, e não um blob JSON único, para que o estado possa ser
-- gravado de forma incremental: uma queda no meio da rodada não pode fazer a
-- próxima começar do zero.
CREATE TABLE IF NOT EXISTS confluence_versions (
    doc_id  TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Document:
    """Formato normalizado, idêntico para as duas fontes."""

    doc_id: str
    source: str
    title: str
    body_text: str
    url: str
    updated: str | None = None
    content_type: str | None = None
    project: str | None = None
    status: str | None = None
    issue_type: str | None = None
    space_key: str | None = None
    labels: tuple[str, ...] = ()

    def content_hash(self) -> str:
        payload = "\x1f".join(
            [
                self.title,
                self.body_text,
                self.url,
                self.updated or "",
                self.status or "",
                self.issue_type or "",
                ",".join(self.labels),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def indexable_text(self) -> str:
        return f"{self.title}\n\n{self.body_text}".strip()


@dataclass
class StoreStats:
    documents: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    pending_index: int = 0
    pending_embed: int = 0


class DocumentStore:
    """Acesso ao SQLite. Commit é explícito, nunca automático.

    O chamador controla o ponto de commit porque é isso que define a
    granularidade do progresso salvo em caso de falha parcial.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level="DEFERRED")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Colunas acrescentadas depois que já existiam stores em produção.

        O CREATE TABLE IF NOT EXISTS do _SCHEMA não altera tabela existente, e
        este store tem 33 mil documentos que custaram 1h19min de extração
        contra as instâncias reais: recriar não é opção.
        """
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(documents)")
        }
        if "embedded_hash" not in existing:
            self._conn.execute("ALTER TABLE documents ADD COLUMN embedded_hash TEXT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_pending_emb "
            "ON documents(embedded_hash)"
        )

    # -- ciclo de vida -----------------------------------------------------

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def __enter__(self) -> DocumentStore:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # Commit também em erro: progresso parcial é progresso.
        try:
            self._conn.commit()
        finally:
            self._conn.close()

    # -- documentos --------------------------------------------------------

    def upsert(self, doc: Document) -> bool:
        """Grava o documento. Retorna True se o conteúdo mudou."""
        digest = doc.content_hash()
        row = self._conn.execute(
            "SELECT content_hash FROM documents WHERE doc_id = ?", (doc.doc_id,)
        ).fetchone()
        if row is not None and row["content_hash"] == digest:
            return False
        self._conn.execute(
            """
            INSERT INTO documents (doc_id, source, content_type, title, body_text, url,
                                   updated, project, status, issue_type, space_key,
                                   labels_json, content_hash, extracted_at, indexed_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    (SELECT indexed_hash FROM documents WHERE doc_id = ?))
            ON CONFLICT(doc_id) DO UPDATE SET
                source=excluded.source, content_type=excluded.content_type,
                title=excluded.title, body_text=excluded.body_text, url=excluded.url,
                updated=excluded.updated, project=excluded.project,
                status=excluded.status, issue_type=excluded.issue_type,
                space_key=excluded.space_key, labels_json=excluded.labels_json,
                content_hash=excluded.content_hash, extracted_at=excluded.extracted_at
            """,
            (
                doc.doc_id, doc.source, doc.content_type, doc.title, doc.body_text,
                doc.url, doc.updated, doc.project, doc.status, doc.issue_type,
                doc.space_key, json.dumps(list(doc.labels), ensure_ascii=False),
                digest, _now(), doc.doc_id,
            ),
        )
        return True

    def get(self, doc_id: str) -> Document | None:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return _row_to_document(row) if row else None

    def delete(self, doc_ids: Sequence[str]) -> int:
        if not doc_ids:
            return 0
        marks = ",".join("?" for _ in doc_ids)
        cursor = self._conn.execute(
            f"DELETE FROM documents WHERE doc_id IN ({marks})", tuple(doc_ids)
        )
        self._conn.execute(
            f"DELETE FROM confluence_versions WHERE doc_id IN ({marks})", tuple(doc_ids)
        )
        return cursor.rowcount

    def list_doc_ids(
        self, *, source: str | None = None, space_key: str | None = None,
        project: str | None = None,
    ) -> set[str]:
        clauses: list[str] = []
        params: list[Any] = []
        if source:
            clauses.append("source = ?")
            params.append(source)
        if space_key:
            clauses.append("space_key = ?")
            params.append(space_key)
        if project:
            clauses.append("project = ?")
            params.append(project)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(f"SELECT doc_id FROM documents{where}", params)
        return {row["doc_id"] for row in rows}

    def iter_pending_index(self, batch_size: int = 200) -> Iterator[Document]:
        """Documentos cujo conteúdo mudou desde a última indexação.

        Pagina por doc_id crescente em vez de reconsultar sempre o mesmo topo
        da fila. Assim o cursor avança independentemente de o consumidor ter
        conseguido indexar o documento: uma falha transitória num documento
        não represa a fila nem interrompe o restante da rodada. Quem falhou
        continua pendente e entra na próxima execução.
        """
        cursor = ""
        while True:
            rows = self._conn.execute(
                """
                SELECT * FROM documents
                WHERE (indexed_hash IS NULL OR indexed_hash <> content_hash)
                  AND doc_id > ?
                ORDER BY doc_id
                LIMIT ?
                """,
                (cursor, batch_size),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                yield _row_to_document(row)
            cursor = rows[-1]["doc_id"]

    def count_pending_index(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM documents "
            "WHERE indexed_hash IS NULL OR indexed_hash <> content_hash"
        ).fetchone()
        return int(row["n"])

    def mark_indexed(self, doc_id: str, content_hash: str) -> None:
        self._conn.execute(
            "UPDATE documents SET indexed_hash = ? WHERE doc_id = ?", (content_hash, doc_id)
        )

    def count_indexed(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE indexed_hash IS NOT NULL"
        ).fetchone()
        return int(row["n"])

    def mark_all_unindexed(self) -> None:
        """Força reindexação completa a partir do store, sem tocar no Atlassian."""
        self._conn.execute("UPDATE documents SET indexed_hash = NULL")

    # -- vetor denso (Fase 2) ----------------------------------------------
    #
    # Coluna separada de indexed_hash de propósito: o BM25 é calculado no
    # cliente em microssegundos, o denso custa GPU. Reindexar o sparse não pode
    # obrigar a reembedar, e vice-versa.

    def iter_pending_embed(self, batch_size: int = 200) -> Iterator[Document]:
        """Documentos sem vetor denso atualizado.

        Só devolve documento JÁ INDEXADO (indexed_hash = content_hash). O
        `embed` faz UPDATE do vetor denso em ponto existente; se o ponto ainda
        não foi criado pelo `index`, o update do Qdrant não tem o que atualizar
        e o vetor se perderia em silêncio, com o documento marcado como
        embedado. Por isso a ordem é sempre index -> embed.

        Pagina por doc_id crescente pelo mesmo motivo do iter_pending_index:
        o cursor avança mesmo quando o consumidor falha num documento, então
        uma falha transitória não represa a fila nem aborta a rodada.
        """
        cursor = ""
        while True:
            rows = self._conn.execute(
                """
                SELECT * FROM documents
                WHERE (embedded_hash IS NULL OR embedded_hash <> content_hash)
                  AND indexed_hash = content_hash
                  AND doc_id > ?
                ORDER BY doc_id
                LIMIT ?
                """,
                (cursor, batch_size),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                yield _row_to_document(row)
            cursor = rows[-1]["doc_id"]

    def count_pending_embed(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM documents "
            "WHERE (embedded_hash IS NULL OR embedded_hash <> content_hash) "
            "AND indexed_hash = content_hash"
        ).fetchone()
        return int(row["n"])

    def mark_embedded(self, doc_id: str, content_hash: str) -> None:
        self._conn.execute(
            "UPDATE documents SET embedded_hash = ? WHERE doc_id = ?",
            (content_hash, doc_id),
        )

    def count_embedded(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE embedded_hash IS NOT NULL"
        ).fetchone()
        return int(row["n"])

    def mark_all_unembedded(self) -> None:
        """Força reembedar tudo a partir do store, sem tocar no Atlassian."""
        self._conn.execute("UPDATE documents SET embedded_hash = NULL")

    def confluence_space_keys(self) -> set[str]:
        """Espaços que hoje TÊM conteúdo no store.

        É o outro lado do diff de escopo: comparado com o que o Confluence
        devolve agora, diz o que entrou e o que saiu.
        """
        return {
            row["space_key"]
            for row in self._conn.execute(
                "SELECT DISTINCT space_key FROM documents "
                "WHERE source = ? AND space_key IS NOT NULL",
                (SOURCE_CONFLUENCE,),
            )
        }

    def iter_all(self) -> Iterator[Document]:
        for row in self._conn.execute("SELECT * FROM documents ORDER BY doc_id"):
            yield _row_to_document(row)

    def stats(self) -> StoreStats:
        total = self._conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        by_source = {
            row["source"]: row["n"]
            for row in self._conn.execute(
                "SELECT source, COUNT(*) AS n FROM documents GROUP BY source"
            )
        }
        return StoreStats(
            documents=int(total),
            by_source=by_source,
            pending_index=self.count_pending_index(),
            pending_embed=self.count_pending_embed(),
        )

    # -- estado do sync ----------------------------------------------------

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute(
            "SELECT value FROM sync_state WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            LOG.warning("estado do sync corrompido, ignorando", extra={"key": key})
            return default

    def set_state(self, key: str, value: Any) -> None:
        self._conn.execute(
            """
            INSERT INTO sync_state (key, value, updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, json.dumps(value, ensure_ascii=False), _now()),
        )

    # confluence_versions: {doc_id: version}
    def confluence_versions(self, space_key: str | None = None) -> dict[str, int]:
        if space_key:
            rows = self._conn.execute(
                """
                SELECT v.doc_id AS doc_id, v.version AS version
                FROM confluence_versions v
                LEFT JOIN documents d ON d.doc_id = v.doc_id
                WHERE d.space_key = ? OR d.space_key IS NULL
                """,
                (space_key,),
            )
        else:
            rows = self._conn.execute("SELECT doc_id, version FROM confluence_versions")
        return {row["doc_id"]: int(row["version"]) for row in rows}

    def set_confluence_version(self, doc_id: str, version: int) -> None:
        self._conn.execute(
            """
            INSERT INTO confluence_versions (doc_id, version, seen_at) VALUES (?,?,?)
            ON CONFLICT(doc_id) DO UPDATE SET version=excluded.version, seen_at=excluded.seen_at
            """,
            (doc_id, int(version), _now()),
        )

    # Deleções detectadas na extração precisam chegar ao Qdrant, mas a etapa
    # de indexação roda depois e o documento já não está no store. A fila
    # atravessa as duas etapas e sobrevive a uma queda entre elas.
    def queue_index_deletions(self, doc_ids: Sequence[str]) -> None:
        if not doc_ids:
            return
        pending = set(self.get_state("pending_index_deletions", []) or [])
        pending.update(doc_ids)
        self.set_state("pending_index_deletions", sorted(pending))

    def take_index_deletions(self) -> list[str]:
        pending = list(self.get_state("pending_index_deletions", []) or [])
        self.set_state("pending_index_deletions", [])
        return pending

    @property
    def jira_last_updated(self) -> str | None:
        value = self.get_state("jira_last_updated")
        return str(value) if value else None

    @jira_last_updated.setter
    def jira_last_updated(self, value: str | None) -> None:
        self.set_state("jira_last_updated", value)


def _row_to_document(row: sqlite3.Row) -> Document:
    return Document(
        doc_id=row["doc_id"],
        source=row["source"],
        title=row["title"],
        body_text=row["body_text"],
        url=row["url"],
        updated=row["updated"],
        content_type=row["content_type"],
        project=row["project"],
        status=row["status"],
        issue_type=row["issue_type"],
        space_key=row["space_key"],
        labels=tuple(json.loads(row["labels_json"] or "[]")),
    )
