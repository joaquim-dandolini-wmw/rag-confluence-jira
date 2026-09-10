"""Escopo do Confluence: descoberta por grupo, diff e limpeza do que saiu.

Nenhum teste toca a instância real. O cliente é um dublê que devolve a lista
de espaços que o usuário de serviço "enxergaria".
"""

from __future__ import annotations

import pytest

from config import ConfluenceConfig
from connectors.confluence_legacy import ConfluenceAuthError
from indexer.sync import purge_spaces, resolve_confluence_scope
from store.documents import Document, DocumentStore


class FakeConfluence:
    """Dublê: getSpaces() devolve o que o usuário autenticado enxerga."""

    def __init__(self, chaves: list[str]) -> None:
        self._chaves = chaves
        self.chamadas = 0

    def get_spaces(self) -> list[dict[str, str]]:
        self.chamadas += 1
        return [{"key": k, "name": k} for k in self._chaves]


def conf(**over) -> ConfluenceConfig:
    base = {
        "url": "https://confluence.exemplo",
        "user": "wmw-rag",
        "password": "x",
        "spaces": (),
        "discover": True,
        "exclude_spaces": (),
    }
    base.update(over)
    return ConfluenceConfig(**base)  # type: ignore[arg-type]


def pagina(space_key: str, n: int = 1) -> Document:
    return Document(
        doc_id=f"confluence:page:{space_key}-{n}",
        source="confluence",
        title=f"Página {n} de {space_key}",
        body_text="conteúdo",
        url=f"https://confluence.exemplo/{space_key}/{n}",
        space_key=space_key,
    )


def store_com(tmp_path, espacos: dict[str, int]) -> DocumentStore:
    s = DocumentStore(tmp_path / "s.sqlite3")
    for key, quantas in espacos.items():
        for i in range(quantas):
            s.upsert(pagina(key, i))
    s.commit()
    return s


# -- descoberta -------------------------------------------------------------

def test_escopo_vem_do_que_o_usuario_enxerga(tmp_path):
    """É assim que "por grupo" funciona: o Confluence já aplicou as permissões.

    Não existe API em 4.2.4 para perguntar quais espaços um GRUPO enxerga
    (getSpacePermissionSet não existe nesta versão), mas getSpaces() responde
    exatamente isso para o usuário autenticado.
    """
    with store_com(tmp_path, {}) as store:
        diff = resolve_confluence_scope(conf(), FakeConfluence(["a", "b"]), store)
    assert diff.scope == ("a", "b")
    assert diff.discovered is True


def test_lista_fixa_nao_consulta_o_servidor(tmp_path):
    cliente = FakeConfluence(["a", "b", "c"])
    with store_com(tmp_path, {}) as store:
        diff = resolve_confluence_scope(
            conf(discover=False, spaces=("a",)), cliente, store
        )
    assert diff.scope == ("a",)
    assert cliente.chamadas == 0


def test_exclusao_manual_tira_do_escopo(tmp_path):
    with store_com(tmp_path, {}) as store:
        diff = resolve_confluence_scope(
            conf(exclude_spaces=("rh",)), FakeConfluence(["a", "rh"]), store
        )
    assert diff.scope == ("a",)


def test_lista_vazia_aborta_em_vez_de_apagar_tudo(tmp_path):
    """Falha do XML-RPC não pode ser lida como "escopo vazio".

    Sem esta trava, uma queda de conexão apagaria o índice inteiro.
    """
    with store_com(tmp_path, {"manuais": 3}) as store:
        with pytest.raises(ConfluenceAuthError, match="zero espaços"):
            resolve_confluence_scope(conf(), FakeConfluence([]), store)
        assert store.confluence_space_keys() == {"manuais"}


# -- diff -------------------------------------------------------------------

def test_diff_separa_o_que_entrou_e_o_que_saiu(tmp_path):
    with store_com(tmp_path, {"manuais": 2, "antigo": 1}) as store:
        diff = resolve_confluence_scope(
            conf(), FakeConfluence(["manuais", "novo"]), store
        )
    assert diff.entered == ("novo",)
    assert diff.left == ("antigo",)
    assert diff.scope == ("manuais", "novo")


def test_nada_muda_quando_o_escopo_e_o_mesmo(tmp_path):
    with store_com(tmp_path, {"a": 1, "b": 1}) as store:
        diff = resolve_confluence_scope(conf(), FakeConfluence(["a", "b"]), store)
    assert diff.entered == () and diff.left == ()


def test_espacos_do_store_sao_listados(tmp_path):
    with store_com(tmp_path, {"a": 2, "b": 1}) as store:
        assert store.confluence_space_keys() == {"a", "b"}


# -- limpeza ----------------------------------------------------------------

def test_purga_remove_documentos_do_espaco_que_saiu(tmp_path):
    with store_com(tmp_path, {"fica": 2, "sai": 3}) as store:
        resultado = purge_spaces(store, ["sai"])
        assert resultado == {"espacos": 1, "documentos": 3}
        assert store.confluence_space_keys() == {"fica"}
        assert store.stats().documents == 2


def test_purga_agenda_a_remocao_no_qdrant(tmp_path):
    """Tirar do store não basta: o ponto continuaria pesquisável no índice."""
    with store_com(tmp_path, {"sai": 2}) as store:
        purge_spaces(store, ["sai"])
        na_fila = store.take_index_deletions()
        assert len(na_fila) == 2


def test_purga_limpa_a_versao_para_reextrair_se_voltar(tmp_path):
    """Espaço que volta ao escopo tem que ser reextraído do zero."""
    caminho = tmp_path / "s.sqlite3"
    with DocumentStore(caminho) as store:
        doc = pagina("sai", 1)
        store.upsert(doc)
        store.set_confluence_version(doc.doc_id, 7)
        store.commit()
        assert store.confluence_versions()

        purge_spaces(store, ["sai"])
        assert store.confluence_versions() == {}


def test_purga_de_espaco_sem_conteudo_nao_faz_nada(tmp_path):
    with store_com(tmp_path, {"a": 1}) as store:
        assert purge_spaces(store, ["nunca-existiu"]) == {
            "espacos": 1, "documentos": 0
        }
        assert store.stats().documents == 1


# -- configuração -----------------------------------------------------------

def test_auto_liga_a_descoberta(monkeypatch):
    from config import load_config

    monkeypatch.setenv("CONFLUENCE_URL", "https://c.exemplo")
    monkeypatch.setenv("CONFLUENCE_USER", "wmw-rag")
    monkeypatch.setenv("CONFLUENCE_PASSWORD", "x")
    monkeypatch.setenv("CONFLUENCE_SPACES", "auto")
    cfg = load_config(dotenv=False)
    assert cfg.confluence is not None
    assert cfg.confluence.discover is True
    assert cfg.confluence.spaces == ()


def test_escopo_vazio_continua_abortando(monkeypatch):
    """A trava da Fase 1 não pode ter sido afrouxada pelo modo auto."""
    from config import load_config

    monkeypatch.setenv("CONFLUENCE_URL", "https://c.exemplo")
    monkeypatch.setenv("CONFLUENCE_USER", "u")
    monkeypatch.setenv("CONFLUENCE_PASSWORD", "x")
    monkeypatch.setenv("CONFLUENCE_SPACES", "")
    cfg = load_config(dotenv=False)
    assert any("CONFLUENCE_SPACES" in e for e in cfg._errors)
    assert cfg.confluence is None


# -- concorrência entre o cron do Jira e a extração do Confluence ------------

def test_dois_escritores_nao_derrubam_a_rodada(tmp_path):
    """Aconteceu em produção: "database is locked" matou a extração.

    O cron do Jira roda a cada 15 minutos e abre o mesmo SQLite que uma
    extração longa do Confluence está usando. Com WAL só há um escritor por
    vez; sem busy_timeout o segundo falha na hora em vez de esperar.
    """
    caminho = tmp_path / "s.sqlite3"
    with DocumentStore(caminho) as a, DocumentStore(caminho) as b:
        a.upsert(pagina("espaco-a", 1))
        a.commit()
        # b escreve enquanto a conexão de a segue aberta
        b.upsert(pagina("espaco-b", 1))
        b.commit()
        assert b.confluence_space_keys() == {"espaco-a", "espaco-b"}


def test_busy_timeout_esta_configurado(tmp_path):
    from store.documents import BUSY_TIMEOUT_MS

    with DocumentStore(tmp_path / "s.sqlite3") as store:
        (valor,) = store._conn.execute("PRAGMA busy_timeout").fetchone()
    assert valor == BUSY_TIMEOUT_MS
