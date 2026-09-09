"""Configuração central: variáveis de ambiente, escopo e logging.

Tudo que lê o ambiente passa por aqui. O motivo não é organização: é o
controle de escopo. O indexador entra nas instâncias com um usuário de
serviço, então qualquer coisa que ele leia vira conteúdo público do índice.
Um único ponto de validação torna impossível esquecer o abort de escopo vazio
em algum caminho de código novo.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent

# Só existe uma estratégia na Fase 1. A lista existe para que o config
# rejeite um valor inválido com mensagem útil em vez de cair adiante.
CONFLUENCE_STRATEGIES: Final[tuple[str, ...]] = ("full_scan",)

DEFAULT_COLLECTION: Final[str] = "atlassian_kb"
DENSE_VECTOR_NAME: Final[str] = "dense"
SPARSE_VECTOR_NAME: Final[str] = "bm25"
# Declarado no schema desde já para não exigir migração na Fase 2.
DENSE_VECTOR_SIZE: Final[int] = 1024


class ConfigError(RuntimeError):
    """Configuração ausente, inválida ou insegura. Sempre aborta a execução."""


# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------

def load_dotenv(path: Path | None = None) -> None:
    """Carrega um .env sem depender de python-dotenv.

    Não sobrescreve variáveis já presentes no ambiente, para que o cron ou o
    systemd continuem tendo a palavra final sobre o arquivo em disco.
    """
    env_path = path or REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


# --------------------------------------------------------------------------
# logging estruturado
# --------------------------------------------------------------------------

class JsonLineFormatter(logging.Formatter):
    """Uma linha JSON por evento, com os campos de `extra` embutidos."""

    _RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str | int = "INFO") -> None:
    """Configura o log na stderr.

    Nunca na stdout: o servidor MCP fala JSON-RPC por stdout e uma linha de
    log solta ali corrompe o transporte.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLineFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level if isinstance(level, int) else level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# leitura de env
# --------------------------------------------------------------------------

def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "sim"}


def _env_list(name: str) -> tuple[str, ...]:
    raw = _env(name)
    if raw is None:
        return ()
    items = [part.strip() for part in raw.replace(";", ",").split(",")]
    return tuple(item for item in items if item)


# --------------------------------------------------------------------------
# blocos de configuração
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class JiraConfig:
    url: str
    pat: str
    projects: tuple[str, ...]
    verify_ssl: bool = True
    timeout: int = 60
    page_size: int = 100

    def browse_url(self, issue_key: str) -> str:
        return f"{self.url}/browse/{issue_key}"


@dataclass(frozen=True)
class ConfluenceConfig:
    url: str
    user: str
    password: str
    spaces: tuple[str, ...]
    verify_ssl: bool = True
    # Alto de propósito: getPages num espaço grande é UMA chamada que devolve
    # milhares de resumos. Medido em produção: 181 s para 6.129 páginas.
    timeout: int = 300
    strategy: str = "full_scan"

    @property
    def rpc_endpoint(self) -> str:
        return f"{self.url}/rpc/xmlrpc"


@dataclass(frozen=True)
class Config:
    store_path: Path
    qdrant_url: str
    collection: str
    fastembed_cache_dir: Path
    allow_model_download: bool
    jira: JiraConfig | None = None
    confluence: ConfluenceConfig | None = None
    _errors: tuple[str, ...] = field(default=(), repr=False)

    def require_jira(self) -> JiraConfig:
        if self.jira is None:
            raise ConfigError(
                "Jira não configurado ou com escopo inválido. Verifique JIRA_URL, "
                "JIRA_PAT e JIRA_PROJECTS.\n" + self.error_report()
            )
        return self.jira

    def require_confluence(self) -> ConfluenceConfig:
        if self.confluence is None:
            raise ConfigError(
                "Confluence não configurado ou com escopo inválido. Verifique "
                "CONFLUENCE_URL, CONFLUENCE_USER, CONFLUENCE_PASSWORD e "
                "CONFLUENCE_SPACES.\n" + self.error_report()
            )
        return self.confluence

    def error_report(self) -> str:
        if not self._errors:
            return ""
        return "Problemas encontrados:\n" + "\n".join(f"  - {e}" for e in self._errors)


def load_config(*, dotenv: bool = True) -> Config:
    """Lê o ambiente e valida.

    Aborta imediatamente em escopo vazio de uma fonte que está configurada.
    Uma fonte simplesmente ausente (sem URL) fica como None e só quebra se
    alguém pedir por ela.
    """
    if dotenv:
        load_dotenv()

    errors: list[str] = []

    store_path = Path(_env("STORE_PATH") or REPO_ROOT / "data" / "documents.sqlite3")
    qdrant_url = _env("QDRANT_URL") or "http://127.0.0.1:6333"
    collection = _env("QDRANT_COLLECTION") or DEFAULT_COLLECTION
    cache_dir = Path(_env("FASTEMBED_CACHE_DIR") or REPO_ROOT / "models" / "fastembed")
    allow_download = _env_bool("ALLOW_MODEL_DOWNLOAD", False)
    verify_ssl = _env_bool("VERIFY_SSL", True)

    jira: JiraConfig | None = None
    jira_url = _env("JIRA_URL")
    if jira_url:
        pat = _env("JIRA_PAT")
        projects = _env_list("JIRA_PROJECTS")
        if not pat:
            errors.append("JIRA_URL definido mas JIRA_PAT vazio.")
        # Escopo vazio nunca significa "tudo". Ver seção de segurança do README.
        elif not projects:
            errors.append(
                "JIRA_PROJECTS está vazio. Isso indexaria TODOS os projetos "
                "visíveis ao usuário de serviço, incluindo projetos restritos, "
                "e tornaria esse conteúdo pesquisável por qualquer pessoa. "
                "Liste os projetos explicitamente."
            )
        else:
            jira = JiraConfig(
                url=jira_url.rstrip("/"),
                pat=pat,
                projects=projects,
                verify_ssl=verify_ssl,
            )

    confluence: ConfluenceConfig | None = None
    conf_url = _env("CONFLUENCE_URL")
    if conf_url:
        user = _env("CONFLUENCE_USER")
        password = _env("CONFLUENCE_PASSWORD")
        spaces = _env_list("CONFLUENCE_SPACES")
        strategy = (_env("CONFLUENCE_INCREMENTAL_STRATEGY") or "full_scan").lower()
        if not user or not password:
            # O Confluence 4.x não suporta Personal Access Token; é usuário e senha ou nada.
            errors.append(
                "CONFLUENCE_URL definido mas CONFLUENCE_USER/CONFLUENCE_PASSWORD "
                "vazios (esta versão não suporta PAT)."
            )
        elif not spaces:
            errors.append(
                "CONFLUENCE_SPACES está vazio. Isso indexaria TODOS os espaços "
                "visíveis ao usuário de serviço, incluindo RH, jurídico e "
                "financeiro, e tornaria esse conteúdo pesquisável por qualquer "
                "pessoa. Liste os espaços explicitamente."
            )
        elif strategy not in CONFLUENCE_STRATEGIES:
            errors.append(
                f"CONFLUENCE_INCREMENTAL_STRATEGY={strategy!r} desconhecida. "
                f"Aceitas: {', '.join(CONFLUENCE_STRATEGIES)}."
            )
        else:
            confluence = ConfluenceConfig(
                timeout=int(_env("CONFLUENCE_TIMEOUT") or 300),
                url=conf_url.rstrip("/"),
                user=user,
                password=password,
                spaces=spaces,
                verify_ssl=verify_ssl,
                strategy=strategy,
            )

    if not jira_url and not conf_url:
        errors.append("Nenhuma fonte configurada: defina JIRA_URL e/ou CONFLUENCE_URL.")

    return Config(
        store_path=store_path,
        qdrant_url=qdrant_url.rstrip("/"),
        collection=collection,
        fastembed_cache_dir=cache_dir,
        allow_model_download=allow_download,
        jira=jira,
        confluence=confluence,
        _errors=tuple(errors),
    )
