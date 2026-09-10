"""Leitura dos logs para a tela: só o fim do arquivo, e tolerante a não-JSON.

O log da aplicação é uma linha JSON por evento, mas o arquivo NÃO é só isso: um
traceback do Python cai ali em várias linhas cruas — foi assim que a perda da
permissão do Confluence apareceu. Quem lê tem que aguentar as duas coisas.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Quanto do fim do arquivo lemos. Um log noturno chega a dezenas de MB; carregar
# tudo para mostrar as últimas 200 linhas seria desperdício.
CAUDA_BYTES = 512 * 1024

RESUMOS = ("concluída", "concluído", "finalizada", "abortada", "resolvido", "removido")

# Barra de progresso do carregamento de modelo. O tqdm escreve com \r, e o
# splitlines transforma cada atualização numa linha: são centenas delas por
# rodada. Não é evento e não é erro — sem este filtro a tela pinta tudo de
# vermelho junto com os traceback de verdade.
_PROGRESSO_RE = re.compile(r"\d+%\|| ?\d+(\.\d+)?(it/s|s/it)\]")

# Ruído do runtime ROCm empacotado no wheel do torch: duas linhas no import e
# mais duas na inicialização do HIP, sempre com este texto exato e nada mais.
# Está registrado no SETUP.md §12 como não-defeito, com a recomendação de
# filtrar no log. Casamento EXATO de propósito: qualquer outra mensagem de
# "No such file or directory" é informação de verdade e continua aparecendo.
_RUIDO_ROCM = "(null): No such file or directory"


def list_files(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    arquivos = []
    for p in sorted(directory.glob("*.log")):
        st = p.stat()
        arquivos.append({"nome": p.name, "bytes": st.st_size, "mtime": st.st_mtime})
    return sorted(arquivos, key=lambda a: -a["mtime"])


def tail(path: Path, limit: int = 200) -> list[str]:
    with path.open("rb") as fh:
        fh.seek(0, 2)
        tamanho = fh.tell()
        fh.seek(max(0, tamanho - CAUDA_BYTES))
        bruto = fh.read()
    texto = bruto.decode("utf-8", errors="replace")
    linhas = texto.splitlines()
    # A primeira linha pode ter sido cortada no meio pelo seek.
    if tamanho > CAUDA_BYTES and linhas:
        linhas = linhas[1:]
    return linhas[-limit:]


def parse(linhas: list[str]) -> list[dict[str, Any]]:
    """Uma linha JSON vira evento; qualquer outra vira evento de nível RAW."""
    eventos: list[dict[str, Any]] = []
    for linha in linhas:
        if not linha.strip():
            continue
        if _PROGRESSO_RE.search(linha) or linha.strip() == _RUIDO_ROCM:
            continue
        if linha.lstrip().startswith("{"):
            try:
                registro = json.loads(linha)
            except ValueError:
                eventos.append({"level": "RAW", "msg": linha})
                continue
            if not isinstance(registro, dict):
                eventos.append({"level": "RAW", "msg": linha})
                continue
            extra = {
                k: v for k, v in registro.items()
                if k not in ("ts", "level", "logger", "msg")
            }
            eventos.append({
                "ts": registro.get("ts", ""),
                "level": str(registro.get("level", "INFO")),
                "logger": registro.get("logger", ""),
                "msg": str(registro.get("msg", "")),
                "extra": extra,
            })
        else:
            eventos.append({"level": "RAW", "msg": linha})
    return eventos


def only_summaries(eventos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filtro de "só o que fecha uma etapa", para a tela não virar cachoeira."""
    return [
        e for e in eventos
        if e.get("level") in ("ERROR", "WARNING", "RAW")
        or any(p in e.get("msg", "") for p in RESUMOS)
    ]


__all__ = ["CAUDA_BYTES", "list_files", "only_summaries", "parse", "tail"]
