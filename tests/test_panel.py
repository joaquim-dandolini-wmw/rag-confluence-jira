"""Testes do painel: a agenda que reescreve crontab e a leitura de log.

A parte perigosa do painel é o crontab. Nenhum teste aqui toca o crontab de
verdade: `render_block` e `merge` são funções puras, e o teste do endpoint
substitui o escritor por um espião.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panel import logs as panel_logs
from panel import schedule as sched

REPO = Path("/srv/rag")


# --------------------------------------------------------------------------
# validação do que vem da tela
# --------------------------------------------------------------------------

def test_hora_invalida_e_recusada() -> None:
    for ruim in ("24:00", "9:5", "meia-noite", "00:60", ""):
        with pytest.raises(sched.ScheduleError):
            sched.parse({"hora": ruim})


def test_sem_fonte_nenhuma_e_recusado() -> None:
    with pytest.raises(sched.ScheduleError):
        sched.parse({"hora": "00:00", "fontes": []})


def test_fonte_desconhecida_e_ignorada_sem_derrubar() -> None:
    agenda = sched.parse({"hora": "00:00", "fontes": ["jira", "sharepoint"]})
    assert agenda.fontes == ("jira",)


def test_ordem_das_fontes_e_canonica() -> None:
    """A ordem não vem da tela: confluence primeiro, como o extract faz."""
    agenda = sched.parse({"hora": "00:00", "fontes": ["jira", "confluence"]})
    assert agenda.fontes == ("confluence", "jira")


# --------------------------------------------------------------------------
# o bloco do crontab
# --------------------------------------------------------------------------

def test_bloco_da_meia_noite_com_as_duas_fontes() -> None:
    bloco = sched.render_block(sched.parse({"hora": "00:00"}), REPO)
    linhas = [l for l in bloco.splitlines() if l and not l.startswith("#")]
    assert len(linhas) == 2, "segunda a sábado + domingo"
    assert linhas[0].startswith("0 0 * * 1-6")
    assert linhas[1].startswith("0 0 * * 0")
    # o run não leva --only: uma rodada só cobre as duas fontes
    assert "run --only" not in bloco
    assert "sync run --no-count-attachments" in linhas[0]
    assert "flock -n /tmp/rag-sync.lock" in linhas[0]
    assert "reconcile --only jira" in linhas[1]
    # o reconcile é ENCADEADO, não agendado à parte
    assert "&&" in linhas[1]


def test_hora_sem_zero_a_esquerda_no_cron() -> None:
    bloco = sched.render_block(sched.parse({"hora": "03:05"}), REPO)
    assert "5 3 * * " in bloco


def test_uma_fonte_so_usa_only() -> None:
    bloco = sched.render_block(sched.parse({"hora": "00:00", "fontes": ["confluence"]}), REPO)
    assert "--only confluence" in bloco
    # sem Jira no escopo não existe reconcile de Jira para encadear
    assert "reconcile" not in bloco
    assert len([l for l in bloco.splitlines() if l and not l.startswith("#")]) == 1


def test_contar_anexos_desligado_e_o_padrao() -> None:
    assert "--no-count-attachments" in sched.render_block(sched.parse({"hora": "00:00"}), REPO)
    ligado = sched.render_block(sched.parse({"hora": "00:00", "contar_anexos": True}), REPO)
    assert "--no-count-attachments" not in ligado


def test_desativado_comenta_as_linhas_mas_mantem_o_bloco() -> None:
    bloco = sched.render_block(sched.parse({"hora": "00:00", "ativo": False}), REPO)
    for linha in bloco.splitlines():
        assert linha.startswith("#"), linha


# --------------------------------------------------------------------------
# costura no crontab existente — onde um erro apaga o trabalho de outra pessoa
# --------------------------------------------------------------------------

def test_merge_preserva_jobs_que_nao_sao_nossos() -> None:
    atual = "SHELL=/bin/bash\nMAILTO=\"\"\n*/5 * * * * backup-do-financeiro\n"
    novo = sched.merge(atual, sched.render_block(sched.parse({"hora": "00:00"}), REPO))
    assert "backup-do-financeiro" in novo
    assert 'MAILTO=""' in novo
    assert novo.count(sched.BEGIN) == 1


def test_merge_substitui_o_bloco_anterior_sem_duplicar() -> None:
    atual = sched.merge("* * * * * outro\n", sched.render_block(sched.parse({"hora": "00:00"}), REPO))
    novo = sched.merge(atual, sched.render_block(sched.parse({"hora": "23:30"}), REPO))
    assert novo.count(sched.BEGIN) == 1
    assert novo.count(sched.END) == 1
    assert "30 23 * * " in novo
    assert "0 0 * * " not in novo
    assert "outro" in novo


def test_merge_e_idempotente() -> None:
    bloco = sched.render_block(sched.parse({"hora": "00:00"}), REPO)
    uma = sched.merge("* * * * * outro\n", bloco)
    duas = sched.merge(uma, bloco)
    assert uma == duas


def test_merge_em_crontab_vazio() -> None:
    bloco = sched.render_block(sched.parse({"hora": "00:00"}), REPO)
    novo = sched.merge("", bloco)
    assert novo.startswith(sched.BEGIN)
    assert novo.endswith("\n")


def test_block_of_extrai_so_o_nosso_pedaco() -> None:
    bloco = sched.render_block(sched.parse({"hora": "00:00"}), REPO)
    crontab = sched.merge("* * * * * outro\n", bloco)
    assert sched.block_of(crontab) == bloco
    assert sched.block_of("* * * * * outro\n") == ""


def test_ida_e_volta_do_arquivo_de_agenda(tmp_path) -> None:
    caminho = tmp_path / "agenda.json"
    agenda = sched.parse({"hora": "01:15", "fontes": ["jira"], "ativo": False})
    sched.save(caminho, agenda)
    assert sched.load(caminho) == agenda
    # arquivo corrompido não derruba o painel: cai no padrão
    caminho.write_text("{isto não é json")
    assert sched.load(caminho) == sched.Schedule()
    assert sched.load(tmp_path / "nao-existe.json") == sched.Schedule()


# --------------------------------------------------------------------------
# leitura de log
# --------------------------------------------------------------------------

def test_linha_json_vira_evento_com_extra() -> None:
    linha = json.dumps({
        "ts": "2026-09-10T09:23:11-0300", "level": "INFO",
        "logger": "indexer.sync", "msg": "espaço concluído",
        "space_key": "qualidade", "falhas": 0,
    })
    (evento,) = panel_logs.parse([linha])
    assert evento["level"] == "INFO"
    assert evento["msg"] == "espaço concluído"
    assert evento["extra"] == {"space_key": "qualidade", "falhas": 0}


def test_traceback_sobrevive_como_raw() -> None:
    """O log NÃO é só JSON: a perda de permissão do Confluence chegou assim."""
    linhas = [
        "Traceback (most recent call last):",
        '  File "indexer/sync.py", line 132, in resolve_confluence_scope',
        "xmlrpc.client.Fault: <Fault 0: 'NotPermittedException'>",
    ]
    eventos = panel_logs.parse(linhas)
    assert [e["level"] for e in eventos] == ["RAW"] * 3
    assert "NotPermitted" in eventos[-1]["msg"]


def test_barra_de_progresso_nao_e_evento() -> None:
    linhas = [
        "Loading weights:  75%|███████▍  | 293/391 [00:00<00:00, 19581.30it/s]",
        "Batches: 100%|██████████| 4/4 [00:01<00:00,  2.71it/s]",
        '{"ts": "x", "level": "INFO", "msg": "embed concluído"}',
    ]
    eventos = panel_logs.parse(linhas)
    assert len(eventos) == 1
    assert eventos[0]["msg"] == "embed concluído"


def test_json_que_nao_e_objeto_nao_explode() -> None:
    (evento,) = panel_logs.parse(["[1, 2, 3]"])
    assert evento["level"] == "RAW"


def test_resumos_mantem_falha_e_descarta_ruido() -> None:
    eventos = panel_logs.parse([
        json.dumps({"level": "INFO", "msg": "progresso do espaço", "pct": 12}),
        json.dumps({"level": "INFO", "msg": "extração do Confluence concluída"}),
        json.dumps({"level": "WARNING", "msg": "conteúdo não pôde ser buscado, seguindo"}),
        json.dumps({"level": "ERROR", "msg": "execução abortada"}),
    ])
    ficaram = [e["msg"] for e in panel_logs.only_summaries(eventos)]
    assert "progresso do espaço" not in ficaram
    assert len(ficaram) == 3


def test_tail_le_so_o_fim(tmp_path) -> None:
    arquivo = tmp_path / "x.log"
    arquivo.write_text("\n".join(f"linha {i}" for i in range(5000)) + "\n")
    linhas = panel_logs.tail(arquivo, 10)
    assert len(linhas) == 10
    assert linhas[-1] == "linha 4999"


def test_lista_de_arquivos_ignora_o_que_nao_e_log(tmp_path) -> None:
    (tmp_path / "a.log").write_text("x")
    (tmp_path / "b.txt").write_text("x")
    nomes = [a["nome"] for a in panel_logs.list_files(tmp_path)]
    assert nomes == ["a.log"]
    assert panel_logs.list_files(tmp_path / "nao-existe") == []


# --------------------------------------------------------------------------
# o endpoint que aplica — com o crontab espionado, nunca escrito
# --------------------------------------------------------------------------

@pytest.fixture
def cliente(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from panel import server

    escritos: list[str] = []
    monkeypatch.setattr(server.sched, "read_crontab", lambda: "* * * * * outro\n")
    monkeypatch.setattr(server.sched, "write_crontab", lambda c: escritos.append(c))
    monkeypatch.setattr(server, "AGENDA_PATH", tmp_path / "agenda.json")
    return TestClient(server.app), escritos, tmp_path / "agenda.json"


def test_post_agenda_aplica_e_grava_o_arquivo(cliente) -> None:
    client, escritos, caminho = cliente
    r = client.post("/api/agenda", json={"hora": "00:00", "fontes": ["confluence", "jira"]})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(escritos) == 1
    assert "outro" in escritos[0], "não pode apagar job de terceiro"
    assert "0 0 * * 1-6" in escritos[0]
    assert json.loads(caminho.read_text())["hora"] == "00:00"


def test_post_agenda_invalida_nao_escreve_nada(cliente) -> None:
    client, escritos, caminho = cliente
    r = client.post("/api/agenda", json={"hora": "99:99"})
    assert r.status_code == 400
    assert "hora inválida" in r.json()["erro"]
    assert escritos == []
    assert not caminho.exists()


def test_get_agenda_mostra_o_que_esta_no_ar(cliente) -> None:
    client, _, _ = cliente
    d = client.get("/api/agenda").json()
    assert d["crontab_tem_bloco"] is False
    assert d["previsto"].startswith(sched.BEGIN)


def test_pagina_inicial_serve_a_tela(cliente) -> None:
    client, _, _ = cliente
    r = client.get("/")
    assert r.status_code == 200
    assert "rag · painel" in r.text
