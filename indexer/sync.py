"""CLI de orquestração do sync.

    python -m indexer.sync extract --only confluence
    python -m indexer.sync extract --only jira --full
    python -m indexer.sync index
    python -m indexer.sync run
    python -m indexer.sync reconcile --only jira
    python -m indexer.sync status

Extração e indexação são etapas separadas de propósito: a extração fala com
as instâncias Atlassian e é cara; a indexação lê só o store local. Trocar o
tamanho da fatia ou reconstruir o índice não toca em nada remoto.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Sequence

from config import Config, ConfigError, load_config, setup_logging
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
from indexer.index import IndexError_, KnowledgeIndex
from store.documents import SOURCE_CONFLUENCE, SOURCE_JIRA, DocumentStore

LOG = logging.getLogger("indexer.sync")

COMMIT_EVERY = 25


def _open_index(cfg: Config) -> KnowledgeIndex:
    return KnowledgeIndex(
        cfg.qdrant_url,
        cfg.collection,
        cfg.fastembed_cache_dir,
        allow_download=cfg.allow_model_download,
    )


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

        for space_key in conf.spaces:
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
        for space_key in conf.spaces:
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

    run_cmd = subparsers.add_parser("run", help="extract + index")
    add_scope(run_cmd)
    add_extract_flags(run_cmd)

    reconcile = subparsers.add_parser("reconcile", help="remove o que sumiu na origem")
    add_scope(reconcile)

    subparsers.add_parser("status", help="mostra o estado do store e do índice")
    return parser


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
    print(f"pendentes:        {stats.pending_index}")
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
            elif args.command == "run":
                report = _run_extract(cfg, store, args)
                report["index"] = run_index(cfg, store)
            elif args.command == "reconcile":
                for source in _sources(args.only, cfg):
                    report[source] = (
                        reconcile_confluence(cfg, store)
                        if source == SOURCE_CONFLUENCE
                        else reconcile_jira(cfg, store)
                    )
            elif args.command == "status":
                _print_status(cfg, store)
                return 0

            if report:
                report["em"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                store.set_state("last_run", report)
                store.commit()
    except (ConfigError, ConfluenceAuthError, JiraAuthError, IndexError_) as exc:
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
