"""Servidor MCP: quatro ferramentas sobre o índice e sobre as instâncias ao vivo.

O diretório se chama mcp_server e não mcp porque um pacote local chamado
`mcp` entraria antes do site-packages no sys.path e sombrearia o próprio SDK.

A separação entre índice e ao vivo é deliberada e está escrita nas docstrings,
que são o contrato de roteamento: o modelo escolhe a ferramenta lendo elas.
Índice para conteúdo e significado; ao vivo para exatidão e frescor.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from config import SEARCH_MODES, ConfigError, load_config, setup_logging
from connectors.confluence_legacy import (
    ConfluenceAuthError,
    ConfluenceClient,
    storage_to_text,
)
from connectors.jira_server import JiraAuthError, JiraClient, JiraRequestError
from indexer.index import KnowledgeIndex

LOG = logging.getLogger("mcp_server")

MAX_SEARCH_LIMIT = 25
MAX_JQL_RESULTS = 100
MAX_PAGE_CHARS = 200_000

server = MCPServer(
    name="atlassian-kb",
    instructions=(
        "Busca sobre um Jira 8.14 e um Confluence 4.2.4 internos. "
        "Use search_knowledge_base para encontrar conteúdo por assunto ou por "
        "significado; use as "
        "ferramentas ao vivo (search_jira_jql, get_jira_issue, "
        "get_confluence_page) sempre que a resposta precisar estar correta "
        "agora, e não apenas aproximada. Todo resultado traz a URL de origem: "
        "cite-a."
    ),
)

_cfg = None
_index: KnowledgeIndex | None = None
_confluence: ConfluenceClient | None = None


def _config() -> Any:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def _knowledge_index() -> KnowledgeIndex:
    global _index
    if _index is None:
        cfg = _config()
        _index = KnowledgeIndex(
            cfg.qdrant_url,
            cfg.collection,
            cfg.fastembed_cache_dir,
            allow_download=cfg.allow_model_download,
            embedding=cfg.embedding,
        )
    return _index


def _confluence_client() -> ConfluenceClient:
    """Sessão XML-RPC reaproveitada entre chamadas.

    Abrir e fechar sessão a cada leitura dobraria o número de round-trips; o
    _call do cliente já reautentica sozinho quando o token expira.
    """
    global _confluence
    if _confluence is None:
        client = ConfluenceClient(_config().require_confluence())
        client.login()
        _confluence = client
    return _confluence


def _error(message: str, **extra: Any) -> dict[str, Any]:
    LOG.warning("ferramenta retornou erro", extra={"detalhe": message, **extra})
    return {"error": message, **extra}


# --------------------------------------------------------------------------
# 1. índice
# --------------------------------------------------------------------------

@server.tool()
def search_knowledge_base(
    query: str,
    limit: int = 8,
    source: str | None = None,
    project: str | None = None,
    space_key: str | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    """Busca híbrida no índice unificado de Jira e Confluence.

    Esta é a ferramenta padrão para encontrar conhecimento: procedimentos,
    runbooks, post-mortems, decisões, discussões de chamados. Ela pesquisa o
    texto das páginas e das issues ao mesmo tempo e devolve os trechos
    relevantes com a URL de origem.

    Encontra por PALAVRA e por SIGNIFICADO, e escolhe sozinha qual dos dois usar
    conforme o formato da pergunta. Um identificador (VENDAS-14993,
    PRUPSYNCPRODUTOS) vai pelo índice lexical, que acerta o exato; uma pergunta
    em prosa vai pelo índice semântico, que acha o documento mesmo quando o
    texto usa outra palavra para a mesma coisa - "dados da fatura para pagamento
    em banco" encontra a página que fala em "boleto bancário". Não é preciso
    adivinhar o vocabulário de quem escreveu.

    Cada resultado traz a URL de origem: a página do Confluence ou a issue do
    Jira em /browse/. Cite-a.

    USE QUANDO:
      - a pergunta for sobre um assunto, um erro, um procedimento ou um
        conceito ("como renovar o certificado do gateway", "o que causou a
        queda do barramento em março");
      - você não souber em qual das duas fontes está a resposta;
      - você precisar de trechos de texto para fundamentar uma explicação;
      - você tiver um identificador solto no meio de um texto (ERR-4012) e
        quiser saber onde ele é mencionado;
      - você suspeitar que o documento existe mas não souber com que palavras
        ele foi escrito: descreva o problema com as suas.

    NÃO USE QUANDO:
      - a pergunta pedir CONTAGEM, LISTAGEM COMPLETA, FILTRO POR STATUS ou
        ORDENAÇÃO de issues ("quantos bugs abertos", "liste os chamados em
        andamento do projeto OPS"). Use search_jira_jql: o índice é
        reconstruído por cron, tem atraso de minutos a horas, e devolve os
        trechos mais parecidos, não o conjunto exato;
      - você já souber a chave da issue e quiser o estado atual dela. Use
        get_jira_issue;
      - você precisar do texto completo de uma página. Use
        get_confluence_page: aqui vêm apenas fatias.

    Args:
        query: texto livre em português ou inglês. Acentuação é indiferente.
            Descreva o assunto; não é necessário acertar o termo do documento.
        limit: número de trechos a devolver (1 a 25).
        source: restringe a "jira" ou "confluence". Deixe vazio para as duas.
        project: chave do projeto Jira, ex. "OPS". Só afeta resultados do Jira.
        space_key: chave do espaço Confluence, ex. "INFRA". Só afeta o Confluence.
        mode: deixe o padrão "auto", que escolhe sozinho pelo formato da
            pergunta. Os outros existem para comparação A/B: "bm25" só lexical,
            "dense" só semântico, "hybrid" funde os dois por RRF.

    Returns:
        results: lista de trechos, cada um com title, url, source, text e score.
    """
    limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    if mode not in SEARCH_MODES:
        return _error(
            f"modo {mode!r} desconhecido; aceitos: {', '.join(SEARCH_MODES)}"
        )
    try:
        hits = _knowledge_index().search(
            query, limit=limit, source=source, project=project,
            space_key=space_key, mode=mode,
        )
    except Exception as exc:  # noqa: BLE001 - a ferramenta devolve o erro, não derruba o servidor
        return _error(f"busca no índice falhou: {exc}")

    return {
        "query": query,
        "mode": mode,
        "count": len(hits),
        "filters": {"source": source, "project": project, "space_key": space_key},
        "results": [hit.as_dict() for hit in hits],
        "note": (
            "Resultados vêm de um índice atualizado por cron e podem estar "
            "defasados. Para estado atual de issue use get_jira_issue ou "
            "search_jira_jql."
        ),
    }


# --------------------------------------------------------------------------
# 2. Jira ao vivo, consulta estruturada
# --------------------------------------------------------------------------

@server.tool()
def search_jira_jql(jql: str, max_results: int = 25) -> dict[str, Any]:
    """Executa JQL diretamente no Jira, ao vivo. Resposta exata e atual.

    Vai direto na instância, sem passar pelo índice. É a única ferramenta que
    pode responder pergunta cuja resposta precisa estar correta AGORA.

    USE QUANDO:
      - a pergunta envolver contagem, listagem completa, filtro por status,
        responsável, data ou prioridade, ou ordenação;
      - o usuário pedir "quantos", "quais", "liste todos", "em aberto",
        "atribuídos a", "criados esta semana";
      - você precisar do conjunto exato de issues, e não de uma amostra
        relevante.

    NÃO USE QUANDO:
      - a pergunta for sobre o CONTEÚDO ou o significado do que está escrito
        nas issues, sem critério estruturado. JQL só casa texto com o operador
        ~, que é lexical e fraco. Use search_knowledge_base;
      - a resposta puder estar no Confluence: JQL não enxerga páginas.

    Args:
        jql: expressão JQL válida do Jira 8.14. Exemplos:
             'project = OPS AND status = "In Progress" ORDER BY updated DESC'
             'project in (OPS, INFRA) AND created >= -7d'
             Restrinja sempre por projeto: a conta de serviço enxerga muito.
        max_results: teto de issues devolvidas (1 a 100). O total real vem no
             campo `total`, mesmo quando maior que este teto.

    Returns:
        total: quantidade real de issues que casam com o JQL.
        issues: lista com key, summary, status, issue_type, assignee, updated e url.
    """
    max_results = max(1, min(int(max_results), MAX_JQL_RESULTS))
    cfg = _config()
    try:
        jira = cfg.require_jira()
    except ConfigError as exc:
        return _error(str(exc))

    try:
        with JiraClient(jira) as client:
            payload = client.search_page(
                jql,
                start_at=0,
                max_results=max_results,
                fields=["summary", "status", "issuetype", "assignee", "updated", "project"],
            )
    except JiraAuthError as exc:
        return _error(str(exc))
    except JiraRequestError as exc:
        # Devolve o motivo do Jira literalmente: é o que permite ao modelo
        # corrigir o JQL na tentativa seguinte.
        return _error(str(exc), jql=jql, dica="revise nomes de campo, status e aspas no JQL")
    except Exception as exc:  # noqa: BLE001
        return _error(f"consulta ao Jira falhou: {exc}", jql=jql)

    issues = []
    for issue in payload.get("issues", []):
        fields = issue.get("fields") or {}
        key = str(issue.get("key"))
        issues.append(
            {
                "key": key,
                "summary": fields.get("summary"),
                "status": (fields.get("status") or {}).get("name"),
                "issue_type": (fields.get("issuetype") or {}).get("name"),
                "assignee": (fields.get("assignee") or {}).get("displayName"),
                "updated": fields.get("updated"),
                "url": jira.browse_url(key),
            }
        )
    return {
        "jql": jql,
        "total": int(payload.get("total", 0)),
        "returned": len(issues),
        "issues": issues,
        "live": True,
    }


# --------------------------------------------------------------------------
# 3. Jira ao vivo, leitura pontual
# --------------------------------------------------------------------------

@server.tool()
def get_jira_issue(issue_key: str, include_comments: bool = True) -> dict[str, Any]:
    """Lê uma issue inteira do Jira, ao vivo, com todos os comentários.

    USE QUANDO:
      - você já tem a chave da issue (formato PROJ-1234), venha ela do usuário
        ou de um resultado de search_knowledge_base;
      - precisar do estado atual: status, responsável, resolução;
      - precisar da discussão completa, inclusive comentários que o índice
        pode ter truncado ou ainda não ter capturado.

    NÃO USE QUANDO:
      - você não tem a chave exata. Descubra antes com search_knowledge_base
        ou search_jira_jql; esta ferramenta não faz busca;
      - quiser várias issues de uma vez. Use search_jira_jql.

    Args:
        issue_key: chave no formato PROJ-1234.
        include_comments: traz a thread completa de comentários.

    Returns:
        A issue com summary, description, status, url e, opcionalmente, comments.
    """
    key = (issue_key or "").strip().upper()
    if not key:
        return _error("issue_key vazio")
    cfg = _config()
    try:
        jira = cfg.require_jira()
    except ConfigError as exc:
        return _error(str(exc))

    try:
        with JiraClient(jira) as client:
            issue = client.get_issue(key)
            comments: list[dict[str, Any]] = []
            if include_comments:
                comments = [
                    {
                        "author": (c.get("author") or {}).get("displayName"),
                        "created": c.get("created"),
                        "updated": c.get("updated"),
                        "body": c.get("body"),
                    }
                    for c in client.iter_comments(key)
                ]
    except JiraAuthError as exc:
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error(f"não foi possível ler {key}: {exc}", issue_key=key)

    fields = issue.get("fields") or {}
    return {
        "key": key,
        "url": jira.browse_url(key),
        "summary": fields.get("summary"),
        "description": fields.get("description"),
        "status": (fields.get("status") or {}).get("name"),
        "issue_type": (fields.get("issuetype") or {}).get("name"),
        "project": (fields.get("project") or {}).get("key"),
        "resolution": (fields.get("resolution") or {}).get("name"),
        "labels": fields.get("labels") or [],
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "comment_count": len(comments),
        "comments": comments,
        "live": True,
    }


# --------------------------------------------------------------------------
# 4. Confluence ao vivo
# --------------------------------------------------------------------------

@server.tool()
def get_confluence_page(page_id: str) -> dict[str, Any]:
    """Lê uma página inteira do Confluence, ao vivo, já convertida em texto.

    USE QUANDO:
      - um resultado de search_knowledge_base parecer certo mas a fatia não
        bastar: pegue o doc_id do resultado (formato confluence:page:12345) e
        passe só o número aqui;
      - precisar de uma tabela, um procedimento longo ou um bloco de código
        que aparece cortado na fatia;
      - precisar da versão atual da página, mais nova que o índice.

    NÃO USE QUANDO:
      - você não tem o id numérico da página. Esta ferramenta não busca por
        título nem por assunto; encontre a página com search_knowledge_base
        primeiro;
      - a fatia devolvida pela busca já responde a pergunta. A página inteira
        pode ser muito longa.

    Args:
        page_id: id numérico da página, como aparece em doc_id
            ("confluence:page:12345" -> "12345") ou na URL pageId=12345.

    Returns:
        A página com title, url, space_key, version, labels e text.
    """
    content_id = (page_id or "").strip()
    if content_id.startswith("confluence:"):
        content_id = content_id.rsplit(":", 1)[-1]
    if not content_id.isdigit():
        return _error(
            "page_id deve ser o id numérico da página, ex. '12345'", recebido=page_id
        )

    try:
        client = _confluence_client()
        page = client.get_page(content_id)
        labels = client.get_labels(content_id)
    except (ConfigError, ConfluenceAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error(f"não foi possível ler a página {content_id}: {exc}")

    text = storage_to_text(page.get("content"), doc_hint=f"confluence:page:{content_id}")
    truncated = len(text) > MAX_PAGE_CHARS
    return {
        "page_id": content_id,
        "title": page.get("title"),
        "url": page.get("url"),
        "space_key": page.get("space"),
        "version": page.get("version"),
        "labels": list(labels),
        "truncated": truncated,
        "text": text[:MAX_PAGE_CHARS],
        "live": True,
    }


def main() -> int:
    setup_logging()
    cfg = load_config()
    if cfg._errors:
        # Abortar aqui em vez de deixar cada ferramenta falhar: um servidor
        # que sobe com escopo inválido é pior do que um que não sobe.
        print(f"\nconfiguração inválida:\n{cfg.error_report()}\n", file=sys.stderr)
        return 2
    LOG.info(
        "servidor MCP iniciando",
        extra={
            "qdrant": cfg.qdrant_url,
            "collection": cfg.collection,
            "jira": bool(cfg.jira),
            "confluence": bool(cfg.confluence),
        },
    )
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
