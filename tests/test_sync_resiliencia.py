"""Um espaço que falha não pode custar a noite inteira.

Este arquivo existe por causa de um incidente real: entre 30/09 e 09/10/2026 o
Confluence passou a devolver 502 na janela da madrugada, e **uma** chamada
`getPages` com erro derrubava a extração inteira — as outras centenas de
espaços nem eram tentadas. Página individual já era tolerada; espaço não era.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from connectors.confluence_legacy import SpaceExtraction
from indexer import sync as sync_mod
from store.documents import SOURCE_CONFLUENCE, Document, DocumentStore


@dataclass(frozen=True)
class ConfFalsa:
    url: str = "https://confluence.invalido"
    spaces: tuple[str, ...] = ()
    discover: bool = True


class CfgFalso:
    def __init__(self) -> None:
        self.confluence = ConfFalsa()

    def require_confluence(self) -> ConfFalsa:
        return self.confluence


class ClienteFalso:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _documento(space_key: str, n: int = 1) -> Document:
    return Document(
        doc_id=f"confluence:page:{space_key}-{n}",
        source=SOURCE_CONFLUENCE,
        title=f"Página {n} de {space_key}",
        body_text="conteúdo",
        url=f"https://confluence.invalido/{space_key}/{n}",
        space_key=space_key,
    )


def _montar(monkeypatch, escopo, quebra):
    """Troca cliente, extrator e resolução de escopo por dublês.

    `quebra` é o conjunto de espaços cujo iter_space levanta — é o 502 do
    incidente, sem precisar de rede.
    """
    class ExtratorFalso:
        def __init__(self, *a, **k): pass

        def iter_space(self, space_key, known_versions, result: SpaceExtraction, **k):
            if space_key in quebra:
                raise RuntimeError(
                    "chamada getPages falhou: <ProtocolError ... 502 Bad Gateway>"
                )
            result.listing_complete = True
            doc = _documento(space_key)
            result.seen_doc_ids.add(doc.doc_id)
            result.pages_listed = 1
            result.fetched = 1
            yield doc, 1

    monkeypatch.setattr(sync_mod, "ConfluenceClient", lambda conf: ClienteFalso())
    monkeypatch.setattr(sync_mod, "ConfluenceExtractor", ExtratorFalso)
    monkeypatch.setattr(
        sync_mod, "resolve_confluence_scope",
        lambda conf, client, store: sync_mod.ScopeDiff(
            scope=tuple(escopo), entered=(), left=(), discovered=True),
    )


@pytest.fixture()
def store(tmp_path) -> DocumentStore:
    with DocumentStore(tmp_path / "docs.sqlite3") as s:
        yield s


def _rodar(store):
    return sync_mod.extract_confluence(
        CfgFalso(), store, full=False, count_attachments=False, fetch_labels=False
    )


def test_espaco_que_falha_nao_derruba_os_outros(monkeypatch, store) -> None:
    _montar(monkeypatch, ["a", "ruim", "b", "c"], {"ruim"})

    totais = _rodar(store)

    assert totais["espacos_com_falha"] == 1
    assert totais["gravados"] == 3, "os três espaços bons tinham que ter sido gravados"
    ids = store.list_doc_ids(source=SOURCE_CONFLUENCE)
    assert {"confluence:page:a-1", "confluence:page:b-1", "confluence:page:c-1"} <= ids


def test_espaco_que_falha_nao_apaga_o_que_ja_estava_indexado(monkeypatch, store) -> None:
    """Listagem parcial não pode virar deleção: é assim que se perde conteúdo."""
    antigo = _documento("ruim", 99)
    store.upsert(antigo)
    store.commit()

    _montar(monkeypatch, ["ruim"], {"ruim"})
    totais = _rodar(store)

    assert totais["removidos"] == 0
    assert antigo.doc_id in store.list_doc_ids(source=SOURCE_CONFLUENCE)


def test_falhas_em_sequencia_interrompem_a_rodada(monkeypatch, store) -> None:
    """Instância fora do ar: insistir em centenas de espaços só gera carga."""
    limite = sync_mod.MAX_FALHAS_SEGUIDAS
    escopo = [f"e{i}" for i in range(limite + 25)]
    _montar(monkeypatch, escopo, set(escopo))

    totais = _rodar(store)

    assert totais.get("abortado") == "falhas seguidas demais"
    assert totais["espacos"] == limite, "devia ter parado no limite, não varrido tudo"


def test_falha_isolada_nao_interrompe_a_rodada(monkeypatch, store) -> None:
    """O contador de sequência zera quando um espaço dá certo."""
    limite = sync_mod.MAX_FALHAS_SEGUIDAS
    escopo = []
    quebra = set()
    for i in range(limite + 5):
        escopo += [f"ruim{i}", f"bom{i}"]
        quebra.add(f"ruim{i}")
    _montar(monkeypatch, escopo, quebra)

    totais = _rodar(store)

    assert "abortado" not in totais
    assert totais["espacos"] == len(escopo)
    assert totais["gravados"] == limite + 5
