"""Pré-cache do modelo BM25 para operação offline.

Rode este script UMA VEZ em uma máquina com saída para a internet e copie o
diretório resultante para o host do indexador. O runtime nunca baixa nada por
conta própria: se o artefato faltar, ele falha dizendo o que falta.

    FASTEMBED_CACHE_DIR=./models/fastembed ALLOW_MODEL_DOWNLOAD=1 \
        python -m scripts.precache_models
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from config import load_config, setup_logging


def _fingerprint(directory: Path) -> tuple[int, int, str]:
    digest = hashlib.sha256()
    files = sorted(p for p in directory.rglob("*") if p.is_file())
    total = 0
    for path in files:
        digest.update(str(path.relative_to(directory)).encode("utf-8"))
        digest.update(path.read_bytes())
        total += path.stat().st_size
    return len(files), total, digest.hexdigest()[:16]


def main() -> int:
    setup_logging()
    cfg = load_config()
    cache_dir = cfg.fastembed_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)

    if not cfg.allow_model_download:
        print(
            "ALLOW_MODEL_DOWNLOAD não está ligado. Este script existe justamente "
            "para baixar; rode com ALLOW_MODEL_DOWNLOAD=1.",
            file=sys.stderr,
        )
        return 2

    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.pop("TRANSFORMERS_OFFLINE", None)

    from indexer.index import BM25_LANGUAGE, BM25_MODEL

    from fastembed import SparseTextEmbedding

    print(f"baixando {BM25_MODEL} para {cache_dir} ...", file=sys.stderr)
    model = SparseTextEmbedding(
        model_name=BM25_MODEL, cache_dir=str(cache_dir), language=BM25_LANGUAGE
    )
    sample = list(model.embed(["certificado vencido no gateway"]))
    if not sample or len(sample[0].indices) == 0:
        print("o modelo carregou mas não produziu vetor; abortando.", file=sys.stderr)
        return 1

    files, total_bytes, digest = _fingerprint(cache_dir)
    print(f"cache pronto em: {cache_dir}")
    print(f"arquivos: {files}  tamanho: {total_bytes / 1024:.1f} KiB  fingerprint: {digest}")
    print("copie este diretório inteiro para FASTEMBED_CACHE_DIR no host restrito.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
