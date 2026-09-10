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


# --------------------------------------------------------------------------
# expor na rede: a trava e a credencial
# --------------------------------------------------------------------------

def test_loopback_dispensa_senha() -> None:
    from panel.server import exposure_error, is_loopback

    for host in ("127.0.0.1", "localhost", "::1", ""):
        assert is_loopback(host)
        assert exposure_error(host, "", False) is None


def test_rede_sem_senha_nao_sobe() -> None:
    """Configuração que achata controle de acesso aborta, como no resto do projeto."""
    from panel.server import exposure_error

    problema = exposure_error("0.0.0.0", "", False)
    assert problema is not None
    assert "PANEL_PASSWORD" in problema
    assert "crontab" in problema


def test_rede_com_senha_sobe() -> None:
    from panel.server import exposure_error

    assert exposure_error("0.0.0.0", "uma-senha", False) is None
    assert exposure_error("10.2.1.132", "uma-senha", False) is None


def test_expor_sem_senha_exige_assumir_o_risco() -> None:
    from panel.server import exposure_error

    assert exposure_error("0.0.0.0", "", True) is None


def _basic(user: str, senha: str) -> dict[str, str]:
    import base64

    token = base64.b64encode(f"{user}:{senha}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_credencial_certa_e_errada() -> None:
    from panel.server import credentials_ok

    assert credentials_ok(_basic("admin", "s3nha")["Authorization"], "admin", "s3nha")
    assert not credentials_ok(_basic("admin", "outra")["Authorization"], "admin", "s3nha")
    assert not credentials_ok(_basic("outro", "s3nha")["Authorization"], "admin", "s3nha")
    assert not credentials_ok(None, "admin", "s3nha")
    assert not credentials_ok("Bearer abc", "admin", "s3nha")
    # base64 quebrado não pode virar exceção, só recusa
    assert not credentials_ok("Basic ###", "admin", "s3nha")
    # sem senha configurada o painel é aberto (só acontece em loopback)
    assert credentials_ok(None, "admin", "")


@pytest.fixture
def cliente_com_senha(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from panel import server

    monkeypatch.setattr(server.sched, "read_crontab", lambda: "")
    monkeypatch.setattr(server.sched, "write_crontab", lambda c: None)
    monkeypatch.setattr(server, "AGENDA_PATH", tmp_path / "agenda.json")
    return TestClient(server.build_app("admin", "s3nha"))


def test_sem_credencial_devolve_401_com_desafio(cliente_com_senha) -> None:
    r = cliente_com_senha.get("/api/estado")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic realm=")


def test_com_credencial_passa(cliente_com_senha) -> None:
    r = cliente_com_senha.get("/api/agenda", headers=_basic("admin", "s3nha"))
    assert r.status_code == 200


def test_a_tela_tambem_e_protegida(cliente_com_senha) -> None:
    assert cliente_com_senha.get("/").status_code == 401
    assert cliente_com_senha.get("/", headers=_basic("admin", "s3nha")).status_code == 200


def test_post_forjado_de_formulario_e_recusado(cliente_com_senha) -> None:
    """Sem CORS, application/json de outro site não passa; formulário passaria."""
    r = cliente_com_senha.post(
        "/api/agenda",
        data="hora=03:00",
        headers={"Content-Type": "application/x-www-form-urlencoded", **_basic("admin", "s3nha")},
    )
    assert r.status_code == 415


def test_post_json_com_credencial_aplica(cliente_com_senha) -> None:
    r = cliente_com_senha.post(
        "/api/agenda", json={"hora": "00:00"}, headers=_basic("admin", "s3nha")
    )
    assert r.status_code == 200 and r.json()["ok"] is True


def test_ruido_do_rocm_nao_e_evento() -> None:
    """SETUP.md §12: o wheel do ROCm emite isto no import e não é defeito."""
    eventos = panel_logs.parse([
        "(null): No such file or directory",
        "  (null): No such file or directory  ",
        '{"ts": "x", "level": "INFO", "msg": "GPU escolhida para o vetor denso"}',
    ])
    assert len(eventos) == 1
    assert eventos[0]["msg"].startswith("GPU escolhida")


def test_outro_no_such_file_continua_aparecendo() -> None:
    """O filtro é casamento exato: erro de arquivo de verdade não pode sumir."""
    eventos = panel_logs.parse([
        "FileNotFoundError: [Errno 2] No such file or directory: 'data/documents.sqlite3'",
    ])
    assert len(eventos) == 1
    assert eventos[0]["level"] == "RAW"


# --------------------------------------------------------------------------
# a tela de mudanças
# --------------------------------------------------------------------------

@pytest.fixture
def store_com_mudancas(tmp_path):
    from datetime import datetime, timedelta, timezone

    from store.documents import Document, DocumentStore

    caminho = tmp_path / "s.sqlite3"
    with DocumentStore(caminho) as store:
        for i in range(3):
            store.upsert(Document(
                doc_id=f"confluence:page:{i}", source="confluence", title=f"Página {i}",
                body_text=f"corpo {i}", url=f"https://c/{i}", space_key="qualidade",
            ))
        store.upsert(Document(
            doc_id="jira:VENDAS-1", source="jira", content_type="issue",
            title="VENDAS-1: uma issue", body_text="descrição", url="https://j/1",
            project="VENDAS", status="Aberto",
        ))
        store.commit()
        # envelhece um documento para cair fora da janela de 24 h
        antigo = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
        store._conn.execute(
            "UPDATE documents SET extracted_at = ? WHERE doc_id = ?",
            (antigo, "confluence:page:0"),
        )
        store.commit()
    return caminho


def test_resumo_conta_por_janela_e_por_fonte(store_com_mudancas) -> None:
    from panel import changes

    with changes.connect(store_com_mudancas) as conn:
        r = changes.summary(conn)
    assert r["documentos"] == 4
    assert r["janelas"]["24h"]["total"] == 3          # o de 3 dias atrás ficou fora
    assert r["janelas"]["24h"]["confluence"] == 2
    assert r["janelas"]["24h"]["jira"] == 1
    assert r["janelas"]["7d"]["total"] == 4
    assert r["por_fonte"]["confluence"]["documentos"] == 3


def test_recent_ordena_do_mais_novo_e_filtra(store_com_mudancas) -> None:
    from panel import changes

    with changes.connect(store_com_mudancas) as conn:
        todos = changes.recent(conn, janela="7d")
        assert [i["doc_id"] for i in todos][-1] == "confluence:page:0", "o mais antigo por último"

        so_jira = changes.recent(conn, janela="7d", fonte="jira")
        assert len(so_jira) == 1
        assert so_jira[0]["onde"] == "VENDAS"
        assert so_jira[0]["status"] == "Aberto"

        # o filtro de texto cobre título, espaço, projeto e id
        assert len(changes.recent(conn, janela="7d", texto="qualidade")) == 3
        assert len(changes.recent(conn, janela="7d", texto="VENDAS")) == 1
        assert changes.recent(conn, janela="7d", texto="nao-existe") == []

        assert len(changes.recent(conn, janela="24h")) == 3
        assert len(changes.recent(conn, janela="7d", limite=2)) == 2


def test_pendencias_aparecem_como_pendencia(store_com_mudancas) -> None:
    from panel import changes

    with changes.connect(store_com_mudancas) as conn:
        item = changes.recent(conn, janela="7d")[0]
    # nada foi indexado nem embedado neste store de teste
    assert item["pendente_index"] is True
    assert item["pendente_denso"] is True


def test_por_dia_agrupa_e_ordena_crescente(store_com_mudancas) -> None:
    from panel import changes

    with changes.connect(store_com_mudancas) as conn:
        dias = changes.per_day(conn, 14)
    assert len(dias) == 2
    assert dias[0]["dia"] < dias[1]["dia"], "do mais antigo para o mais novo"
    assert sum(d["total"] for d in dias) == 4


def test_store_ausente_da_erro_claro(tmp_path) -> None:
    from panel import changes

    with pytest.raises(changes.StoreUnavailable):
        changes.connect(tmp_path / "nao-existe.sqlite3")


def _aponta_para(monkeypatch, store_path) -> None:
    """Faz o painel ler o store do teste. O Config é frozen, então é replace."""
    import dataclasses

    from panel import server

    falso = dataclasses.replace(server.cfg(), store_path=store_path)
    monkeypatch.setattr(server, "cfg", lambda: falso)


def test_endpoint_de_mudancas_responde(cliente, monkeypatch, store_com_mudancas) -> None:
    client, _, _ = cliente
    _aponta_para(monkeypatch, store_com_mudancas)
    d = client.get("/api/mudancas?janela=7d&limite=10").json()
    assert d["resumo"]["documentos"] == 4
    assert len(d["itens"]) == 4
    assert d["janela"] == "7d"
    assert d["remocoes_na_ultima_rodada"] == {}


def test_endpoint_recusa_janela_desconhecida(cliente, monkeypatch, store_com_mudancas) -> None:
    client, _, _ = cliente
    _aponta_para(monkeypatch, store_com_mudancas)
    assert client.get("/api/mudancas?janela=eternidade").json()["janela"] == "7d"


def test_endpoint_sem_store_devolve_erro_e_lista_vazia(cliente, monkeypatch, tmp_path) -> None:
    client, _, _ = cliente
    _aponta_para(monkeypatch, tmp_path / "nao-existe.sqlite3")
    d = client.get("/api/mudancas").json()
    assert d["itens"] == []
    assert "não encontrado" in d["erro"]
