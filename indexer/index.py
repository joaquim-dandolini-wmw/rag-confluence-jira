"""Índice no Qdrant: BM25 sparse (Fase 1) + vetor denso e fusão RRF (Fase 2).

O schema declarou os dois tipos de vetor desde a Fase 1, com o denso vazio, de
propósito: uma coleção do Qdrant não pode ganhar um novo tipo de vetor depois de
criada, então a Fase 2 popula o que já existe em vez de migrar.

Por que HÍBRIDO e não substituição: o BM25 é quem acerta identificador exato -
VENDAS-14993, PRUPSYNCPRODUTOS, ERR-4012 - e é exatamente onde o denso é ruim,
porque uma chave de projeto não tem vizinhança semântica. O denso resolve
sinônimo, onde o BM25 é ruim, porque "boleto" e "cobrança bancária" não
compartilham token. Os dois rankings são fundidos por RRF no SERVIDOR, via
prefetch + FusionQuery, e não no cliente: fundir aqui exigiria duas viagens e
reimplementar o que o Qdrant já faz.
"""

from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException

from config import (
    DEFAULT_RRF_K,
    DEFAULT_RRF_WEIGHT_BM25,
    DEFAULT_RRF_WEIGHT_DENSE,
    DENSE_VECTOR_NAME,
    DENSE_VECTOR_SIZE,
    SEARCH_MODES,
    SPARSE_VECTOR_NAME,
    EmbeddingConfig,
    RerankConfig,
)
from indexer.chunking import Chunk
from indexer.embeddings import DenseEmbedder
from indexer.reranking import CrossEncoderReranker
from store.documents import Document

LOG = logging.getLogger("indexer.index")

BM25_MODEL = "Qdrant/bm25"
BM25_LANGUAGE = "portuguese"
# O modificador IDF é calculado pelo servidor; sem ele o BM25 vira contagem
# bruta de termos e o ranking desmonta. Chegou em versões recentes do Qdrant.
MIN_QDRANT_VERSION = (1, 9)

_PAYLOAD_KEYWORD_INDEXES = ("source", "project", "space_key", "status", "doc_id")

# O cliente reaproveita conexões do pool e ocasionalmente pega uma que o
# servidor já fechou por keep-alive, o que chega como "connection reset by
# peer". Numa rodada de dezenas de milhares de documentos isso acontece; não
# é falha do Qdrant e não deve custar o documento.
_TRANSIENT_QDRANT = (ResponseHandlingException, ConnectionError, OSError)
_UPSERT_RETRIES = 3

# Quantas fatias buscar por vaga de resultado quando se deduplica por
# documento, mais uma folga fixa. A folga existe porque um único documento
# pode ocupar o lote inteiro: com identificador exato, as 6 primeiras fatias
# eram todas da mesma issue e `limit=2` devolvia 1 resultado só.
_DEDUPE_OVERFETCH = 5
_DEDUPE_FLOOR = 20
_DEDUPE_CAP = 300

# Identificador desta base: chave de projeto (VENDAS-14993, ERR-4012) ou nome
# de objeto de banco/procedure em caixa alta (PRUPSYNCPRODUTOS, VLCHAVE).
_IDENT_RE = re.compile(r"[A-Z][A-Z0-9_]*-\d+|\b[A-Z][A-Z0-9_]{5,}\b")


def classify_query(query: str) -> str:
    """Decide o modo do "auto": lexical, misto ou prosa.

    Existe porque a fusão RRF, medida nesta coleção, NÃO atende os dois
    critérios ao mesmo tempo - e nenhum peso, profundidade ou deduplicação
    resolve:

      identificador exato   BM25 acerta em 1º; o híbrido joga
                            PRUPSYNCPRODUTOS para fora do top-5
      sinônimo puro         o denso acha os 3 alvos (8/13/28); o BM25 não
                            acha nenhum; o híbrido acha 2 em 55/64

    Então em vez de um compromisso que perde nos dois, a consulta escolhe o
    instrumento. O BM25 é preciso e o denso é abrangente; a pergunta diz de
    qual dos dois ela precisa.
    """
    identificadores = _IDENT_RE.findall(query)
    if not identificadores:
        return "dense"
    # Só o identificador, ou quase: é busca lexical: "VENDAS-14993".
    resto = _IDENT_RE.sub(" ", query).split()
    if len(resto) <= 1:
        return "bm25"
    # Identificador no meio de uma frase: os dois contribuem.
    return "hybrid"


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
        embedding: EmbeddingConfig | None = None,
        embedder: DenseEmbedder | None = None,
        rerank: RerankConfig | None = None,
        reranker: CrossEncoderReranker | None = None,
    ) -> None:
        self.collection = collection
        self._client = QdrantClient(url=qdrant_url, timeout=timeout)
        self._cache_dir = cache_dir
        self._allow_download = allow_download
        self._model: Any | None = None
        self._embedding_cfg = embedding
        self._embedder = embedder
        # Modo efetivamente usado na última busca. Com "auto" o chamador não
        # sabe qual dos três rodou, e essa informação é o que permite a
        # comparação A/B e o diagnóstico de um resultado ruim.
        self.last_mode: str | None = None
        self._rerank_cfg = rerank
        self._reranker_obj = reranker
        # Se a última busca passou pelo cross-encoder. Uma falha do reranker
        # degrada para a ordem da primeira etapa em vez de quebrar a busca, e
        # é aqui que isso fica visível.
        self.last_reranked: bool = False

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

    @property
    def embedder(self) -> DenseEmbedder:
        """Carregado só quando alguém realmente pede o denso.

        Uma busca em modo bm25 não deve pagar 1,4 s de carga do e5 nem exigir
        GPU, e o `index` da Fase 1 continua funcionando sem torch instalado.
        """
        if self._embedder is None:
            if self._embedding_cfg is None:
                raise IndexError_(
                    "o vetor denso foi pedido mas nenhuma configuração de "
                    "embedding foi passada ao KnowledgeIndex. Use "
                    "load_config().embedding."
                )
            self._embedder = DenseEmbedder(self._embedding_cfg)
        return self._embedder

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

    def collection_exists(self) -> bool:
        return bool(self._client.collection_exists(self.collection))

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

    def _retry(self, what: str, action: Any, doc_id: str) -> Any:
        for attempt in range(_UPSERT_RETRIES):
            try:
                return action()
            except _TRANSIENT_QDRANT as exc:
                if attempt == _UPSERT_RETRIES - 1:
                    raise
                LOG.warning(
                    "falha transitória no Qdrant, tentando de novo",
                    extra={"operacao": what, "doc_id": doc_id,
                           "tentativa": attempt + 1, "erro": str(exc)},
                )
                time.sleep(0.5 * (attempt + 1))

    def index_document(self, document: Document, chunks: Sequence[Chunk]) -> int:
        self._retry("delete", lambda: self.delete_document(document.doc_id), document.doc_id)
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
        self._retry(
            "upsert",
            lambda: self._client.upsert(
                collection_name=self.collection, points=points, wait=False
            ),
            document.doc_id,
        )
        return len(points)

    def update_dense(
        self, vectors: Sequence[tuple[str, Sequence[float]]], doc_id: str
    ) -> int:
        """Escreve o vetor denso em pontos que JÁ EXISTEM.

        É update, não upsert: os chunk_id são uuid5 determinísticos, então
        casam com os pontos criados pelo `index`. Se o ponto não existir, o
        Qdrant não tem o que atualizar e o vetor se perde - por isso o
        `iter_pending_embed` só entrega documento já indexado.
        """
        if not vectors:
            return 0
        points = [
            models.PointVectors(id=chunk_id, vector={DENSE_VECTOR_NAME: list(vector)})
            for chunk_id, vector in vectors
        ]
        self._retry(
            "update_vectors",
            lambda: self._client.update_vectors(
                collection_name=self.collection, points=points, wait=False
            ),
            doc_id,
        )
        return len(points)

    def count_dense(self) -> int:
        """Pontos que já têm o vetor denso preenchido.

        Serve para detectar estado obsoleto: o embedded_hash mora no document
        store mas descreve o estado do QDRANT. Store e coleção podem divergir
        (store trazido de outra máquina, coleção recriada), e sem esta contagem
        o `embed` acreditaria que não há nada a fazer com o denso vazio.
        """
        return int(
            self._client.count(
                self.collection,
                count_filter=models.Filter(
                    must=[models.HasVectorCondition(has_vector=DENSE_VECTOR_NAME)]
                ),
                exact=True,
            ).count
        )

    # -- leitura -----------------------------------------------------------

    def _sparse_query(self, query: str) -> models.SparseVector | None:
        """Vetor sparse da consulta.

        fold_accents() aqui é obrigatório e simétrico com a indexação: o
        tokenizer do BM25 não normaliza diacrítico. NÃO se aplica ao denso.
        """
        embedded = list(self.model.query_embed(fold_accents(query)))
        if not embedded:
            return None
        return models.SparseVector(
            indices=embedded[0].indices.tolist(), values=embedded[0].values.tolist()
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        source: str | None = None,
        project: str | None = None,
        space_key: str | None = None,
        status: str | None = None,
        mode: str = "hybrid",
        prefetch_limit: int | None = None,
        dedupe_by_document: bool = True,
        bm25_weight: float = DEFAULT_RRF_WEIGHT_BM25,
        dense_weight: float = DEFAULT_RRF_WEIGHT_DENSE,
        rrf_k: int = DEFAULT_RRF_K,
        rerank: bool | None = None,
    ) -> list[SearchHit]:
        if mode not in SEARCH_MODES:
            raise IndexError_(
                f"modo de busca {mode!r} desconhecido. Aceitos: "
                f"{', '.join(SEARCH_MODES)}."
            )
        if not query.strip():
            return []
        if mode == "auto":
            mode = classify_query(query)
            LOG.debug("modo escolhido pelo auto", extra={"modo": mode})
        self.last_mode = mode

        # Consulta lexical não passa pelo reranker. Medido: o cross-encoder
        # julga relevância semântica do par, e um identificador quase não tem
        # semântica - "VENDAS-14993" caiu de 1º para 2º e "PRUPSYNCPRODUTOS"
        # saiu do top-5. Quando o usuário digita a chave exata, o que ele quer
        # é exatidão, e disso o BM25 já dá conta.
        usar_rerank = self._quer_rerank(rerank) and mode != "bm25"
        self.last_reranked = usar_rerank
        # Com reranker, a primeira etapa é um FILTRO, não a resposta: ela traz
        # candidatos e o cross-encoder decide a ordem. Sem ele, a primeira
        # etapa já é a resposta.
        alvo = self._reranker.candidates if usar_rerank else limit

        # Sem deduplicar, um documento longo ocupa várias vagas com fatias
        # vizinhas: medido, 3 de 5 resultados vinham de 3 documentos só. Para
        # quem consome (o modelo, via MCP) isso é contexto desperdiçado. Busca-se
        # mais fundo e devolve-se a MELHOR fatia de cada documento. Deduplicar
        # ANTES do reranker também evita gastar forward em fatias do mesmo
        # documento, que é a parte cara da consulta.
        fetch = (
            min(alvo * _DEDUPE_OVERFETCH + _DEDUPE_FLOOR, _DEDUPE_CAP)
            if dedupe_by_document
            else alvo
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

        # As duas pernas NÃO buscam na mesma profundidade, e isso é o ajuste que
        # mais importa na fusão. Medido nesta coleção, com o documento-alvo de
        # uma consulta em sinônimo puro:
        #
        #   bm25=50 denso=50   -> alvo em 46/54/não-achou
        #   bm25= 5 denso=100  -> alvo em 17/20/36, identificador exato intacto
        #
        # O motivo é o mecanismo do RRF: ele soma 1/(k+rank), então cada
        # candidato que o BM25 traz ocupa uma posição boa mesmo sendo
        # irrelevante para uma consulta em prosa, e dilui o acerto do denso. O
        # BM25 é instrumento de PRECISÃO - só as primeiras posições dele valem;
        # o denso é instrumento de RECALL - precisa de profundidade.
        dense_deep = prefetch_limit or max(fetch * 10, 100)
        bm25_deep = prefetch_limit or max(fetch, 5)

        if mode == "bm25":
            sparse = self._sparse_query(query)
            if sparse is None:
                return []
            response = self._client.query_points(
                collection_name=self.collection,
                query=sparse,
                using=SPARSE_VECTOR_NAME,
                limit=fetch,
                query_filter=query_filter,
                with_payload=True,
            )
        elif mode == "dense":
            response = self._client.query_points(
                collection_name=self.collection,
                query=self.embedder.embed_query(query),
                using=DENSE_VECTOR_NAME,
                limit=fetch,
                query_filter=query_filter,
                with_payload=True,
            )
        else:
            sparse = self._sparse_query(query)
            # O filtro vai DENTRO de cada prefetch: a fusão só reordena ids já
            # trazidos, então filtrar apenas no topo deixaria cada perna gastar
            # suas vagas com pontos fora do escopo.
            dense_leg = models.Prefetch(
                query=self.embedder.embed_query(query),
                using=DENSE_VECTOR_NAME,
                filter=query_filter,
                limit=dense_deep,
                # hnsw_ef alto de propósito: com o padrão, duas execuções da
                # MESMA consulta devolviam ordens diferentes, porque a busca
                # aproximada trocava o conjunto trazido e a fusão desempatava
                # ao acaso. Medido nesta coleção de 148 mil pontos.
                params=models.SearchParams(hnsw_ef=max(dense_deep * 2, 256)),
            )
            prefetch = [dense_leg]
            pesos = [dense_weight]
            if sparse is not None:
                prefetch.insert(
                    0,
                    models.Prefetch(
                        query=sparse,
                        using=SPARSE_VECTOR_NAME,
                        filter=query_filter,
                        limit=bm25_deep,
                    ),
                )
                pesos.insert(0, bm25_weight)
            response = self._client.query_points(
                collection_name=self.collection,
                prefetch=prefetch,
                # RRF PONDERADO, não o RRF simples. Com peso igual o denso
                # derruba o acerto exato do BM25: para "VENDAS-14993" o denso
                # traz VENDAS-12493 e VENDAS-15493 (tokens numericamente
                # parecidos, sem vizinhança semântica real) e a fusão promove
                # um deles. O peso maior no BM25 preserva o identificador sem
                # abrir mão do sinônimo, que é o que o denso acrescenta.
                query=models.RrfQuery(
                    rrf=models.Rrf(k=rrf_k, weights=pesos)
                ),
                limit=fetch,
                with_payload=True,
            )

        hits: list[SearchHit] = []
        vistos: set[str] = set()
        for point in response.points:
            payload = point.payload or {}
            doc_id = str(payload.get("doc_id", ""))
            if dedupe_by_document:
                if doc_id in vistos:
                    continue
                vistos.add(doc_id)
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
            if len(hits) == alvo:
                break

        if not usar_rerank:
            return hits
        return self._aplica_rerank(query, hits, limit)

    # -- reranking ---------------------------------------------------------

    def _quer_rerank(self, pedido: bool | None) -> bool:
        if pedido is False:
            return False
        if self._reranker is None and self._rerank_cfg is None:
            return False
        if pedido is True:
            return True
        return self._reranker.enabled if self._reranker else bool(
            self._rerank_cfg and self._rerank_cfg.enabled
        )

    @property
    def _reranker(self) -> CrossEncoderReranker | None:
        if self._reranker_obj is None and self._rerank_cfg is not None:
            self._reranker_obj = CrossEncoderReranker(self._rerank_cfg)
        return self._reranker_obj

    def _aplica_rerank(
        self, query: str, hits: list[SearchHit], limit: int
    ) -> list[SearchHit]:
        reranker = self._reranker
        if reranker is None:
            return hits[:limit]
        try:
            ordenados = reranker.rerank(
                query, hits, text_of=lambda hit: hit.text, limit=limit
            )
        except Exception as exc:  # noqa: BLE001 - busca degradada é melhor que busca quebrada
            LOG.warning(
                "reranker falhou, devolvendo a ordem da primeira etapa",
                extra={"erro": str(exc), "candidatos": len(hits)},
            )
            self.last_reranked = False
            return hits[:limit]
        # O score passa a ser o do cross-encoder: é ele que define a ordem, e
        # devolver o score da primeira etapa faria a lista parecer desordenada.
        return [replace(hit, score=pontos) for hit, pontos in ordenados]

    def count(self) -> int:
        return int(self._client.count(self.collection, exact=True).count)
