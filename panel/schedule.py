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
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# O bloco é delimitado. Nunca reescrevemos o crontab inteiro: o usuário pode ter
# jobs que não são deste projeto, e perdê-los em silêncio seria imperdoável.
BEGIN = "# BEGIN rag-painel — gerado pelo painel, não edite à mão"
END = "# END rag-painel"

SOURCES = ("confluence", "jira")
_HORA_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Intervalos que a tela oferece para o Jira. Fechado de propósito: cada um vira
# uma expressão de cron conhecida, e nada digitado livremente chega ao crontab.
INTERVALOS_JIRA = (15, 30, 60, 120)
DIAS_JIRA = ("todos", "uteis")

LOCK = "/tmp/rag-sync.lock"
# Quanto a rodada do Confluence espera por uma do Jira que esteja no meio.
# Uma rodada do Jira leva minutos; 30 min é folga de sobra, e ainda assim um
# limite: se algo travar o lock, a noite desiste em vez de empilhar.
ESPERA_CONFLUENCE_S = 1800
LOG_CONFLUENCE = "logs/noturno.log"
# A tela fala no horário de quem usa; o cron roda no relógio do servidor, que
# aqui é UTC. A conversão é feita ao montar o bloco — sem isso "Jira das 7h às
# 20h" rodaria das 4h às 17h de Brasília.
FUSO_DA_TELA = "America/Sao_Paulo"
LOG_JIRA = "logs/jira.log"


class ScheduleError(ValueError):
    """Payload de agenda inválido."""


@dataclass(frozen=True)
class Confluence:
    """Uma rodada por dia: o Confluence 4.2.4 não diz o que mudou, então cada
    rodada varre todas as páginas — horas, não minutos."""

    ativo: bool = True
    hora: str = "00:00"
    contar_anexos: bool = False


@dataclass(frozen=True)
class Jira:
    """Rodadas curtas ao longo do dia: o Jira é incremental pelo `updated`,
    então cada rodada só busca o que mudou desde a anterior."""

    ativo: bool = True
    intervalo_min: int = 30
    inicio: int = 7   # hora da primeira rodada
    fim: int = 20     # exclusivo: a última rodada é antes desta hora
    dias: str = "todos"
    reconcile_domingo: bool = True


@dataclass(frozen=True)
class Schedule:
    confluence: Confluence = field(default_factory=Confluence)
    jira: Jira = field(default_factory=Jira)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bool(bruto: dict[str, Any], chave: str, padrao: bool) -> bool:
    return bool(bruto.get(chave, padrao))


def _inteiro(valor: Any, nome: str) -> int:
    try:
        return int(valor)
    except (TypeError, ValueError):
        raise ScheduleError(f"{nome} inválido: {valor!r}") from None


def _do_formato_antigo(payload: dict[str, Any]) -> dict[str, Any]:
    """O agenda.json de antes tinha uma janela só para as duas fontes."""
    fontes = payload.get("fontes", list(SOURCES))
    if isinstance(fontes, str):
        fontes = [fontes]
    ativo = bool(payload.get("ativo", True))
    return {
        "confluence": {
            "ativo": ativo and "confluence" in fontes,
            "hora": payload.get("hora", "00:00"),
            "contar_anexos": payload.get("contar_anexos", False),
        },
        "jira": {
            "ativo": ativo and "jira" in fontes,
            "reconcile_domingo": payload.get("reconcile_domingo", True),
        },
    }


def parse(payload: dict[str, Any]) -> Schedule:
    """Valida o que veio da tela. Nada daqui vai para o crontab sem passar aqui."""
    if "confluence" not in payload and "jira" not in payload:
        payload = _do_formato_antigo(payload)

    c = payload.get("confluence") or {}
    j = payload.get("jira") or {}
    if not isinstance(c, dict) or not isinstance(j, dict):
        raise ScheduleError("formato de agenda inválido")

    hora = str(c.get("hora", "00:00")).strip()
    if not _HORA_RE.match(hora):
        raise ScheduleError(f"hora inválida: {hora!r} — use HH:MM em 24 h")

    intervalo = _inteiro(j.get("intervalo_min", 30), "intervalo do Jira")
    if intervalo not in INTERVALOS_JIRA:
        raise ScheduleError(
            f"intervalo do Jira inválido: {intervalo} — use "
            + ", ".join(str(i) for i in INTERVALOS_JIRA) + " minutos"
        )
    inicio = _inteiro(j.get("inicio", 7), "início do Jira")
    fim = _inteiro(j.get("fim", 20), "fim do Jira")
    if not (0 <= inicio <= 23 and 1 <= fim <= 24 and inicio < fim):
        raise ScheduleError(
            f"horário do Jira inválido: das {inicio}h às {fim}h — o início tem de vir antes do fim"
        )
    dias = str(j.get("dias", "todos"))
    if dias not in DIAS_JIRA:
        raise ScheduleError(f"dias do Jira inválidos: {dias!r}")

    return Schedule(
        confluence=Confluence(
            ativo=_bool(c, "ativo", True),
            hora=hora,
            contar_anexos=_bool(c, "contar_anexos", False),
        ),
        jira=Jira(
            ativo=_bool(j, "ativo", True),
            intervalo_min=intervalo,
            inicio=inicio,
            fim=fim,
            dias=dias,
            reconcile_domingo=_bool(j, "reconcile_domingo", True),
        ),
    )


def _sync(repo: Path, *args: str) -> str:
    return f"env PYTHONPATH={repo} {repo}/.venv/bin/python -m indexer.sync {' '.join(args)}"


def deslocamento_do_servidor(fuso: str = FUSO_DA_TELA) -> int:
    """Quantas horas o relógio do servidor está À FRENTE do fuso da tela.

    UTC contra Brasília dá 3. O Brasil não tem horário de verão desde 2019,
    então o valor é estável; ainda assim é recalculado a cada gravação.
    """
    agora = datetime.now(timezone.utc)
    servidor = agora.astimezone().utcoffset()
    tela = agora.astimezone(ZoneInfo(fuso)).utcoffset()
    assert servidor is not None and tela is not None
    horas, resto = divmod(int((servidor - tela).total_seconds()), 3600)
    if resto:
        raise ScheduleError("fuso com fração de hora não é suportado pela agenda")
    return horas


def _faixas(valores: Iterable[int]) -> str:
    """{0,1,2,5} -> "0-2,5". É assim que o cron lê lista de horas e de dias."""
    ordenados = sorted(set(valores))
    partes: list[str] = []
    i = 0
    while i < len(ordenados):
        j = i
        while j + 1 < len(ordenados) and ordenados[j + 1] == ordenados[j] + 1:
            j += 1
        a, b = ordenados[i], ordenados[j]
        partes.append(f"{a}" if a == b else f"{a}-{b}")
        i = j + 1
    return ",".join(partes)


def _no_servidor(horas: Iterable[int], dias: set[int] | None,
                 desloc: int) -> list[tuple[str, str]]:
    """Horas e dias da semana da tela -> campos (hora, dia) do cron do servidor.

    Uma hora que atravessa a meia-noite ao converter muda também de DIA: 22h de
    domingo em Brasília é 1h de segunda em UTC. Por isso o resultado pode ter
    mais de uma linha, uma por deslocamento de dia.
    """
    grupos: dict[int, set[int]] = {}
    for h in horas:
        grupos.setdefault((h + desloc) // 24, set()).add((h + desloc) % 24)
    if dias is None:
        return [(_faixas(set().union(*grupos.values())), "*")]
    return [
        (_faixas(hs), _faixas((d + dia) % 7 for d in dias))
        for dia, hs in sorted(grupos.items())
    ]


def _linhas_cron(minuto: str, horas: Iterable[int], dias: set[int] | None,
                 desloc: int, resto: str, prefixo: str = "") -> list[str]:
    return [f"{prefixo}{minuto} {h} * * {d} {resto}" for h, d in _no_servidor(horas, dias, desloc)]


def render_block(agenda: Schedule, repo: Path, desloc: int | None = None) -> str:
    """Monta o bloco do crontab. Sempre com flock: uma rodada por vez.

    As duas fontes dividem o MESMO lock, porque escrevem no mesmo SQLite e no
    mesmo Qdrant. O que muda é como cada uma reage ao lock ocupado:

    - Jira usa `-n`, desiste na hora. Pular não perde nada: a próxima rodada
      busca tudo desde o cursor, inclusive o que a pulada buscaria.
    - Confluence usa `-w`, espera um pouco. Se ele desistisse porque uma rodada
      do Jira estava no meio à meia-noite, a noite inteira seria perdida.

    Os horários da agenda estão no fuso da tela; `desloc` é quanto o servidor
    está à frente dele (None: calcula agora).
    """
    if desloc is None:
        desloc = deslocamento_do_servidor()
    c, j = agenda.confluence, agenda.jira
    desligado = "# (desativado no painel) "
    frente = f" (o relógio do servidor está {desloc:+d} h)" if desloc else ""
    linhas = [BEGIN, f"# horários em Brasília{frente}"]

    hh, mm = (int(x) for x in c.hora.split(":"))
    espera = f"flock -w {ESPERA_CONFLUENCE_S} {LOCK}"
    run_c = _sync(repo, "run", "--only", "confluence")
    if not c.contar_anexos:
        run_c += " --no-count-attachments"
    rec = _sync(repo, "reconcile", "--only", "jira")
    reconcilia = j.ativo and j.reconcile_domingo

    pc = "" if c.ativo else desligado
    linhas.append(f"# Confluence: uma vez por dia às {c.hora}" + ("" if c.ativo else " — desativado"))
    if c.ativo and reconcilia:
        # A varredura de deleções do Jira vai ENCADEADA depois da rodada de
        # domingo, dentro do mesmo lock. Agendada à parte no mesmo minuto, ela
        # disputaria o lock com uma rodada de horas e desistiria. É `;` e não
        # `&&`: as fontes são independentes, falha numa não cancela a outra.
        linhas += _linhas_cron(str(mm), [hh], {1, 2, 3, 4, 5, 6}, desloc,
                               f"cd {repo} && {espera} {run_c} >> {LOG_CONFLUENCE} 2>&1")
        linhas += _linhas_cron(str(mm), [hh], {0}, desloc,
                               f"cd {repo} && {espera} bash -c '{run_c}; {rec}' "
                               f">> {LOG_CONFLUENCE} 2>&1")
    else:
        linhas += _linhas_cron(str(mm), [hh], None, desloc,
                               f"cd {repo} && {espera} {run_c} >> {LOG_CONFLUENCE} 2>&1", pc)

    pj = "" if j.ativo else desligado
    rotulo_dias = "seg a sex" if j.dias == "uteis" else "todos os dias"
    linhas.append(f"# Jira: a cada {j.intervalo_min} min, das {j.inicio:02d}h às {j.fim:02d}h, {rotulo_dias}"
                  + ("" if j.ativo else " — desativado"))
    minuto = f"*/{j.intervalo_min}" if j.intervalo_min < 60 else "0"
    passo = 2 if j.intervalo_min == 120 else 1
    dias = {1, 2, 3, 4, 5} if j.dias == "uteis" else None
    linhas += _linhas_cron(minuto, range(j.inicio, j.fim, passo), dias, desloc,
                           f"cd {repo} && flock -n {LOCK} "
                           f"{_sync(repo, 'run', '--only', 'jira')} >> {LOG_JIRA} 2>&1", pj)
    if reconcilia and not c.ativo:
        # Sem a rodada do Confluence para pegar carona, a reconciliação roda
        # sozinha no mesmo horário de domingo.
        linhas.append(f"# Jira: deleções reconciliadas no domingo às {c.hora}")
        linhas += _linhas_cron(str(mm), [hh], {0}, desloc,
                               f"cd {repo} && {espera} {rec} >> {LOG_JIRA} 2>&1")

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
    "BEGIN", "END", "SOURCES", "Confluence", "Jira", "Schedule", "ScheduleError",
    "block_of", "deslocamento_do_servidor", "load", "merge", "parse", "read_crontab", "render_block",
    "save", "write_crontab",
]
