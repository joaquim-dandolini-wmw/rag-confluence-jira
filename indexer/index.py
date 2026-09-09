"""Índice no Qdrant. Fase 1: apenas BM25 sparse.

O schema já declara os dois tipos de vetor. O denso fica declarado e vazio de
propósito: criar a coleção agora com a dimensão certa evita migração quando a
camada de embeddings entrar, e uma coleção do Qdrant não pode ganhar um novo
tipo de vetor depois de criada.
"""

from __future__ import annotations

import logging
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from qdrant_client import QdrantClient, models

from config import DENSE_VECTOR_NAME, DENSE_VECTOR_SIZE, SPARSE_VECTOR_NAME
from indexer.chunking import Chunk
from store.documents import Document

LOG = logging.getLogger("indexer.index")

BM25_MODEL = "Qdrant/bm25"
BM25_LANGUAGE = "portuguese"
# O modificador IDF é calculado pelo servidor; sem ele o BM25 vira contagem
# bruta de termos e o ranking desmonta. Chegou em versões recentes do Qdrant.
MIN_QDRANT_VERSION = (1, 9)

_PAYLOAD_KEYWORD_INDEXES = ("source", "project", "space_key", "status", "doc_id")


class IndexError_(RuntimeError):
    """Falha de configuração do índice: aborta a execução."""


def fold_accents(text: str) -> str:
    """Remove diacríticos preservando a letra base.

    O tokenizer BM25 do fastembed faz stopwords e stemming, mas não normaliza
    acento. Sem isto, "certificado expirado" e "certificado expirádo" viram
    termos diferentes e a busca sem acento não encontra o documento acentuado.
    Aplicado dos dois lados - indexação e consulta - para que sejam simétricos.
    O texto original continua no payload; só o que alimenta o tokenizer é
    normalizado.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


@dataclass(frozen=True)
class SearchHit:
    score: float
    doc_id: str
    chunk_id: str
    source: str
    title: str
    url: str
    text: str
    heading: str | None = None
    project: str | None = None
    space_key: str | None = None
    status: str | None = None
    issue_type: str | None = None
    updated: str | None = None
    content_type: str | None = None
    labels: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "source": self.source,
            "title": self.title,
            # Todo resultado carrega a origem: sem URL o usuário não consegue
            # confirmar nem citar a fonte.
            "url": self.url,
            "doc_id": self.doc_id,
            "heading": self.heading,
            "project": self.project,
            "space_key": self.space_key,
            "status": self.status,
            "issue_type": self.issue_type,
            "updated": self.updated,
            "content_type": self.content_type,
            "labels": list(self.labels),
            "text": self.text,
        }


def _load_sparse_model(cache_dir: Path, allow_download: bool) -> Any:
    """Carrega o BM25 do cache local, sem tentar baixar em silêncio."""
    if not allow_download:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    cache_dir.mkdir(parents=True, exist_ok=True)

    from fastembed import SparseTextEmbedding  # importado tarde: respeita o env acima

    try:
        return SparseTextEmbedding(
            model_name=BM25_MODEL,
            cache_dir=str(cache_dir),
            language=BM25_LANGUAGE,
        )
    except Exception as exc:  # noqa: BLE001 - qualquer falha aqui é fatal e precisa de mensagem
        raise IndexError_(
            f"não foi possível carregar o modelo {BM25_MODEL} a partir de {cache_dir}.\n"
            f"Causa: {type(exc).__name__}: {exc}\n\n"
            "Esta máquina está em modo offline por padrão. Em um host com rede, rode:\n"
            f"    FASTEMBED_CACHE_DIR={cache_dir} ALLOW_MODEL_DOWNLOAD=1 "
            "python -m scripts.precache_models\n"
            f"e copie o diretório {cache_dir} para cá. Para permitir o download "
            "diretamente daqui, defina ALLOW_MODEL_DOWNLOAD=1."
        ) from exc


class KnowledgeIndex:
    def __init__(
        self,
        qdrant_url: str,
        collection: str,
        cache_dir: Path,
        *,
        allow_download: bool = False,
        timeout: int = 60,
    ) -> None:
        self.collection = collection
        self._client = QdrantClient(url=qdrant_url, timeout=timeout)
        self._cache_dir = cache_dir
        self._allow_download = allow_download
        self._model: Any | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> KnowledgeIndex:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @property
    def model(self) -> Any:
        if self._model is None:
            self._model = _load_sparse_model(self._cache_dir, self._allow_download)
        return self._model

    # -- schema ------------------------------------------------------------

    def check_server(self) -> str:
        info = self._client.info()
        version = getattr(info, "version", "") or ""
        parts = version.split(".")
        try:
            numeric = (int(parts[0]), int(parts[1]))
        except (IndexError, ValueError):
            LOG.warning("versão do Qdrant não reconhecida", extra={"versao": version})
            return version
        if numeric < MIN_QDRANT_VERSION:
            raise IndexError_(
                f"Qdrant {version} é antigo demais: o modificador IDF do vetor sparse "
                f"exige {MIN_QDRANT_VERSION[0]}.{MIN_QDRANT_VERSION[1]} ou superior. "
                "Atualize a tag da imagem no docker-compose.yml."
            )
        return version

    def ensure_collection(self, *, recreate: bool = False) -> bool:
        version = self.check_server()
        exists = self._client.collection_exists(self.collection)
        if exists and recreate:
            LOG.warning("recriando a coleção", extra={"collection": self.collection})
            self._client.delete_collection(self.collection)
            exists = False
        if not exists:
            self._client.create_collection(
                collection_name=self.collection,
                vectors_config={
                    # Declarado agora, populado só na Fase 2.
                    DENSE_VECTOR_NAME: models.VectorParams(
                        size=DENSE_VECTOR_SIZE, distance=models.Distance.COSINE
                    )
                },
                sparse_vectors_config={
                    SPARSE_VECTOR_NAME: models.SparseVectorParams(
                        modifier=models.Modifier.IDF
                    )
                },
            )
            LOG.info(
                "coleção criada",
                extra={"collection": self.collection, "qdrant": version},
            )
        for field_name in _PAYLOAD_KEYWORD_INDEXES:
            try:
                self._client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field_name,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
            except Exception as exc:  # noqa: BLE001 - índice já existente não é erro
                LOG.debug("índice de payload já existia", extra={"campo": field_name, "erro": str(exc)})
        return not exists

    # -- escrita -----------------------------------------------------------

    def _embed(self, texts: Sequence[str]) -> list[models.SparseVector]:
        folded = [fold_accents(text) for text in texts]
        return [
            models.SparseVector(indices=e.indices.tolist(), values=e.values.tolist())
            for e in self.model.embed(folded)
        ]

    def delete_document(self, doc_id: str) -> None:
        """Remove todas as fatias de um documento.

        Obrigatório antes de reinserir: se a página encurtou, as fatias de
        índice maior continuariam no Qdrant e apareceriam em buscas para
        sempre, com conteúdo que não existe mais.
        """
        self._client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="doc_id", match=models.MatchValue(value=doc_id)
                        )
                    ]
                )
            ),
            wait=True,
        )

    def delete_documents(self, doc_ids: Iterable[str]) -> int:
        removed = 0
        for doc_id in doc_ids:
            self.delete_document(doc_id)
            removed += 1
        return removed

    def index_document(self, document: Document, chunks: Sequence[Chunk]) -> int:
        self.delete_document(document.doc_id)
        if not chunks:
            return 0
        vectors = self._embed([chunk.text for chunk in chunks])
        points = [
            models.PointStruct(
                id=chunk.chunk_id,
                vector={SPARSE_VECTOR_NAME: vector},
                payload={
                    "doc_id": document.doc_id,
                    "chunk_index": chunk.index,
                    "source": document.source,
                    "content_type": document.content_type,
                    "title": document.title,
                    "url": document.url,
                    "updated": document.updated,
                    "project": document.project,
                    "status": document.status,
                    "issue_type": document.issue_type,
                    "space_key": document.space_key,
                    "labels": list(document.labels),
                    "heading": chunk.heading,
                    "text": chunk.text,
                },
            )
            for chunk, vector in zip(chunks, vectors)
        ]
        self._client.upsert(collection_name=self.collection, points=points, wait=False)
        return len(points)

    # -- leitura -----------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        source: str | None = None,
        project: str | None = None,
        space_key: str | None = None,
        status: str | None = None,
    ) -> list[SearchHit]:
        if not query.strip():
            return []
        embedded = list(self.model.query_embed(fold_accents(query)))
        if not embedded:
            return []
        sparse = models.SparseVector(
            indices=embedded[0].indices.tolist(), values=embedded[0].values.tolist()
        )

        conditions = [
            models.FieldCondition(key=key, match=models.MatchValue(value=value))
            for key, value in (
                ("source", source),
                ("project", project),
                ("space_key", space_key),
                ("status", status),
            )
            if value
        ]
        query_filter = models.Filter(must=conditions) if conditions else None

        response = self._client.query_points(
            collection_name=self.collection,
            query=sparse,
            using=SPARSE_VECTOR_NAME,
            limit=limit,
            query_filter=query_filter,
            with_payload=True,
        )
        hits: list[SearchHit] = []
        for point in response.points:
            payload = point.payload or {}
            hits.append(
                SearchHit(
                    score=float(point.score),
                    doc_id=str(payload.get("doc_id", "")),
                    chunk_id=str(point.id),
                    source=str(payload.get("source", "")),
                    title=str(payload.get("title", "")),
                    url=str(payload.get("url", "")),
                    text=str(payload.get("text", "")),
                    heading=payload.get("heading"),
                    project=payload.get("project"),
                    space_key=payload.get("space_key"),
                    status=payload.get("status"),
                    issue_type=payload.get("issue_type"),
                    updated=payload.get("updated"),
                    content_type=payload.get("content_type"),
                    labels=tuple(payload.get("labels") or ()),
                )
            )
        return hits

    def count(self) -> int:
        return int(self._client.count(self.collection, exact=True).count)
