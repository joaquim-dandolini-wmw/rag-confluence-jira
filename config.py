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
import re
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
# Declarado no schema desde a Fase 1 para não exigir migração na Fase 2.
DENSE_VECTOR_SIZE: Final[int] = 1024

# O e5-large produz 1024 dimensões, que é o que a coleção já declara. Trocar
# de modelo exige um de mesma dimensão ou recriar a coleção.
DEFAULT_EMBED_MODEL: Final[str] = "intfloat/multilingual-e5-large"
# "cuda" deixa o código escolher a GPU discreta pelo número de compute units;
# "cuda:N" fixa um índice. Ver indexer/embeddings.py:_resolve_device.
EMBED_DEVICES: Final[tuple[str, ...]] = ("cpu", "cuda")
_EMBED_DEVICE_RE = re.compile(r"^(cpu|cuda(:\d+)?)$")
# Medido na RX 6900 XT (gfx1030): a vazão satura em batch 8 e piora acima de 32,
# porque o padding até o maior item do lote passa a desperdiçar cálculo. 16 dá a
# vazão máxima (89 fatias/s) com 1,87 GiB de pico em vez dos 13,77 GiB do 256.
DEFAULT_EMBED_BATCH_SIZE: Final[int] = 16

SEARCH_MODES: Final[tuple[str, ...]] = ("bm25", "dense", "hybrid", "auto")

# Transporte do servidor MCP.
#   stdio            um processo por cliente, alcançado por SSH. Autenticação é
#                    a chave SSH, e nada escuta na rede.
#   streamable-http  um serviço só, alcançado por URL. Sem chave por pessoa, e
#                    também sem identificação de quem perguntou o quê.
MCP_TRANSPORTS: Final[tuple[str, ...]] = ("stdio", "streamable-http", "sse")
# 127.0.0.1 de propósito: o padrão do CÓDIGO não expõe nada. Quem quer atender
# a rede põe 0.0.0.0 no .env, deliberadamente, e cuida do firewall.
DEFAULT_MCP_HOST: Final[str] = "127.0.0.1"
DEFAULT_MCP_PORT: Final[int] = 8765
DEFAULT_MCP_PATH: Final[str] = "/mcp"

# Pesos da fusão RRF, na ordem (bm25, denso). NÃO são iguais de propósito: com
# peso igual o denso derruba o acerto exato do BM25 em identificador, que é
# justamente onde o denso não tem o que oferecer. Valores medidos - ver
# SETUP.md. k é o denominador do RRF: 1/(k + rank).
DEFAULT_RRF_WEIGHT_BM25: Final[float] = 2.0
DEFAULT_RRF_WEIGHT_DENSE: Final[float] = 1.0
DEFAULT_RRF_K: Final[int] = 60

# Reranker cross-encoder. Mesmo backbone XLM-R large do e5, então o
# comportamento em português é o já conhecido. Descartado o jina-reranker
# porque exige trust_remote_code, e executar código remoto num deploy interno
# offline não vale o meio GB economizado.
DEFAULT_RERANK_MODEL: Final[str] = "BAAI/bge-reranker-v2-m3"
# Quantos candidatos da primeira etapa entram no reranker. Medido nesta base,
# mais candidatos PIORA: o alvo de sinônimo caiu da posição 3 para 5 quando fui
# de 20 para 80 candidatos, porque mais competição dilui. E cada candidato custa
# um forward de cross-encoder. 20 e 30 deram o mesmo resultado; fica 30 pela
# margem, a 475 ms contra 446 ms.
DEFAULT_RERANK_CANDIDATES: Final[int] = 30
DEFAULT_RERANK_BATCH_SIZE: Final[int] = 16


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
class EmbeddingConfig:
    """Vetor denso da Fase 2.

    O device sai do ambiente e não do código porque a mesma instalação roda a
    carga inicial na GPU (~28 min) e o incremental de cron onde der. O PyTorch
    com build ROCm expõe a GPU da AMD pela MESMA API "cuda" — não existe device
    "rocm" nem "hip".
    """

    model_name: str
    cache_dir: Path
    # "cpu", "cuda" (escolhe a GPU discreta sozinho) ou "cuda:N" para fixar.
    device: str
    batch_size: int
    allow_download: bool


@dataclass(frozen=True)
class McpConfig:
    """Como o servidor MCP é alcançado.

    Trocável por ambiente porque a escolha não é técnica, é de operação: com
    poucos clientes, stdio por SSH dá autenticação por pessoa e revogação
    individual; com muitos, HTTP elimina a configuração por pessoa mas passa a
    atender qualquer um que alcance a porta.
    """

    transport: str
    host: str
    port: int
    path: str
    # Caminhos do certificado e da chave. Vazios = HTTP puro. O SDK não expõe
    # TLS no run(), então quando isto está preenchido o servidor serve o app
    # com uvicorn diretamente, que tem.
    tls_cert: Path | None
    tls_key: Path | None
    # O SDK valida o cabeçalho Host contra esta lista (proteção contra DNS
    # rebinding) e recusa o que não estiver nela. Bindar em 0.0.0.0 não basta:
    # sem o endereço que o cliente digita nesta lista, a requisição é rejeitada.
    # Aceita curinga de porta ("10.2.1.132:*"), não de host.
    allowed_hosts: tuple[str, ...]

    @property
    def scheme(self) -> str:
        return "https" if self.tls_cert else "http"

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}{self.path}"


@dataclass(frozen=True)
class RerankConfig:
    """Segunda etapa da busca: reordena o que a primeira trouxe.

    Desligável por ambiente porque é a etapa mais cara da consulta e a única
    que pode ser trocada sem reindexar nada — o reranker não grava vetor, só
    reordena.
    """

    model_name: str
    cache_dir: Path
    device: str
    batch_size: int
    candidates: int
    enabled: bool
    allow_download: bool


@dataclass(frozen=True)
class Config:
    store_path: Path
    qdrant_url: str
    collection: str
    fastembed_cache_dir: Path
    allow_model_download: bool
    embedding: EmbeddingConfig
    rerank: RerankConfig
    mcp: McpConfig
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


def _primary_ipv4() -> str | None:
    """IP de saída desta máquina, sem enviar pacote nenhum.

    O socket UDP só resolve a rota; não há tráfego. Serve para descobrir o
    endereço que os clientes da rede vão digitar, sem depender de `ip addr`.
    """
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return str(sock.getsockname()[0])
    except OSError:
        return None


def _default_allowed_hosts(bind_host: str) -> tuple[str, ...]:
    hosts = ["localhost:*", "127.0.0.1:*"]
    if bind_host not in {"127.0.0.1", "localhost", "0.0.0.0", "::"}:
        hosts.append(f"{bind_host}:*")
    if bind_host in {"0.0.0.0", "::"}:
        # Bind em todas as interfaces: o cliente vai digitar o IP da máquina,
        # que é o que precisa entrar na lista.
        proprio = _primary_ipv4()
        if proprio:
            hosts.append(f"{proprio}:*")
    return tuple(dict.fromkeys(hosts))


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

    embed_device = (_env("EMBED_DEVICE") or "cuda").lower()
    if not _EMBED_DEVICE_RE.match(embed_device):
        errors.append(
            f"EMBED_DEVICE={embed_device!r} desconhecido. Aceitos: "
            f"{', '.join(EMBED_DEVICES)} ou \"cuda:N\". O PyTorch com build "
            "ROCm usa \"cuda\" para a GPU da AMD; não existe device \"rocm\" "
            "nem \"hip\"."
        )
    raw_batch = _env("EMBED_BATCH_SIZE")
    try:
        embed_batch = int(raw_batch) if raw_batch else DEFAULT_EMBED_BATCH_SIZE
    except ValueError:
        embed_batch = DEFAULT_EMBED_BATCH_SIZE
        errors.append(f"EMBED_BATCH_SIZE={raw_batch!r} não é um inteiro.")
    if embed_batch < 1:
        errors.append(f"EMBED_BATCH_SIZE={embed_batch} precisa ser >= 1.")
    rerank_device = (_env("RERANK_DEVICE") or embed_device).lower()
    if not _EMBED_DEVICE_RE.match(rerank_device):
        errors.append(
            f"RERANK_DEVICE={rerank_device!r} desconhecido. Aceitos: "
            f"{', '.join(EMBED_DEVICES)} ou \"cuda:N\"."
        )
    raw_cand = _env("RERANK_CANDIDATES")
    try:
        rerank_cand = int(raw_cand) if raw_cand else DEFAULT_RERANK_CANDIDATES
    except ValueError:
        rerank_cand = DEFAULT_RERANK_CANDIDATES
        errors.append(f"RERANK_CANDIDATES={raw_cand!r} não é um inteiro.")
    raw_rb = _env("RERANK_BATCH_SIZE")
    try:
        rerank_batch = int(raw_rb) if raw_rb else DEFAULT_RERANK_BATCH_SIZE
    except ValueError:
        rerank_batch = DEFAULT_RERANK_BATCH_SIZE
        errors.append(f"RERANK_BATCH_SIZE={raw_rb!r} não é um inteiro.")
    rerank = RerankConfig(
        model_name=_env("RERANK_MODEL") or DEFAULT_RERANK_MODEL,
        cache_dir=Path(_env("RERANK_CACHE_DIR") or REPO_ROOT / "models" / "reranker"),
        device=rerank_device,
        batch_size=max(rerank_batch, 1),
        candidates=max(rerank_cand, 1),
        enabled=_env_bool("RERANK_ENABLED", True),
        allow_download=allow_download,
    )

    mcp_transport = (_env("MCP_TRANSPORT") or "stdio").lower()
    if mcp_transport not in MCP_TRANSPORTS:
        errors.append(
            f"MCP_TRANSPORT={mcp_transport!r} desconhecido. Aceitos: "
            f"{', '.join(MCP_TRANSPORTS)}."
        )
    raw_port = _env("MCP_PORT")
    try:
        mcp_port = int(raw_port) if raw_port else DEFAULT_MCP_PORT
    except ValueError:
        mcp_port = DEFAULT_MCP_PORT
        errors.append(f"MCP_PORT={raw_port!r} não é um inteiro.")
    mcp_host = _env("MCP_HOST") or DEFAULT_MCP_HOST
    permitidos = _env_list("MCP_ALLOWED_HOSTS")
    if not permitidos:
        permitidos = _default_allowed_hosts(mcp_host)
    cert = _env("MCP_TLS_CERT")
    key = _env("MCP_TLS_KEY")
    if bool(cert) != bool(key):
        errors.append(
            "MCP_TLS_CERT e MCP_TLS_KEY vão juntos: definir só um deixaria o "
            "servidor em HTTP puro sem avisar."
        )
        cert = key = None
    for nome, caminho in (("MCP_TLS_CERT", cert), ("MCP_TLS_KEY", key)):
        if caminho and not Path(caminho).is_file():
            errors.append(f"{nome}={caminho!r} não existe.")
    mcp = McpConfig(
        transport=mcp_transport,
        host=mcp_host,
        port=mcp_port,
        path=_env("MCP_PATH") or DEFAULT_MCP_PATH,
        allowed_hosts=permitidos,
        tls_cert=Path(cert) if cert else None,
        tls_key=Path(key) if key else None,
    )

    embedding = EmbeddingConfig(
        model_name=_env("EMBED_MODEL") or DEFAULT_EMBED_MODEL,
        cache_dir=Path(_env("EMBED_CACHE_DIR") or REPO_ROOT / "models" / "e5"),
        device=embed_device,
        batch_size=embed_batch,
        allow_download=allow_download,
    )

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
        embedding=embedding,
        rerank=rerank,
        mcp=mcp,
        jira=jira,
        confluence=confluence,
        _errors=tuple(errors),
    )
