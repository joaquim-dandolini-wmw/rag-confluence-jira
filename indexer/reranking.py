"""Reranker cross-encoder: reordena os candidatos que a busca trouxe.

Por que existe, com número medido nesta base. O vetor denso ACHA o documento
por sinônimo, mas não o coloca no topo: dos três alvos de sinônimo puro, um
ficou na posição 8 e os outros dois em 13 e 28. Estão no índice e fora de uma
página de 10 resultados, o que na prática é o mesmo que não achar.

A causa é estrutural do bi-encoder: consulta e documento são vetorizados
SEPARADAMENTE, então o vetor do documento tem que servir para toda consulta
possível. Um cross-encoder lê os dois JUNTOS num único forward e pontua o par,
o que é muito mais preciso — e caro, por isso só roda sobre algumas dezenas de
candidatos que a primeira etapa já filtrou, nunca sobre as 148 mil fatias.

O modelo é o mesmo backbone XLM-R large do e5, então o comportamento em
português é o já conhecido. Fica em fp16 na mesma GPU: ~1,1 GiB de pesos ao
lado dos 1,87 GiB do e5, num teto de 15,98 GiB.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Protocol, Sequence, TypeVar, cast

from config import RerankConfig
from indexer.embeddings import EmbeddingError, _model_dir_name, _resolve_device

LOG = logging.getLogger("indexer.reranking")

# Os pares passam pelo tokenizer de um XLM-R: consulta + fatia. A fatia tem no
# máximo 1.400 chars, então 512 tokens cobrem o par inteiro na prática e é o
# comprimento com que o modelo foi treinado.
MAX_PAIR_TOKENS = 512


class RerankError(RuntimeError):
    """Falha de configuração do reranker: aborta a execução."""


class CrossEncoderLike(Protocol):
    """O mínimo que o reranker usa. Existe para o teste injetar um dublê."""

    def predict(
        self,
        sentences: list[tuple[str, str]],
        *,
        batch_size: int,
        show_progress_bar: bool,
    ) -> Any: ...


def _precache_hint(cfg: RerankConfig) -> str:
    return (
        f"não foi possível carregar {cfg.model_name} a partir de {cfg.cache_dir}.\n\n"
        "Esta máquina está em modo offline por padrão e nunca baixa modelo em "
        "silêncio. Falta o diretório:\n"
        f"    {cfg.cache_dir / _model_dir_name(cfg.model_name)}\n\n"
        "Em um host com rede, rode:\n"
        f"    RERANK_CACHE_DIR={cfg.cache_dir} ALLOW_MODEL_DOWNLOAD=1 "
        "python -m scripts.precache_models\n"
        f"e copie o diretório {cfg.cache_dir} inteiro para cá (~2,3 GB).\n"
        "Para desligar o reranker, use RERANK_ENABLED=0."
    )


def _load_cross_encoder(cfg: RerankConfig) -> CrossEncoderLike:
    if not cfg.allow_download:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)

    model_dir = cfg.cache_dir / _model_dir_name(cfg.model_name)
    if not cfg.allow_download and not model_dir.is_dir():
        raise RerankError(_precache_hint(cfg))

    import torch
    from sentence_transformers import CrossEncoder

    try:
        device = _resolve_device(cfg.device, torch)
    except EmbeddingError as exc:
        # A mensagem de _resolve_device já explica iGPU e placa ausente.
        raise RerankError(str(exc)) from exc

    kwargs: dict[str, Any] = {"dtype": torch.float16} if device != "cpu" else {}
    ultimo: Exception | None = None
    for model_kwargs in ([kwargs, {"torch_dtype": torch.float16}] if kwargs else [{}]):
        try:
            encoder = CrossEncoder(
                cfg.model_name,
                device=device,
                cache_folder=str(cfg.cache_dir),
                max_length=MAX_PAIR_TOKENS,
                model_kwargs=model_kwargs,
                # Explícito, não só por HF_HUB_OFFLINE: aquela variável é lida
                # no import do huggingface_hub, que já aconteceu antes daqui.
                local_files_only=not cfg.allow_download,
            )
            break
        except TypeError as exc:
            ultimo = exc
            continue
        except Exception as exc:  # noqa: BLE001 - falha aqui é fatal e precisa de mensagem
            raise RerankError(
                f"{_precache_hint(cfg)}\n\nCausa: {type(exc).__name__}: {exc}"
            ) from exc
    else:
        raise RerankError(
            f"{_precache_hint(cfg)}\n\nCausa: {type(ultimo).__name__}: {ultimo}"
        ) from ultimo

    LOG.info(
        "reranker carregado",
        extra={"modelo": cfg.model_name, "device": device,
               "batch_size": cfg.batch_size, "max_tokens": MAX_PAIR_TOKENS},
    )
    return cast(CrossEncoderLike, encoder)


T = TypeVar("T")


class CrossEncoderReranker:
    """Reordena candidatos pontuando o par (consulta, texto) de cada um.

    Carregado na primeira utilização, não na construção: o `status` e uma busca
    com o reranker desligado não devem pagar carga de modelo nem exigir GPU.
    """

    def __init__(
        self, cfg: RerankConfig, *, encoder: CrossEncoderLike | None = None
    ) -> None:
        self._cfg = cfg
        self._encoder = encoder

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    @property
    def candidates(self) -> int:
        return self._cfg.candidates

    @property
    def model_name(self) -> str:
        return self._cfg.model_name

    @property
    def encoder(self) -> CrossEncoderLike:
        if self._encoder is None:
            self._encoder = _load_cross_encoder(self._cfg)
        return self._encoder

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        """Pontua cada texto contra a consulta. Maior é mais relevante.

        A escala é logit do modelo, não probabilidade, e NÃO é comparável entre
        consultas diferentes — só serve para ordenar dentro de uma consulta.
        """
        if not texts:
            return []
        pares = [(query, texto) for texto in texts]
        brutos = self.encoder.predict(
            pares, batch_size=self._cfg.batch_size, show_progress_bar=False
        )
        return [float(valor) for valor in brutos]

    def rerank(
        self,
        query: str,
        items: Sequence[T],
        *,
        text_of: Any,
        limit: int,
    ) -> list[tuple[T, float]]:
        """Devolve os `limit` melhores itens, do melhor para o pior.

        `text_of` extrai o texto de cada item, para que este módulo não precise
        conhecer o SearchHit e continue testável com uma lista de strings.
        """
        if not items:
            return []
        pontuacoes = self.score(query, [text_of(item) for item in items])
        ordenados = sorted(
            zip(items, pontuacoes), key=lambda par: par[1], reverse=True
        )
        return ordenados[:limit]

    def warmup(self) -> None:
        self.score("aquecimento", ["aquecimento"])


__all__ = [
    "MAX_PAIR_TOKENS",
    "CrossEncoderLike",
    "CrossEncoderReranker",
    "RerankError",
]
