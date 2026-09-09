"""CLI de orquestração do sync.

    python -m indexer.sync extract --only confluence
    python -m indexer.sync extract --only jira --full
    python -m indexer.sync index
    python -m indexer.sync embed
    python -m indexer.sync run
    python -m indexer.sync reconcile --only jira
    python -m indexer.sync scope
    python -m indexer.sync search "boleto do pedido" --mode hybrid
    python -m indexer.sync status

Extração e indexação são etapas separadas de propósito: a extração fala com
as instâncias Atlassian e é cara; a indexação lê só o store local. Trocar o
tamanho da fatia ou reconstruir o índice não toca em nada remoto.

O `embed` é uma terceira etapa pelo mesmo motivo: o BM25 é calculado no cliente
em microssegundos, o vetor denso custa GPU. Reindexar o sparse não pode obrigar
a reembedar. A ordem em `run` é extract -> index -> embed, porque o embed faz
UPDATE do vetor denso em ponto que já existe.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Sequence

from config import (
    DEFAULT_EMBED_BATCH_SIZE,
    ConfluenceConfig,
    EMBED_DEVICES,
    SEARCH_MODES,
    Config,
    ConfigError,
    EmbeddingConfig,
    load_config,
    setup_logging,
)
from connectors.confluence_legacy import (
    ConfluenceAuthError,
    ConfluenceClient,
    ConfluenceExtractor,
    SpaceExtraction,
)
from connectors.jira_server import (
    JiraAuthError,
    JiraClient,
    JiraExtraction,
    JiraExtractor,
    JiraRequestError,
)
from indexer.chunking import DEFAULT_MAX_CHARS, DEFAULT_OVERLAP, chunk_document
from indexer.embeddings import DenseEmbedder, EmbeddingError
from indexer.index import IndexError_, KnowledgeIndex
from store.documents import (
    SOURCE_CONFLUENCE,
    SOURCE_JIRA,
    Document,
    DocumentStore,
)

LOG = logging.getLogger("indexer.sync")

COMMIT_EVERY = 25

# Quantos batches de embedding são acumulados antes de descarregar no Qdrant.
# Com EMBED_BATCH_SIZE=16 dá 1.024 fatias por descarga: material suficiente
# para a ordenação por comprimento reduzir padding, e ~230 documentos de
# progresso em risco numa queda, o que é aceitável.
FLUSH_BATCHES = 64
# Pontos por requisição de update_vectors. 1.024 vetores de 1.024 floats numa
# só requisição passam de 20 MB de JSON; 256 mantém o payload em poucos MB.
UPDATE_BATCH = 256


def _open_index(
    cfg: Config,
    *,
    embedding: EmbeddingConfig | None = None,
    embedder: DenseEmbedder | None = None,
) -> KnowledgeIndex:
    return KnowledgeIndex(
        cfg.qdrant_url,
        cfg.collection,
        cfg.fastembed_cache_dir,
        allow_download=cfg.allow_model_download,
        embedding=embedding or cfg.embedding,
        embedder=embedder,
        rerank=cfg.rerank,
    )


# --------------------------------------------------------------------------
# escopo do Confluence
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ScopeDiff:
    """O que o escopo tem hoje, o que entrou e o que saiu."""

    scope: tuple[str, ...]
    entered: tuple[str, ...]
    left: tuple[str, ...]
    discovered: bool

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "espacos_no_escopo": len(self.scope),
            "entraram": list(self.entered),
            "sairam": list(self.left),
            "descoberto": self.discovered,
        }


def resolve_confluence_scope(
    conf: ConfluenceConfig, client: ConfluenceClient, store: DocumentStore
) -> ScopeDiff:
    """Decide quais espaços indexar e compara com o que já está no store.

    Com CONFLUENCE_SPACES=auto o escopo vem do getSpaces(), que devolve
    exatamente o que o usuário de serviço enxerga - ou seja, o Confluence já
    aplicou as permissões de grupo dele. Trocar o usuário de grupo muda o
    escopo sozinho, sem tocar em arquivo de configuração. Medido nesta
    instância: timedesenv enxerga 716 espaços, wmw-rag enxerga 30.
    """
    if conf.discover:
        visiveis = {str(e.get("key")) for e in client.get_spaces() if e.get("key")}
        if not visiveis:
            # NUNCA tratar lista vazia como "nada no escopo": seria apagar o
            # índice inteiro por causa de uma falha transitória do XML-RPC.
            raise ConfluenceAuthError(
                "getSpaces() devolveu zero espaços. Isso é falha de conexão ou "
                "de permissão, não escopo vazio - abortando antes de remover "
                "conteúdo por engano."
            )
        escopo = visiveis - set(conf.exclude_spaces)
    else:
        escopo = set(conf.spaces)

    no_store = store.confluence_space_keys()
    return ScopeDiff(
        scope=tuple(sorted(escopo)),
        entered=tuple(sorted(escopo - no_store)),
        left=tuple(sorted(no_store - escopo)),
        discovered=conf.discover,
    )


def purge_spaces(store: DocumentStore, espacos: Sequence[str]) -> dict[str, int]:
    """Remove do store o conteúdo de espaços que saíram do escopo.

    Sem isto, um espaço tirado do escopo continuaria pesquisável para sempre:
    o extract só visita o que está no escopo, então ninguém mais olharia para
    aquele conteúdo. O delete() do store também limpa o confluence_versions,
    então um espaço que volte é reextraído do zero.
    """
    total = 0
    for space_key in espacos:
        doc_ids = sorted(
            store.list_doc_ids(source=SOURCE_CONFLUENCE, space_key=space_key)
        )
        if not doc_ids:
            continue
        store.delete(doc_ids)
        store.queue_index_deletions(doc_ids)
        store.commit()
        total += len(doc_ids)
        LOG.info(
            "espaço fora do escopo, conteúdo removido",
            extra={"space_key": space_key, "documentos": len(doc_ids)},
        )
    return {"espacos": len(espacos), "documentos": total}


# --------------------------------------------------------------------------
# extração
# --------------------------------------------------------------------------

def extract_confluence(
    cfg: Config, store: DocumentStore, *, full: bool, count_attachments: bool,
    fetch_labels: bool = True,
) -> dict[str, Any]:
    conf = cfg.require_confluence()
    totals = {
        "espacos": 0, "paginas_listadas": 0, "blogs_listados": 0, "buscados": 0,
        "pulados_sem_mudanca": 0, "gravados": 0, "falhas": 0, "removidos": 0,
        "anexos": 0, "anexos_indisponiveis": 0,
        "tempo_busca_s": 0.0, "tempo_anexos_s": 0.0,
    }
    started = time.monotonic()

    with ConfluenceClient(conf) as client:
        extractor = ConfluenceExtractor(client, conf)
        known_versions = store.confluence_versions()

        diff = resolve_confluence_scope(conf, client, store)
        LOG.info("escopo do Confluence resolvido", extra=diff.as_log_fields())
        totals["escopo"] = len(diff.scope)
        totals["espacos_novos"] = len(diff.entered)
        if diff.left:
            removidos = purge_spaces(store, diff.left)
            totals["removidos"] += removidos["documentos"]
            totals["espacos_removidos"] = removidos["espacos"]

        for space_key in diff.scope:
            result = SpaceExtraction(space_key=space_key)
            written = 0
            try:
                for document, version in extractor.iter_space(
                    space_key,
                    known_versions,
                    result,
                    force=full,
                    count_attachments=count_attachments,
                    fetch_labels=fetch_labels,
                ):
                    store.upsert(document)
                    store.set_confluence_version(document.doc_id, version)
                    written += 1
                    if written % COMMIT_EVERY == 0:
                        store.commit()
            finally:
                # Progresso parcial é progresso: a próxima rodada não recomeça.
                store.commit()

            if result.listing_complete:
                stale = store.list_doc_ids(
                    source=SOURCE_CONFLUENCE, space_key=space_key
                ) - result.seen_doc_ids
                if stale:
                    store.delete(sorted(stale))
                    store.queue_index_deletions(sorted(stale))
                    store.commit()
                    totals["removidos"] += len(stale)
                    LOG.info(
                        "conteúdo removido no Confluence, retirado do store",
                        extra={"space_key": space_key, "removidos": len(stale)},
                    )
            else:
                LOG.warning(
                    "listagem incompleta: diff de deleções pulado neste espaço",
                    extra={"space_key": space_key},
                )

            totals["espacos"] += 1
            totals["gravados"] += written
            for key in (
                "paginas_listadas", "blogs_listados", "buscados", "pulados_sem_mudanca",
                "falhas", "anexos", "anexos_indisponiveis", "tempo_busca_s", "tempo_anexos_s",
            ):
                totals[key] += result.as_log_fields()[key]
            LOG.info("espaço concluído", extra=result.as_log_fields())

    totals["tempo_total_s"] = round(time.monotonic() - started, 2)
    totals["media_por_getpage_ms"] = round(
        (totals["tempo_busca_s"] / totals["buscados"] * 1000) if totals["buscados"] else 0.0, 1
    )
    LOG.info("extração do Confluence concluída", extra=totals)
    return totals


def extract_jira(
    cfg: Config, store: DocumentStore, *, full: bool, include_comments: bool
) -> dict[str, Any]:
    jira = cfg.require_jira()
    metrics = JiraExtraction()
    started = time.monotonic()

    with JiraClient(jira) as client:
        extractor = JiraExtractor(client, jira)
        since = None if full else store.jira_last_updated
        processed = 0
        try:
            for document in extractor.iter_documents(
                since, metrics, include_comments=include_comments
            ):
                if store.upsert(document):
                    metrics.issues_changed += 1
                processed += 1
                if processed % COMMIT_EVERY == 0:
                    # As issues chegam ordenadas por updated ASC, então salvar
                    # o cursor no meio da rodada é seguro: tudo antes dele já
                    # está gravado.
                    if metrics.max_updated:
                        store.jira_last_updated = metrics.max_updated
                    store.commit()
        finally:
            if metrics.max_updated:
                store.jira_last_updated = metrics.max_updated
            store.commit()

    totals = metrics.as_log_fields()
    totals["tempo_total_s"] = round(time.monotonic() - started, 2)
    LOG.info("extração do Jira concluída", extra=totals)
    return totals


# --------------------------------------------------------------------------
# indexação
# --------------------------------------------------------------------------

def run_index(
    cfg: Config,
    store: DocumentStore,
    *,
    recreate: bool = False,
    reindex_all: bool = False,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> dict[str, Any]:
    started = time.monotonic()
    totals = {"documentos": 0, "fatias": 0, "removidos": 0, "falhas": 0}

    with _open_index(cfg) as index:
        index.ensure_collection(recreate=recreate)
        if reindex_all or recreate:
            store.mark_all_unindexed()
            # Reindexar apaga e recria os pontos do documento, e o ponto novo
            # nasce só com o sparse. Sem invalidar o embedded_hash junto, o
            # `embed` acharia que já embedou e o vetor denso ficaria vazio para
            # sempre - sem erro nenhum, o que é pior.
            store.mark_all_unembedded()
            store.commit()
        elif index.count() == 0 and store.count_indexed() > 0:
            # O indexed_hash mora no store, mas descreve o estado do Qdrant.
            # Ao mover o store para outra máquina - que é justamente o caminho
            # barato de reconstruir o índice - ele chega afirmando que tudo já
            # foi indexado enquanto a coleção nova está vazia, e a indexação
            # não faria nada. Coleção vazia com store cheio só pode ser isso.
            LOG.warning(
                "índice vazio mas o store diz que já foi indexado; "
                "marcando tudo como pendente",
                extra={"documentos_marcados": store.count_indexed()},
            )
            store.mark_all_unindexed()
            store.mark_all_unembedded()
            store.commit()

        pending_deletions = store.take_index_deletions()
        store.commit()
        for doc_id in pending_deletions:
            index.delete_document(doc_id)
            totals["removidos"] += 1

        processed = 0
        try:
            for document in store.iter_pending_index():
                chunks = chunk_document(
                    document.doc_id,
                    document.title,
                    document.body_text,
                    max_chars=max_chars,
                    overlap=overlap,
                )
                try:
                    written = index.index_document(document, chunks)
                except Exception as exc:  # noqa: BLE001 - uma página ruim não derruba a rodada
                    totals["falhas"] += 1
                    LOG.warning(
                        "documento não pôde ser indexado, seguindo",
                        extra={"doc_id": document.doc_id, "erro": str(exc)},
                    )
                    continue
                store.mark_indexed(document.doc_id, document.content_hash())
                totals["documentos"] += 1
                totals["fatias"] += written
                processed += 1
                if processed % COMMIT_EVERY == 0:
                    store.commit()
        finally:
            store.commit()

        totals["pontos_no_indice"] = index.count()

    totals["tempo_total_s"] = round(time.monotonic() - started, 2)
    LOG.info("indexação concluída", extra=totals)
    return totals


# --------------------------------------------------------------------------
# vetor denso (Fase 2)
# --------------------------------------------------------------------------

def _embed_um_a_um(
    index: KnowledgeIndex,
    embedder: DenseEmbedder,
    store: DocumentStore,
    lote: Sequence[tuple[Document, Sequence[Any]]],
    totals: dict[str, Any],
) -> None:
    """Reprocessa um lote que falhou, documento a documento.

    Existe para que um documento ruim não custe os outros 230 do lote. Aqui a
    falha é por documento: loga o doc_id e segue, como no `index`.
    """
    for document, chunks in lote:
        try:
            vectors = embedder.embed_passages([chunk.text for chunk in chunks])
            written = index.update_dense(
                list(zip((chunk.chunk_id for chunk in chunks), vectors)),
                document.doc_id,
            )
        except Exception as exc:  # noqa: BLE001 - um documento ruim não derruba a rodada
            totals["falhas"] += 1
            LOG.warning(
                "documento não pôde ser embedado, seguindo",
                extra={"doc_id": document.doc_id, "erro": str(exc)},
            )
            continue
        store.mark_embedded(document.doc_id, document.content_hash())
        totals["documentos"] += 1
        totals["fatias"] += written


def run_embed(
    cfg: Config,
    store: DocumentStore,
    *,
    reembed_all: bool = False,
    device: str | None = None,
    batch_size: int | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> dict[str, Any]:
    """Popula o vetor denso dos pontos que o `index` já criou.

    Não recria a coleção e não faz upsert: os chunk_id são uuid5
    determinísticos, então o que acontece aqui é UPDATE do vetor denso nos
    148 mil pontos que já existem. Recriar a coleção jogaria fora o BM25.
    """
    embed_cfg = cfg.embedding
    if device:
        embed_cfg = replace(embed_cfg, device=device)
    if batch_size:
        embed_cfg = replace(embed_cfg, batch_size=batch_size)

    started = time.monotonic()
    totals: dict[str, Any] = {
        "documentos": 0, "fatias": 0, "falhas": 0,
        "device": embed_cfg.device, "batch_size": embed_cfg.batch_size,
        "modelo": embed_cfg.model_name,
    }

    embedder = DenseEmbedder(embed_cfg)
    with _open_index(cfg, embedding=embed_cfg, embedder=embedder) as index:
        if not index.collection_exists():
            raise IndexError_(
                f"a coleção {cfg.collection!r} não existe. Rode "
                "`python -m indexer.sync index` primeiro: o `embed` atualiza o "
                "vetor denso de pontos existentes, não cria pontos."
            )
        if reembed_all:
            store.mark_all_unembedded()
            store.commit()
        else:
            com_denso = index.count_dense()
            if com_denso == 0 and store.count_embedded() > 0:
                # Mesma armadilha do indexed_hash: o embedded_hash mora no
                # store e descreve o estado do Qdrant. Store trazido de outra
                # máquina, ou coleção recriada, chega afirmando que tudo já foi
                # embedado com o denso vazio - e o comando não faria nada.
                LOG.warning(
                    "nenhum ponto tem vetor denso mas o store diz que já foi "
                    "embedado; marcando tudo como pendente",
                    extra={"documentos_marcados": store.count_embedded()},
                )
                store.mark_all_unembedded()
                store.commit()

        pendentes = store.count_pending_embed()
        LOG.info(
            "embed iniciado",
            extra={"pendentes": pendentes, "device": embed_cfg.device,
                   "batch_size": embed_cfg.batch_size},
        )
        if pendentes:
            # Paga a carga do modelo e o primeiro kernel fora da medição, para
            # que as fatias/s reportadas sejam de regime e não de aquecimento.
            embedder.warmup()

        # Múltiplo do batch para que nenhum batch saia pela metade, e grande o
        # bastante para a ordenação por comprimento ter material com que
        # trabalhar. Também é a granularidade do progresso salvo: uma queda
        # custa no máximo este lote.
        flush_chunks = max(embed_cfg.batch_size * FLUSH_BATCHES, embed_cfg.batch_size)
        totals["flush_fatias"] = flush_chunks

        # O lote atravessa documentos, e não para no fim de cada um. A mediana
        # é de 4,4 fatias por documento: embedar documento a documento nunca
        # encheria o batch e o overhead por chamada dominaria - medido, 13
        # fatias/s contra 89. Acumular também melhora o padding, porque o
        # sentence-transformers ordena o lote inteiro por comprimento antes de
        # fatiar em batches.
        lote: list[tuple[Document, list[Any]]] = []
        fatias_no_lote = 0

        def descarregar() -> None:
            """Embeda o lote acumulado e grava o progresso dele."""
            nonlocal fatias_no_lote
            if not lote:
                return
            textos = [chunk.text for _, chunks in lote for chunk in chunks]
            ids = [chunk.chunk_id for _, chunks in lote for chunk in chunks]
            try:
                vectors = embedder.embed_passages(textos)
                for inicio in range(0, len(ids), UPDATE_BATCH):
                    fim = inicio + UPDATE_BATCH
                    index.update_dense(
                        list(zip(ids[inicio:fim], vectors[inicio:fim])),
                        lote[0][0].doc_id,
                    )
            except Exception as exc:  # noqa: BLE001 - o lote cai para documento a documento
                LOG.warning(
                    "lote falhou, reprocessando documento a documento",
                    extra={"documentos_no_lote": len(lote), "erro": str(exc)},
                )
                _embed_um_a_um(index, embedder, store, lote, totals)
            else:
                for document, chunks in lote:
                    store.mark_embedded(document.doc_id, document.content_hash())
                    totals["documentos"] += 1
                    totals["fatias"] += len(chunks)
            lote.clear()
            fatias_no_lote = 0
            store.commit()
            # Uma carga inicial leva dezenas de minutos. Sem esta linha o
            # comando fica silencioso o tempo todo e não há como saber se
            # avança, qual o ritmo nem quanto falta - mesmo motivo da linha de
            # progresso da extração do Confluence.
            decorrido = time.monotonic() - started
            ritmo = totals["fatias"] / decorrido if decorrido > 0 else 0.0
            restantes = max(pendentes - totals["documentos"], 0)
            LOG.info(
                "progresso do embed",
                extra={
                    "documentos": totals["documentos"],
                    "de": pendentes,
                    "pct": round(100 * totals["documentos"] / pendentes, 1)
                    if pendentes
                    else 100.0,
                    "fatias": totals["fatias"],
                    "fatias_por_s": round(ritmo, 1),
                    "falhas": totals["falhas"],
                    "eta_min": round(
                        restantes
                        * (totals["fatias"] / max(totals["documentos"], 1))
                        / ritmo
                        / 60,
                        1,
                    )
                    if ritmo > 0
                    else None,
                },
            )

        try:
            for document in store.iter_pending_embed():
                chunks = chunk_document(
                    document.doc_id,
                    document.title,
                    document.body_text,
                    max_chars=max_chars,
                    overlap=overlap,
                )
                if not chunks:
                    store.mark_embedded(document.doc_id, document.content_hash())
                    continue
                lote.append((document, chunks))
                fatias_no_lote += len(chunks)
                if fatias_no_lote >= flush_chunks:
                    descarregar()
            descarregar()
        finally:
            # Progresso parcial é progresso: uma queda no meio não pode fazer a
            # próxima rodada começar do zero.
            store.commit()

        totals["pontos_com_denso"] = index.count_dense()

    elapsed = time.monotonic() - started
    totals["tempo_total_s"] = round(elapsed, 2)
    totals["fatias_por_segundo"] = (
        round(totals["fatias"] / elapsed, 1) if elapsed > 0 else 0.0
    )
    LOG.info("embed concluído", extra=totals)
    return totals


# --------------------------------------------------------------------------
# reconciliação de deleções
# --------------------------------------------------------------------------

def reconcile_jira(cfg: Config, store: DocumentStore) -> dict[str, Any]:
    """Detecta issues apagadas ou movidas para fora do escopo.

    Fora do caminho de 15 minutos: varre TODAS as chaves do escopo, o que é
    barato em payload mas caro em número de requisições.
    """
    jira = cfg.require_jira()
    with JiraClient(jira) as client:
        live = {f"jira:{key}" for key in client.iter_all_keys(jira.projects)}
    stored = store.list_doc_ids(source=SOURCE_JIRA)
    stale = sorted(stored - live)
    if stale:
        store.delete(stale)
        store.queue_index_deletions(stale)
        store.commit()
    LOG.info(
        "reconciliação do Jira concluída",
        extra={"no_jira": len(live), "no_store": len(stored), "removidos": len(stale)},
    )
    return {"no_jira": len(live), "no_store": len(stored), "removidos": len(stale)}


def reconcile_confluence(cfg: Config, store: DocumentStore) -> dict[str, Any]:
    """Diff de IDs por espaço, sem buscar conteúdo: só getPages/getBlogEntries."""
    conf = cfg.require_confluence()
    live: set[str] = set()
    with ConfluenceClient(conf) as client:
        for space_key in resolve_confluence_scope(conf, client, store).scope:
            for summary in client.get_page_summaries(space_key):
                live.add(f"confluence:page:{summary.get('id')}")
            for summary in client.get_blog_summaries(space_key):
                live.add(f"confluence:blog:{summary.get('id')}")
    stored = store.list_doc_ids(source=SOURCE_CONFLUENCE)
    stale = sorted(stored - live)
    if stale:
        store.delete(stale)
        store.queue_index_deletions(stale)
        store.commit()
    LOG.info(
        "reconciliação do Confluence concluída",
        extra={"no_confluence": len(live), "no_store": len(stored), "removidos": len(stale)},
    )
    return {"no_confluence": len(live), "no_store": len(stored), "removidos": len(stale)}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _sources(only: str | None, cfg: Config) -> list[str]:
    if only:
        return [only]
    available = []
    if cfg.confluence is not None:
        available.append(SOURCE_CONFLUENCE)
    if cfg.jira is not None:
        available.append(SOURCE_JIRA)
    return available


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="indexer.sync", description=__doc__)
    parser.add_argument("--log-level", default="INFO")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_scope(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--only", choices=[SOURCE_CONFLUENCE, SOURCE_JIRA])

    def add_extract_flags(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--full", action="store_true",
            help="ignora o estado incremental e reextrai tudo",
        )
        sub.add_argument(
            "--count-attachments", dest="count_attachments",
            action=argparse.BooleanOptionalAction, default=True,
            help="conta anexos por espaço (custa +1 chamada XML-RPC por página)",
        )
        sub.add_argument(
            "--fetch-labels", dest="fetch_labels",
            action=argparse.BooleanOptionalAction, default=True,
            help="busca labels das páginas (custa +1 chamada XML-RPC por página)",
        )
        sub.add_argument(
            "--include-comments", dest="include_comments",
            action=argparse.BooleanOptionalAction, default=True,
            help="busca os comentários completos de cada issue do Jira",
        )

    extract = subparsers.add_parser("extract", help="extrai para o document store")
    add_scope(extract)
    add_extract_flags(extract)

    index_cmd = subparsers.add_parser("index", help="indexa o document store no Qdrant")
    index_cmd.add_argument("--reindex-all", action="store_true")
    index_cmd.add_argument("--recreate", action="store_true", help="apaga e recria a coleção")
    index_cmd.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    index_cmd.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)

    embed_cmd = subparsers.add_parser(
        "embed", help="popula o vetor denso dos pontos já indexados"
    )
    embed_cmd.add_argument(
        "--reembed-all", action="store_true",
        help="ignora o progresso e reembeda tudo a partir do store",
    )
    embed_cmd.add_argument(
        "--device", choices=EMBED_DEVICES,
        help="sobrepõe EMBED_DEVICE (o PyTorch ROCm usa \"cuda\" para a AMD)",
    )
    embed_cmd.add_argument(
        "--batch-size", type=int,
        help=f"sobrepõe EMBED_BATCH_SIZE (padrão medido: {DEFAULT_EMBED_BATCH_SIZE})",
    )
    embed_cmd.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    embed_cmd.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)

    run_cmd = subparsers.add_parser("run", help="extract + index + embed")
    add_scope(run_cmd)
    add_extract_flags(run_cmd)
    run_cmd.add_argument(
        "--no-embed", dest="embed", action="store_false", default=True,
        help="para depois do index, sem popular o vetor denso",
    )
    run_cmd.add_argument("--device", choices=EMBED_DEVICES)
    run_cmd.add_argument("--batch-size", type=int)

    reconcile = subparsers.add_parser("reconcile", help="remove o que sumiu na origem")
    add_scope(reconcile)

    search_cmd = subparsers.add_parser(
        "search", help="consulta o índice; serve para comparar os modos A/B"
    )
    search_cmd.add_argument("query")
    search_cmd.add_argument("--mode", choices=SEARCH_MODES, default="auto")
    search_cmd.add_argument("--limit", type=int, default=5)
    search_cmd.add_argument("--source", choices=[SOURCE_CONFLUENCE, SOURCE_JIRA])
    search_cmd.add_argument("--project")
    search_cmd.add_argument("--space-key", dest="space_key")
    search_cmd.add_argument(
        "--rerank", dest="rerank", action=argparse.BooleanOptionalAction,
        default=None, help="liga/desliga o reranker (padrão: RERANK_ENABLED)",
    )

    scope_cmd = subparsers.add_parser(
        "scope", help="mostra quais espaços entraram e saíram do escopo"
    )
    scope_cmd.add_argument(
        "--apply", action="store_true",
        help="remove o conteúdo dos espaços que saíram (o padrão só analisa)",
    )

    subparsers.add_parser("status", help="mostra o estado do store e do índice")
    return parser


def _run_scope(cfg: Config, store: DocumentStore, aplicar: bool) -> None:
    conf = cfg.require_confluence()
    with ConfluenceClient(conf) as client:
        diff = resolve_confluence_scope(conf, client, store)

    origem = "getSpaces() do usuário" if diff.discovered else "CONFLUENCE_SPACES"
    print(f"origem do escopo:  {origem}")
    print(f"usuário:           {conf.user}")
    print(f"espaços no escopo: {len(diff.scope)}")
    if conf.exclude_spaces:
        print(f"excluídos à mão:   {', '.join(conf.exclude_spaces)}")

    no_store = store.confluence_space_keys()
    mantidos = sorted(no_store & set(diff.scope))
    print("")
    print(f"JÁ INDEXADOS, seguem ({len(mantidos)}):")
    for k in mantidos:
        print(f"   = {k}")

    print("")
    print(f"ENTRARAM, serão indexados ({len(diff.entered)}):")
    for k in diff.entered:
        print(f"   + {k}")
    if not diff.entered:
        print("   (nenhum)")

    print("")
    print(f"SAÍRAM, conteúdo a remover ({len(diff.left)}):")
    total = 0
    for k in diff.left:
        n = len(store.list_doc_ids(source=SOURCE_CONFLUENCE, space_key=k))
        total += n
        print(f"   - {k}  ({n} documentos)")
    if not diff.left:
        print("   (nenhum)")

    print("")
    if aplicar and diff.left:
        resultado = purge_spaces(store, diff.left)
        print(f"removidos {resultado['documentos']} documentos de "
              f"{resultado['espacos']} espaços do store.")
        print("rode `index` para tirá-los do Qdrant também.")
    elif diff.left:
        print(f"{total} documentos seriam removidos. Rode com --apply.")
    else:
        print("nada a remover.")


def _run_search(cfg: Config, args: argparse.Namespace) -> None:
    started = time.monotonic()
    with _open_index(cfg) as index:
        hits = index.search(
            args.query,
            limit=args.limit,
            mode=args.mode,
            source=args.source,
            project=args.project,
            space_key=args.space_key,
            rerank=args.rerank,
        )
        modo_usado = index.last_mode or args.mode
        reranked = index.last_reranked
    elapsed_ms = (time.monotonic() - started) * 1000
    pedido = f" (pedido: {args.mode})" if modo_usado != args.mode else ""
    print(f"modo={modo_usado}{pedido}  rerank={'sim' if reranked else 'nao'}  "
          f"resultados={len(hits)}  {elapsed_ms:.0f} ms")
    for position, hit in enumerate(hits, start=1):
        print(f"{position:>2}. [{hit.score:.4f}] {hit.title}")
        print(f"    {hit.url}")
        trecho = " ".join(hit.text.split())[:160]
        print(f"    {trecho}")


def _run_extract(cfg: Config, store: DocumentStore, args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for source in _sources(args.only, cfg):
        if source == SOURCE_CONFLUENCE:
            report["confluence"] = extract_confluence(
                cfg, store, full=args.full, count_attachments=args.count_attachments,
                fetch_labels=args.fetch_labels,
            )
        else:
            report["jira"] = extract_jira(
                cfg, store, full=args.full, include_comments=args.include_comments
            )
    return report


def _print_status(cfg: Config, store: DocumentStore) -> None:
    stats = store.stats()
    print(f"store:            {cfg.store_path}")
    print(f"documentos:       {stats.documents}")
    for source, count in sorted(stats.by_source.items()):
        print(f"  {source:<14} {count}")
    print(f"pendentes idx:    {stats.pending_index}")
    print(f"pendentes denso:  {stats.pending_embed}")
    print(f"cursor jira:      {store.jira_last_updated or '-'}")
    print(f"versoes conf.:    {len(store.confluence_versions())}")
    print(f"delecoes na fila: {len(store.get_state('pending_index_deletions', []) or [])}")
    last = store.get_state("last_run")
    if last:
        print(f"ultima rodada:    {last}")
    try:
        with _open_index(cfg) as index:
            print(f"qdrant:           {cfg.qdrant_url} v{index.check_server()}")
            print(f"pontos:           {index.count()}")
            print(f"pontos c/ denso:  {index.count_dense()}")
            print(
                f"embed:            {cfg.embedding.model_name} "
                f"device={cfg.embedding.device} batch={cfg.embedding.batch_size}"
            )
    except Exception as exc:  # noqa: BLE001 - status nunca deve abortar
        print(f"qdrant:           indisponível ({exc})")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logging(args.log_level)

    try:
        cfg = load_config()
        if cfg._errors:
            raise ConfigError(cfg.error_report())
    except ConfigError as exc:
        LOG.error("configuração inválida, abortando", extra={"detalhe": str(exc)})
        print(f"\nconfiguração inválida:\n{exc}\n", file=sys.stderr)
        return 2

    report: dict[str, Any] = {}
    try:
        with DocumentStore(cfg.store_path) as store:
            if args.command == "extract":
                report = _run_extract(cfg, store, args)
            elif args.command == "index":
                report = {
                    "index": run_index(
                        cfg, store, recreate=args.recreate, reindex_all=args.reindex_all,
                        max_chars=args.max_chars, overlap=args.overlap,
                    )
                }
            elif args.command == "embed":
                report = {
                    "embed": run_embed(
                        cfg, store, reembed_all=args.reembed_all,
                        device=args.device, batch_size=args.batch_size,
                        max_chars=args.max_chars, overlap=args.overlap,
                    )
                }
            elif args.command == "run":
                report = _run_extract(cfg, store, args)
                report["index"] = run_index(cfg, store)
                # O denso vem DEPOIS do index de propósito: o embed faz update
                # de vetor em ponto existente, então o ponto tem que existir.
                if args.embed:
                    report["embed"] = run_embed(
                        cfg, store, device=args.device, batch_size=args.batch_size
                    )
            elif args.command == "reconcile":
                for source in _sources(args.only, cfg):
                    report[source] = (
                        reconcile_confluence(cfg, store)
                        if source == SOURCE_CONFLUENCE
                        else reconcile_jira(cfg, store)
                    )
            elif args.command == "scope":
                _run_scope(cfg, store, args.apply)
                return 0
            elif args.command == "search":
                _run_search(cfg, args)
                return 0
            elif args.command == "status":
                _print_status(cfg, store)
                return 0

            if report:
                report["em"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                store.set_state("last_run", report)
                store.commit()
    except (
        ConfigError,
        ConfluenceAuthError,
        JiraAuthError,
        IndexError_,
        EmbeddingError,
    ) as exc:
        LOG.error("execução abortada", extra={"erro": str(exc)})
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        LOG.warning("interrompido pelo usuário; o progresso já gravado foi mantido")
        return 130

    LOG.info("rodada finalizada", extra={"relatorio": report})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
