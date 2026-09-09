"""Conector do Jira 8.14 (Server/DC) via REST API v2.

Autenticação por Personal Access Token no header Bearer: PATs foram
introduzidos exatamente na 8.14, então esta é a primeira versão em que dá
para evitar senha em texto claro no arquivo de configuração.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterator, Sequence

import requests

from config import JiraConfig
from connectors.confluence_legacy import wiki_to_text
from store.documents import Document

LOG = logging.getLogger("connectors.jira")

# Formato aceito pelo JQL. Sem segundos: o JQL não os aceita nesta versão.
_JQL_DATETIME = "%Y-%m-%d %H:%M"
_ISSUE_FIELDS = [
    "summary", "description", "status", "issuetype",
    "project", "updated", "created", "labels", "resolution",
]
# Teto por issue. Thread muito longa vira ruído no BM25 e estoura o chunking
# sem acrescentar recall; os comentários mais recentes são os que resolvem.
_COMMENT_BUDGET_CHARS = 20_000
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_PROGRESS_EVERY = 500


class JiraAuthError(RuntimeError):
    """Credencial inválida ou sem permissão: sempre aborta a execução."""


class JiraRequestError(RuntimeError):
    """Requisição recusada pelo Jira, com o motivo que ele mesmo deu."""


def _jira_error_detail(response: requests.Response) -> str:
    """Extrai a explicação do Jira do corpo da resposta.

    Um 400 em /search quase sempre é JQL inválido, e o Jira diz exatamente o
    que está errado em errorMessages. Sem isso o chamador - inclusive o modelo
    usando a ferramenta MCP - recebe só "400 Client Error" e não tem como se
    corrigir.
    """
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip()[:400]
    partes = [str(m) for m in payload.get("errorMessages", [])]
    partes += [f"{k}: {v}" for k, v in (payload.get("errors") or {}).items()]
    return "; ".join(partes) or (response.text or "").strip()[:400]


def _parse_jira_datetime(value: str) -> datetime | None:
    """Converte o formato do Jira Server, que traz offset mas não dois-pontos."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


@dataclass
class JiraExtraction:
    issues_seen: int = 0
    issues_changed: int = 0
    comments_fetched: int = 0
    comments_truncated: int = 0
    failed: int = 0
    requests_made: int = 0
    seconds: float = 0.0
    max_updated: str | None = None

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "issues_lidas": self.issues_seen,
            "issues_alteradas": self.issues_changed,
            "comentarios": self.comments_fetched,
            "threads_truncadas": self.comments_truncated,
            "falhas": self.failed,
            "requisicoes": self.requests_made,
            "tempo_s": round(self.seconds, 2),
            "cursor": self.max_updated,
        }


class JiraClient:
    def __init__(self, cfg: JiraConfig) -> None:
        self._cfg = cfg
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {cfg.pat}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self._session.verify = cfg.verify_ssl
        self.requests_made = 0

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> JiraClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # -- transporte --------------------------------------------------------

    def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None, retries: int = 3,
    ) -> dict[str, Any]:
        url = f"{self._cfg.url}{path}"
        last_error: Exception | None = None
        for attempt in range(retries):
            self.requests_made += 1
            try:
                response = self._session.request(
                    method, url, json=json_body, params=params, timeout=self._cfg.timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == retries - 1:
                    break
                time.sleep(2**attempt)
                continue

            if response.status_code in (401, 403):
                raise JiraAuthError(
                    f"Jira recusou a credencial em {path} (HTTP {response.status_code}). "
                    "Verifique JIRA_PAT e a permissão do usuário de serviço."
                )
            if 400 <= response.status_code < 500:
                raise JiraRequestError(
                    f"Jira recusou {method} {path} (HTTP {response.status_code}): "
                    f"{_jira_error_detail(response)}"
                )
            if response.status_code in _RETRY_STATUS and attempt < retries - 1:
                delay = 2**attempt
                LOG.warning(
                    "Jira respondeu erro transitório, tentando de novo",
                    extra={"path": path, "status": response.status_code, "espera_s": delay},
                )
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError(f"requisição {method} {path} falhou: {last_error}")

    # -- consultas ---------------------------------------------------------

    def myself(self) -> dict[str, Any]:
        return self._request("GET", "/rest/api/2/myself")

    def search_page(
        self, jql: str, start_at: int, max_results: int, fields: Sequence[str]
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/rest/api/2/search",
            json_body={
                "jql": jql,
                "startAt": start_at,
                "maxResults": max_results,
                "fields": list(fields),
            },
        )

    def iter_search(
        self, jql: str, fields: Sequence[str] = _ISSUE_FIELDS
    ) -> Iterator[dict[str, Any]]:
        start_at = 0
        total = None
        while True:
            payload = self.search_page(jql, start_at, self._cfg.page_size, fields)
            issues = payload.get("issues", [])
            total = payload.get("total", 0)
            if not issues:
                return
            yield from issues
            start_at += len(issues)
            if start_at >= total:
                return

    def iter_comments(self, issue_key: str) -> Iterator[dict[str, Any]]:
        """Comentários completos e paginados.

        O campo `comment` embutido na resposta do /search vem truncado em
        issues longas; é justamente nelas que costuma estar a solução.
        """
        start_at = 0
        while True:
            payload = self._request(
                "GET",
                f"/rest/api/2/issue/{issue_key}/comment",
                params={"startAt": start_at, "maxResults": 100, "orderBy": "created"},
            )
            comments = payload.get("comments", [])
            if not comments:
                return
            yield from comments
            start_at += len(comments)
            if start_at >= int(payload.get("total", 0)):
                return

    def get_issue(self, issue_key: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/rest/api/2/issue/{issue_key}",
            params={"fields": ",".join(_ISSUE_FIELDS)},
        )

    def iter_all_keys(self, projects: Sequence[str]) -> Iterator[str]:
        """Só as chaves, para a reconciliação de deleções."""
        jql = f"{_project_clause(projects)} ORDER BY key ASC"
        for issue in self.iter_search(jql, fields=["key"]):
            key = issue.get("key")
            if key:
                yield str(key)


def _project_clause(projects: Sequence[str]) -> str:
    quoted = ", ".join(f'"{p}"' for p in projects)
    return f"project in ({quoted})"


def build_incremental_jql(
    projects: Sequence[str], since: str | None, *, safety_hours: int = 1
) -> str:
    """Monta o JQL incremental com a janela de segurança.

    A janela existe porque a issue alterada no mesmo minuto em que a rodada
    anterior terminou não entra no `>=` da próxima; sem ela essa alteração
    fica invisível para sempre.

    O horário do JQL é interpretado no fuso do usuário do Jira, não em UTC.
    Por isso o cursor é o próprio campo `updated` devolvido pelo Jira, com o
    offset dele, e não o relógio da máquina do indexador.
    """
    clause = _project_clause(projects)
    if not since:
        return f"{clause} ORDER BY updated ASC"
    parsed = _parse_jira_datetime(since)
    if parsed is None:
        LOG.warning("cursor do Jira ilegível, refazendo carga completa", extra={"cursor": since})
        return f"{clause} ORDER BY updated ASC"
    window = (parsed - timedelta(hours=safety_hours)).strftime(_JQL_DATETIME)
    return f'{clause} AND updated >= "{window}" ORDER BY updated ASC'


def _render_comments(
    comments: Sequence[dict[str, Any]], issue_key: str, metrics: JiraExtraction
) -> str:
    rendered: list[str] = []
    for comment in comments:
        author = (comment.get("author") or {}).get("displayName") or "desconhecido"
        created = comment.get("created") or ""
        body = wiki_to_text(comment.get("body"))
        if not body:
            continue
        rendered.append(f"### {author} — {created}\n{body}")

    if not rendered:
        return ""

    # Mantém os mais recentes: percorre de trás para frente até o teto.
    kept: list[str] = []
    used = 0
    for block in reversed(rendered):
        if used + len(block) > _COMMENT_BUDGET_CHARS and kept:
            metrics.comments_truncated += 1
            LOG.warning(
                "thread de comentários truncada no teto de caracteres",
                extra={
                    "issue": issue_key,
                    "comentarios_total": len(rendered),
                    "comentarios_mantidos": len(kept),
                    "teto_chars": _COMMENT_BUDGET_CHARS,
                },
            )
            break
        kept.append(block)
        used += len(block)
    kept.reverse()
    return "## Comentários\n" + "\n\n".join(kept)


def issue_to_document(
    issue: dict[str, Any], cfg: JiraConfig, comments: Sequence[dict[str, Any]],
    metrics: JiraExtraction,
) -> Document:
    fields = issue.get("fields") or {}
    key = str(issue.get("key") or "")
    summary = str(fields.get("summary") or "").strip()
    description = wiki_to_text(fields.get("description"))

    parts = [summary, description]
    comment_text = _render_comments(comments, key, metrics)
    if comment_text:
        parts.append(comment_text)
    body = "\n\n".join(part for part in parts if part).strip()

    status = ((fields.get("status") or {}).get("name")) or None
    issue_type = ((fields.get("issuetype") or {}).get("name")) or None
    project = ((fields.get("project") or {}).get("key")) or None

    return Document(
        doc_id=f"jira:{key}",
        source="jira",
        content_type="issue",
        title=f"{key}: {summary}" if summary else key,
        body_text=body,
        url=cfg.browse_url(key),
        updated=str(fields.get("updated") or "") or None,
        project=project,
        status=status,
        issue_type=issue_type,
        labels=tuple(str(label) for label in (fields.get("labels") or [])),
    )


class JiraExtractor:
    def __init__(self, client: JiraClient, cfg: JiraConfig) -> None:
        self._client = client
        self._cfg = cfg

    def iter_documents(
        self, since: str | None, metrics: JiraExtraction, *, include_comments: bool = True
    ) -> Iterator[Document]:
        jql = build_incremental_jql(self._cfg.projects, since)
        LOG.info("consulta incremental do Jira", extra={"jql": jql})
        started = time.monotonic()
        try:
            for issue in self._client.iter_search(jql):
                metrics.issues_seen += 1
                if metrics.issues_seen % _PROGRESS_EVERY == 0:
                    elapsed = time.monotonic() - started
                    LOG.info(
                        "progresso do Jira",
                        extra={
                            "issues": metrics.issues_seen,
                            "issues_por_s": round(metrics.issues_seen / elapsed, 2) if elapsed else 0,
                            "cursor": metrics.max_updated,
                        },
                    )
                key = str(issue.get("key") or "")
                comments: list[dict[str, Any]] = []
                if include_comments:
                    try:
                        comments = list(self._client.iter_comments(key))
                        metrics.comments_fetched += len(comments)
                    except (requests.RequestException, RuntimeError) as exc:
                        # Perder os comentários de uma issue não justifica
                        # perder a issue: indexa o que dá e registra.
                        metrics.failed += 1
                        LOG.warning(
                            "comentários não puderam ser lidos, indexando sem eles",
                            extra={"issue": key, "erro": str(exc)},
                        )
                document = issue_to_document(issue, self._cfg, comments, metrics)
                updated = document.updated
                if updated and (metrics.max_updated is None or updated > metrics.max_updated):
                    metrics.max_updated = updated
                yield document
        finally:
            metrics.seconds += time.monotonic() - started
            metrics.requests_made = self._client.requests_made
