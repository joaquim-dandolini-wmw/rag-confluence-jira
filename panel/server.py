"""Painel web de operação: estado das rodadas, logs, acesso e agenda.

Por que existe: operar isto hoje é `tail -f` num log JSON e `crontab -e`. O que
o painel resolve é ver, de um lugar só, se a última rodada fechou, se o acesso
ao Confluence ainda responde e a que horas a próxima acontece — e mudar esse
horário sem editar crontab à mão.

O que ele NÃO faz, de propósito: disparar rodada. Um clique que começa um
trabalho de horas escrevendo no mesmo SQLite do cron merece mais cuidado do que
um botão; o `flock` que protege isso está no crontab, não aqui.

Segurança: escreve no crontab do usuário, então o padrão é escutar SÓ em
127.0.0.1. Para acessar de outra máquina, túnel SSH:

    ssh -L 8770:127.0.0.1:8770 joaquimdp@10.2.1.132
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from config import Config, load_config, setup_logging
from indexer.index import KnowledgeIndex
from panel import logs as panel_logs
from panel import schedule as sched
from store.documents import DocumentStore

LOG = logging.getLogger("panel")

REPO = Path(__file__).resolve().parent.parent
HTML = Path(__file__).resolve().parent / "index.html"
AGENDA_PATH = REPO / "deploy" / "agenda.json"
LOG_DIR = REPO / "logs"

_cfg: Config | None = None


def cfg() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def _index(c: Config) -> KnowledgeIndex:
    return KnowledgeIndex(
        c.qdrant_url, c.collection, c.fastembed_cache_dir,
        allow_download=c.allow_model_download, embedding=c.embedding, rerank=c.rerank,
    )


# -- rotas -----------------------------------------------------------------

def home(request: Request) -> FileResponse:
    return FileResponse(HTML)


def estado(request: Request) -> JSONResponse:
    c = cfg()
    dados: dict[str, Any] = {"erros_de_config": list(c._errors), "repo": str(REPO)}

    # Usa o DocumentStore do projeto em vez de SQL solto: se o esquema mudar, o
    # painel acompanha sozinho.
    try:
        with DocumentStore(c.store_path) as store:
            st = store.stats()
            dados["store"] = {
                "caminho": str(c.store_path),
                "documentos": st.documents,
                "por_fonte": st.by_source,
                "pendentes_index": st.pending_index,
                "pendentes_denso": st.pending_embed,
                "cursor_jira": store.jira_last_updated,
                "versoes_confluence": len(store.confluence_versions()),
                "delecoes_na_fila": len(store.get_state("pending_index_deletions", []) or []),
            }
            dados["ultima_rodada"] = store.get_state("last_run")
    except Exception as exc:  # noqa: BLE001 - painel nunca deve cair por isso
        dados["store"] = {"erro": str(exc)}

    try:
        with _index(c) as idx:
            dados["qdrant"] = {
                "url": c.qdrant_url,
                "colecao": c.collection,
                "versao": idx.check_server(),
                "pontos": idx.count(),
                "pontos_com_denso": idx.count_dense(),
            }
    except Exception as exc:  # noqa: BLE001
        dados["qdrant"] = {"url": c.qdrant_url, "erro": str(exc)}

    dados["modelos"] = {
        "embed": c.embedding.model_name,
        "embed_device": c.embedding.device,
        "embed_batch": c.embedding.batch_size,
        "rerank": c.rerank.model_name if c.rerank.enabled else None,
        "rerank_candidatos": c.rerank.candidates,
    }
    dados["mcp"] = {
        "transporte": c.mcp.transport,
        "host": c.mcp.host,
        "porta": c.mcp.port,
        "caminho": c.mcp.path,
        "tls": bool(c.mcp.tls_cert and c.mcp.tls_key),
    }
    return JSONResponse(dados)


def acessos(request: Request) -> JSONResponse:
    """Configuração de acesso das duas fontes. Nunca devolve segredo."""
    c = cfg()
    conf = c.confluence
    jira = c.jira
    dados: dict[str, Any] = {
        "confluence": None if conf is None else {
            "url": conf.url,
            "endpoint": conf.rpc_endpoint,
            "usuario": conf.user,
            "senha_definida": bool(conf.password),
            "escopo": "auto (grupos do usuário)" if conf.discover else "lista fixa",
            "espacos": list(conf.spaces),
            "excluidos": list(conf.exclude_spaces),
            "timeout": conf.timeout,
            "verify_ssl": conf.verify_ssl,
            "estrategia": conf.strategy,
        },
        "jira": None if jira is None else {
            "url": jira.url,
            "pat_definido": bool(jira.pat),
            "projetos": list(jira.projects),
            "timeout": jira.timeout,
            "verify_ssl": jira.verify_ssl,
            "page_size": jira.page_size,
        },
    }
    try:
        with DocumentStore(c.store_path) as store:
            dados["espacos_no_store"] = sorted(store.confluence_space_keys())
    except Exception as exc:  # noqa: BLE001
        dados["espacos_no_store"] = []
        dados["erro_store"] = str(exc)
    return JSONResponse(dados)


def testar_confluence(request: Request) -> JSONResponse:
    """Login + getSpaces ao vivo. É o teste que revela perda de permissão.

    O login sozinho não serve: ele só confere usuário e senha, e continua
    passando depois de o usuário perder "Use Confluence" — foi exatamente o que
    aconteceu em 10/09/2026. Quem denuncia é a primeira chamada de verdade.
    """
    c = cfg()
    if c.confluence is None:
        return JSONResponse({"ok": False, "erro": "Confluence não configurado no .env"})

    from connectors.confluence_legacy import ConfluenceClient

    inicio = time.monotonic()
    client = ConfluenceClient(c.confluence)
    try:
        client.login()
        espacos = [str(e.get("key")) for e in client.get_spaces() if e.get("key")]
    except Exception as exc:  # noqa: BLE001 - o erro é o resultado do teste
        return JSONResponse({
            "ok": False,
            "etapa": "login" if "login" in str(exc).lower() else "getSpaces",
            "erro": str(exc),
            "ms": round((time.monotonic() - inicio) * 1000),
        })
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass

    pessoais = [e for e in espacos if e.startswith("~")]
    return JSONResponse({
        "ok": True,
        "espacos_visiveis": len(espacos),
        "pessoais": len(pessoais),
        "amostra": sorted(espacos)[:12],
        "ms": round((time.monotonic() - inicio) * 1000),
    })


def ver_logs(request: Request) -> JSONResponse:
    arquivos = panel_logs.list_files(LOG_DIR)
    nome = request.query_params.get("arquivo") or (arquivos[0]["nome"] if arquivos else "")
    try:
        limite = max(20, min(1000, int(request.query_params.get("limite", "200"))))
    except ValueError:
        limite = 200
    resumo = request.query_params.get("resumo") == "1"

    eventos: list[dict[str, Any]] = []
    if nome:
        caminho = LOG_DIR / Path(nome).name  # nunca aceita caminho de fora
        if caminho.is_file():
            eventos = panel_logs.parse(panel_logs.tail(caminho, limite))
            if resumo:
                eventos = panel_logs.only_summaries(eventos)
    return JSONResponse({"arquivos": arquivos, "arquivo": nome, "eventos": eventos})


def ver_agenda(request: Request) -> JSONResponse:
    agenda = sched.load(AGENDA_PATH)
    crontab = sched.read_crontab()
    return JSONResponse({
        "agenda": agenda.as_dict(),
        "no_ar": sched.block_of(crontab),
        "previsto": sched.render_block(agenda, REPO),
        "crontab_tem_bloco": sched.BEGIN in crontab,
    })


async def salvar_agenda(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": "corpo não é JSON"}, status_code=400)

    try:
        agenda = sched.parse(payload)
        bloco = sched.render_block(agenda, REPO)
        sched.write_crontab(sched.merge(sched.read_crontab(), bloco))
        sched.save(AGENDA_PATH, agenda)
    except sched.ScheduleError as exc:
        return JSONResponse({"ok": False, "erro": str(exc)}, status_code=400)

    LOG.info("agenda alterada pelo painel", extra=agenda.as_dict())
    return JSONResponse({"ok": True, "agenda": agenda.as_dict(), "no_ar": bloco})


app = Starlette(routes=[
    Route("/", home),
    Route("/api/estado", estado),
    Route("/api/acessos", acessos),
    Route("/api/confluence/testar", testar_confluence, methods=["POST"]),
    Route("/api/logs", ver_logs),
    Route("/api/agenda", ver_agenda),
    Route("/api/agenda", salvar_agenda, methods=["POST"]),
])


def main() -> int:
    setup_logging(os.environ.get("PANEL_LOG_LEVEL", "INFO"))
    cfg()  # carrega o .env cedo, para PANEL_* já estar no ambiente
    host = os.environ.get("PANEL_HOST", "127.0.0.1")
    porta = int(os.environ.get("PANEL_PORT", "8770"))
    if host not in ("127.0.0.1", "localhost", "::1"):
        LOG.warning(
            "painel escutando fora do loopback: ele ALTERA o crontab e não tem "
            "autenticação — prefira túnel SSH",
            extra={"host": host, "porta": porta},
        )
    LOG.info("painel no ar", extra={"url": f"http://{host}:{porta}/"})

    import uvicorn

    uvicorn.run(app, host=host, port=porta, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
