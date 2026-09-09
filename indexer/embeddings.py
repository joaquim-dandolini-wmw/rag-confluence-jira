"""Vetor denso da Fase 2: intfloat/multilingual-e5-large.

Fecha a lacuna que o BM25 não fecha. O BM25 acerta identificador exato
(VENDAS-14993, PRUPSYNCPRODUTOS) e erra sinônimo; o denso faz o inverso. Os dois
convivem no mesmo ponto do Qdrant e são fundidos por RRF na consulta — ver
indexer/index.py.

Três decisões que não são óbvias e que quebram silenciosamente se invertidas:

  1. O e5 foi treinado com prefixo de instrução. Consulta leva "query: " e
     documento leva "passage: ". Sem eles a recuperação piora de forma visível:
     medido neste store, "boleto bancário do pedido" traz a página certa com
     prefixo e traz "Foto do Pedido" sem.

  2. O texto do denso NÃO passa por fold_accents(). Aquela função existe porque
     o tokenizer do BM25 não normaliza diacrítico; para um transformer
     multilingual o acento carrega significado e remover piora o resultado. O
     denso consome o texto ORIGINAL.

  3. Os embeddings são normalizados em L2 porque a coleção usa distância de
     cosseno. Com vetor não normalizado o Qdrant ainda responde, só responde
     errado.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence, cast

from config import DENSE_VECTOR_SIZE, EmbeddingConfig

LOG = logging.getLogger("indexer.embeddings")

QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "


class EmbeddingError(RuntimeError):
    """Falha de configuração do embedder: aborta a execução."""


class Encoder(Protocol):
    """O mínimo que o embedder usa de um SentenceTransformer.

    Existe para que o teste injete um dublê sem GPU e sem rede.
    """

    def encode(
        self,
        sentences: list[str],
        *,
        batch_size: int,
        normalize_embeddings: bool,
        show_progress_bar: bool,
    ) -> Any: ...


def _model_dir_name(model_name: str) -> str:
    """Nome do diretório do modelo no cache do huggingface_hub."""
    return "models--" + model_name.replace("/", "--")


def _precache_hint(cfg: EmbeddingConfig) -> str:
    return (
        f"não foi possível carregar {cfg.model_name} a partir de {cfg.cache_dir}.\n\n"
        "Esta máquina está em modo offline por padrão e nunca baixa modelo em "
        "silêncio. Falta o diretório:\n"
        f"    {cfg.cache_dir / _model_dir_name(cfg.model_name)}\n\n"
        "Em um host com rede, rode:\n"
        f"    EMBED_CACHE_DIR={cfg.cache_dir} ALLOW_MODEL_DOWNLOAD=1 "
        "python -m scripts.precache_models\n"
        f"e copie o diretório {cfg.cache_dir} inteiro para cá (~2,2 GB).\n"
        "Para permitir o download diretamente daqui, defina ALLOW_MODEL_DOWNLOAD=1."
    )


# Uma iGPU tem 1 ou 2 compute units; a RX 6900 XT tem 40. É o único sinal
# confiável para distinguir as duas: a iGPU do Ryzen reporta 15,11 GiB de
# "VRAM" (memória do sistema compartilhada), quase igual aos 15,98 GiB da
# discreta, então tamanho de memória não serve para escolher.
_MIN_COMPUTE_UNITS = 4


def _resolve_device(requested: str, torch: Any) -> str:
    """Resolve "cuda" para o índice da GPU discreta, e recusa a integrada.

    Não use HIP_VISIBLE_DEVICES para isso. Medido nesta máquina: a ordenação
    que o HIP aplica a essa variável NÃO é a mesma que o torch usa na
    enumeração padrão, então HIP_VISIBLE_DEVICES=0 pode selecionar a iGPU
    mesmo quando o device 0 do torch é a discreta. O resultado é uma execução
    que funciona, não dá erro nenhum e roda ~7x mais lenta num chip de 1 CU.
    Escolher pelo número de compute units não depende de ordenação.
    """
    if requested == "cpu":
        return "cpu"

    if not torch.cuda.is_available() or torch.cuda.device_count() == 0:
        raise EmbeddingError(
            f"EMBED_DEVICE={requested} mas o PyTorch não vê nenhuma GPU.\n"
            f"torch={torch.__version__} hip={torch.version.hip}\n"
            "Confira com `PATH=/opt/rocm/bin:$PATH rocminfo | grep gfx` e veja o "
            "SETUP.md seção 4. Para rodar na CPU use EMBED_DEVICE=cpu - custa "
            "~24x mais (medido: 3,7 contra 89 fatias/s)."
        )

    disponiveis = [
        (indice, torch.cuda.get_device_properties(indice))
        for indice in range(torch.cuda.device_count())
    ]

    if ":" in requested:
        indice = int(requested.split(":", 1)[1])
        if indice >= torch.cuda.device_count():
            raise EmbeddingError(
                f"EMBED_DEVICE={requested} mas só há "
                f"{torch.cuda.device_count()} GPU(s) visível(is): "
                + ", ".join(f"{i}={p.gcnArchName}" for i, p in disponiveis)
            )
        escolhido, props = indice, dict(disponiveis)[indice]
    else:
        escolhido, props = max(disponiveis, key=lambda par: par[1].multi_processor_count)

    if props.multi_processor_count < _MIN_COMPUTE_UNITS:
        raise EmbeddingError(
            f"a única GPU visível é {props.gcnArchName} com "
            f"{props.multi_processor_count} compute unit(s) - é a GPU INTEGRADA "
            "do processador, não a placa discreta.\n\n"
            "Embedar nela funciona mas é ordens de grandeza mais lento, então o "
            "comando aborta em vez de rodar por horas em silêncio.\n\n"
            "Verifique se a placa discreta está no barramento:\n"
            "    lspci | grep -iE 'vga|display'\n"
            "    PATH=/opt/rocm/bin:$PATH rocminfo | grep -E 'Name:.*gfx'\n\n"
            "Se ela não aparecer, o barramento perdeu a placa: só um "
            "desligamento completo (não um reboot) a traz de volta.\n"
            "Para rodar na CPU de propósito, use EMBED_DEVICE=cpu."
        )

    LOG.info(
        "GPU escolhida para o vetor denso",
        extra={
            "device": f"cuda:{escolhido}",
            "arch": props.gcnArchName,
            "compute_units": props.multi_processor_count,
            "vram_gib": round(props.total_memory / 2**30, 2),
            "candidatas": {
                str(i): f"{p.gcnArchName}/{p.multi_processor_count}CU"
                for i, p in disponiveis
            },
        },
    )
    return f"cuda:{escolhido}"


def _load_encoder(cfg: EmbeddingConfig) -> Encoder:
    """Carrega o e5 do cache local, sem tentar baixar em silêncio.

    A ordem aqui importa: as variáveis de ambiente precisam estar postas ANTES
    de o torch e o huggingface_hub serem importados, senão elas não têm efeito.
    """
    if not cfg.allow_download:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HOME", str(cfg.cache_dir))
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)

    model_dir = cfg.cache_dir / _model_dir_name(cfg.model_name)
    if not cfg.allow_download and not model_dir.is_dir():
        raise EmbeddingError(_precache_hint(cfg))

    # Importados tarde: respeitam o ambiente montado acima.
    import torch
    from sentence_transformers import SentenceTransformer

    device = _resolve_device(cfg.device, torch)

    # fp16 na GPU: metade da VRAM e resultado que confere com fp32 em 4 casas
    # decimais (medido). Na CPU fp16 não tem ganho e degrada, então fica fp32.
    # transformers < 5 chama o argumento torch_dtype; a 5.x renomeou para dtype
    # e o antigo emite deprecation. Tenta o novo e cai para o antigo.
    fp16_kwargs: list[dict[str, Any]] = (
        [{"dtype": torch.float16}, {"torch_dtype": torch.float16}]
        if device != "cpu"
        else [{}]
    )
    encoder = None
    last_exc: Exception | None = None
    for model_kwargs in fp16_kwargs:
        try:
            encoder = SentenceTransformer(
                cfg.model_name,
                device=device,
                cache_folder=str(cfg.cache_dir),
                model_kwargs=model_kwargs,
                # Explícito, e não só por HF_HUB_OFFLINE: o huggingface_hub lê
                # aquela variável no import do módulo, e algo na cadeia do
                # qdrant-client/fastembed já o importou antes de chegarmos aqui.
                # Sem isto o loader ainda bate na huggingface.co.
                local_files_only=not cfg.allow_download,
            )
            break
        except TypeError as exc:
            last_exc = exc
            continue
        except Exception as exc:  # noqa: BLE001 - falha aqui é fatal e precisa de mensagem
            raise EmbeddingError(
                f"{_precache_hint(cfg)}\n\nCausa: {type(exc).__name__}: {exc}"
            ) from exc
    if encoder is None:
        raise EmbeddingError(
            f"{_precache_hint(cfg)}\n\nCausa: {type(last_exc).__name__}: {last_exc}"
        ) from last_exc

    # sentence-transformers 5.x renomeou get_sentence_embedding_dimension para
    # get_embedding_dimension; o nome antigo ainda existe mas avisa deprecação.
    medir = getattr(encoder, "get_embedding_dimension", None) or (
        encoder.get_sentence_embedding_dimension
    )
    dim = medir()
    if dim != DENSE_VECTOR_SIZE:
        raise EmbeddingError(
            f"{cfg.model_name} produz {dim} dimensões, mas a coleção declara "
            f"{DENSE_VECTOR_SIZE}. Uma coleção do Qdrant não pode mudar a "
            "dimensão de um vetor depois de criada: escolha um modelo de "
            f"{DENSE_VECTOR_SIZE} dimensões ou recrie a coleção do zero — o que "
            "descarta as 148 mil fatias já indexadas."
        )
    LOG.info(
        "encoder denso carregado",
        extra={"modelo": cfg.model_name, "device": device,
               "batch_size": cfg.batch_size, "dimensoes": dim},
    )
    return cast(Encoder, encoder)


class DenseEmbedder:
    """Gera os vetores densos, aplicando prefixo e normalização L2.

    O encoder é carregado na primeira utilização, não na construção: o comando
    `status` monta a configuração inteira e não deve pagar 1,4 s de carga de
    modelo nem exigir GPU.
    """

    def __init__(self, cfg: EmbeddingConfig, *, encoder: Encoder | None = None) -> None:
        self._cfg = cfg
        self._encoder = encoder

    @property
    def device(self) -> str:
        return self._cfg.device

    @property
    def batch_size(self) -> int:
        return self._cfg.batch_size

    @property
    def model_name(self) -> str:
        return self._cfg.model_name

    @property
    def encoder(self) -> Encoder:
        if self._encoder is None:
            self._encoder = _load_encoder(self._cfg)
        return self._encoder

    def _encode(self, texts: list[str], batch_size: int) -> list[list[float]]:
        if not texts:
            return []
        raw = self.encoder.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        vectors = [[float(value) for value in row] for row in raw]
        for vector in vectors:
            if len(vector) != DENSE_VECTOR_SIZE:
                raise EmbeddingError(
                    f"o encoder devolveu {len(vector)} dimensões, esperado "
                    f"{DENSE_VECTOR_SIZE}."
                )
        return vectors

    def embed_passages(
        self, texts: Sequence[str], *, batch_size: int | None = None
    ) -> list[list[float]]:
        """Vetores para indexação. Recebe o texto ORIGINAL, com acento e tudo."""
        prefixed = [PASSAGE_PREFIX + text for text in texts]
        return self._encode(prefixed, batch_size or self._cfg.batch_size)

    def embed_query(self, query: str) -> list[float]:
        """Vetor para consulta. Prefixo diferente do de passagem, de propósito."""
        vectors = self._encode([QUERY_PREFIX + query], 1)
        return vectors[0]

    def warmup(self) -> None:
        """Paga a carga do modelo e o primeiro kernel antes de medir vazão."""
        self.embed_passages(["aquecimento"], batch_size=1)


def iter_batches(items: Sequence[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


__all__ = [
    "DenseEmbedder",
    "EmbeddingError",
    "Encoder",
    "PASSAGE_PREFIX",
    "QUERY_PREFIX",
    "iter_batches",
]
