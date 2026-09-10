"""Agenda das rodadas: lê e escreve o bloco do painel no crontab do usuário.

Separado do servidor de propósito. Aqui está a única parte perigosa do painel —
reescrever crontab — e ela é feita por funções puras, testáveis sem web e sem
tocar no crontab de verdade: `render_block` monta as linhas, `merge` costura o
bloco no arquivo existente PRESERVANDO o resto.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# O bloco é delimitado. Nunca reescrevemos o crontab inteiro: o usuário pode ter
# jobs que não são deste projeto, e perdê-los em silêncio seria imperdoável.
BEGIN = "# BEGIN rag-painel — gerado pelo painel, não edite à mão"
END = "# END rag-painel"

SOURCES = ("confluence", "jira")
_HORA_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ScheduleError(ValueError):
    """Payload de agenda inválido."""


@dataclass(frozen=True)
class Schedule:
    """Uma janela por dia, que é o modo como este sistema é operado."""

    hora: str = "00:00"
    fontes: tuple[str, ...] = SOURCES
    contar_anexos: bool = False
    reconcile_domingo: bool = True
    ativo: bool = True

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fontes"] = list(self.fontes)
        return d


def parse(payload: dict[str, Any]) -> Schedule:
    """Valida o que veio da tela. Nada daqui vai para o crontab sem passar aqui."""
    hora = str(payload.get("hora", "00:00")).strip()
    if not _HORA_RE.match(hora):
        raise ScheduleError(f"hora inválida: {hora!r} — use HH:MM em 24 h")

    brutas = payload.get("fontes", list(SOURCES))
    if isinstance(brutas, str):
        brutas = [brutas]
    fontes = tuple(f for f in SOURCES if f in set(brutas))
    if not fontes:
        raise ScheduleError("escolha ao menos uma fonte: confluence ou jira")

    return Schedule(
        hora=hora,
        fontes=fontes,
        contar_anexos=bool(payload.get("contar_anexos", False)),
        reconcile_domingo=bool(payload.get("reconcile_domingo", True)),
        ativo=bool(payload.get("ativo", True)),
    )


def _comando(repo: Path, agenda: Schedule, *, reconcile: bool) -> str:
    py = f"{repo}/.venv/bin/python"
    base = f"env PYTHONPATH={repo} {py} -m indexer.sync run"
    if len(agenda.fontes) == 1:
        base += f" --only {agenda.fontes[0]}"
    if not agenda.contar_anexos:
        base += " --no-count-attachments"
    if not reconcile:
        return base
    # O reconcile entra ENCADEADO, depois que a rodada termina, e dentro do
    # mesmo lock — não agendado em paralelo, que disputaria o mesmo SQLite.
    rec = f"env PYTHONPATH={repo} {py} -m indexer.sync reconcile --only jira"
    return f"bash -c '{base} && {rec}'"


def render_block(agenda: Schedule, repo: Path) -> str:
    """Monta o bloco do crontab. Sempre com flock: uma rodada por vez.

    O `-n` faz a segunda desistir na hora em vez de enfileirar: se uma noite
    passar da meia-noite seguinte, é melhor pular do que ter duas rodadas
    brigando pelo store.
    """
    hh, mm = agenda.hora.split(":")
    quando = f"{int(mm)} {int(hh)}"
    lock = "flock -n /tmp/rag-sync.lock"
    log = "logs/noturno.log"
    prefixo = "" if agenda.ativo else "# (desativado no painel) "

    linhas = [BEGIN, f"# fontes: {', '.join(agenda.fontes)} — janela diária às {agenda.hora}"]
    if agenda.reconcile_domingo and "jira" in agenda.fontes:
        linhas += [
            f"{prefixo}{quando} * * 1-6 cd {repo} && {lock} "
            f"{_comando(repo, agenda, reconcile=False)} >> {log} 2>&1",
            f"{prefixo}{quando} * * 0 cd {repo} && {lock} "
            f"{_comando(repo, agenda, reconcile=True)} >> {log} 2>&1",
        ]
    else:
        linhas.append(
            f"{prefixo}{quando} * * * cd {repo} && {lock} "
            f"{_comando(repo, agenda, reconcile=False)} >> {log} 2>&1"
        )
    linhas.append(END)
    return "\n".join(linhas)


def merge(atual: str, bloco: str) -> str:
    """Troca o bloco do painel no crontab, mantendo todo o resto intacto.

    Sem bloco anterior, acrescenta no fim. É idempotente: aplicar duas vezes o
    mesmo bloco dá o mesmo arquivo.
    """
    linhas = atual.splitlines()
    fora: list[str] = []
    dentro = False
    achou = False
    for linha in linhas:
        if linha.startswith(BEGIN):
            dentro = achou = True
            continue
        if dentro and linha.startswith(END):
            dentro = False
            continue
        if not dentro:
            fora.append(linha)

    if achou:
        # Recoloca o bloco onde ele estava: no fim do que sobrou, sem duplicar.
        while fora and not fora[-1].strip():
            fora.pop()
    novo = "\n".join([*fora, bloco]) if fora else bloco
    return novo.rstrip("\n") + "\n"


def read_crontab() -> str:
    """O crontab do usuário. Ausente devolve vazio, que não é erro."""
    p = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else ""


def write_crontab(conteudo: str) -> None:
    p = subprocess.run(["crontab", "-"], input=conteudo, capture_output=True, text=True)
    if p.returncode != 0:
        raise ScheduleError(f"crontab recusou: {p.stderr.strip() or p.returncode}")


def block_of(crontab: str) -> str:
    """Extrai o bloco do painel de um crontab, para a tela mostrar o que está no ar."""
    dentro = False
    linhas: list[str] = []
    for linha in crontab.splitlines():
        if linha.startswith(BEGIN):
            dentro = True
        if dentro:
            linhas.append(linha)
        if dentro and linha.startswith(END):
            break
    return "\n".join(linhas)


def load(caminho: Path) -> Schedule:
    if not caminho.exists():
        return Schedule()
    try:
        return parse(json.loads(caminho.read_text()))
    except (ValueError, OSError):
        return Schedule()


def save(caminho: Path, agenda: Schedule) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(agenda.as_dict(), ensure_ascii=False, indent=2) + "\n")


__all__ = [
    "BEGIN", "END", "SOURCES", "Schedule", "ScheduleError",
    "block_of", "load", "merge", "parse", "read_crontab", "render_block",
    "save", "write_crontab",
]
