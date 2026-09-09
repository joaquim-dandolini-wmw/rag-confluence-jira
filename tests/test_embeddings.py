"""Testes do vetor denso. Nenhum depende de GPU, de rede ou do Qdrant.

O encoder é substituído por um dublê que registra o que recebeu, porque o que
precisa ser garantido aqui não é a qualidade do modelo - é o contrato em volta
dele: prefixo certo em cada lado, normalização pedida, acento preservado e
chunk_id estável.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import (
    DENSE_VECTOR_NAME,
    DENSE_VECTOR_SIZE,
    SEARCH_MODES,
    EmbeddingConfig,
    load_config,
)
from indexer.chunking import chunk_document, chunk_id_for
from indexer.embeddings import (
    PASSAGE_PREFIX,
    QUERY_PREFIX,
    DenseEmbedder,
    EmbeddingError,
    iter_batches,
)
from indexer.index import IndexError_, KnowledgeIndex, fold_accents
from store.documents import Document, DocumentStore


class FakeEncoder:
    """Dublê do SentenceTransformer: registra as chamadas e devolve vetor L2."""

    def __init__(self, dim: int = DENSE_VECTOR_SIZE) -> None:
        self.dim = dim
        self.calls: list[dict[str, object]] = []

    def encode(
        self,
        sentences: list[str],
        *,
        batch_size: int,
        normalize_embeddings: bool,
        show_progress_bar: bool,
    ) -> list[list[float]]:
        self.calls.append(
            {
                "sentences": list(sentences),
                "batch_size": batch_size,
                "normalize_embeddings": normalize_embeddings,
                "show_progress_bar": show_progress_bar,
            }
        )
        # Vetor unitário no primeiro eixo: já normalizado, como o real devolve.
        return [[1.0] + [0.0] * (self.dim - 1) for _ in sentences]

    @property
    def sentences_seen(self) -> list[str]:
        seen: list[str] = []
        for call in self.calls:
            seen.extend(call["sentences"])  # type: ignore[arg-type]
        return seen


def make_config(**overrides: object) -> EmbeddingConfig:
    base = {
        "model_name": "intfloat/multilingual-e5-large",
        "cache_dir": Path("/tmp/nao-existe-de-proposito"),
        "device": "cpu",
        "batch_size": 16,
        "allow_download": False,
    }
    base.update(overrides)
    return EmbeddingConfig(**base)  # type: ignore[arg-type]


def make_embedder(encoder: FakeEncoder | None = None, **overrides: object):
    enc = encoder or FakeEncoder()
    return DenseEmbedder(make_config(**overrides), encoder=enc), enc


# --------------------------------------------------------------------------
# prefixos de instrução
# --------------------------------------------------------------------------

def test_passagem_recebe_prefixo_de_passagem():
    embedder, enc = make_embedder()
    embedder.embed_passages(["o certificado expirou"])
    assert enc.sentences_seen == [PASSAGE_PREFIX + "o certificado expirou"]


def test_consulta_recebe_prefixo_de_consulta():
    embedder, enc = make_embedder()
    embedder.embed_query("o certificado venceu")
    assert enc.sentences_seen == [QUERY_PREFIX + "o certificado venceu"]


def test_os_dois_prefixos_sao_diferentes():
    """O e5 é assimétrico: usar o mesmo prefixo dos dois lados perde qualidade."""
    assert QUERY_PREFIX != PASSAGE_PREFIX
    assert QUERY_PREFIX == "query: "
    assert PASSAGE_PREFIX == "passage: "


def test_prefixo_e_aplicado_a_todas_as_fatias_do_lote():
    embedder, enc = make_embedder()
    embedder.embed_passages(["um", "dois", "três"])
    assert enc.sentences_seen == [
        PASSAGE_PREFIX + "um",
        PASSAGE_PREFIX + "dois",
        PASSAGE_PREFIX + "três",
    ]


# --------------------------------------------------------------------------
# normalização L2
# --------------------------------------------------------------------------

def test_normalizacao_l2_e_sempre_pedida_ao_encoder():
    """A coleção usa distância de cosseno: sem normalizar, responde errado."""
    embedder, enc = make_embedder()
    embedder.embed_passages(["texto"])
    embedder.embed_query("consulta")
    assert all(call["normalize_embeddings"] is True for call in enc.calls)


def test_vetor_devolvido_tem_norma_unitaria():
    embedder, _ = make_embedder()
    vector = embedder.embed_query("consulta")
    norma = sum(value * value for value in vector) ** 0.5
    assert norma == pytest.approx(1.0, abs=1e-6)


def test_dimensao_do_vetor_bate_com_o_schema_da_colecao():
    embedder, _ = make_embedder()
    assert len(embedder.embed_query("x")) == DENSE_VECTOR_SIZE


def test_dimensao_errada_do_encoder_aborta():
    """Vetor de dimensão diferente seria recusado pelo Qdrant lá adiante."""
    embedder, _ = make_embedder(FakeEncoder(dim=768))
    with pytest.raises(EmbeddingError, match="768"):
        embedder.embed_passages(["texto"])


# --------------------------------------------------------------------------
# acento: o denso NÃO faz folding
# --------------------------------------------------------------------------

def test_denso_preserva_diacritico():
    """fold_accents() existe para o tokenizer do BM25 e só para ele.

    Para um transformer multilingual o acento carrega significado; remover
    piora o resultado.
    """
    texto = "não foi possível gerar a cobrança do pedido"
    embedder, enc = make_embedder()
    embedder.embed_passages([texto])
    visto = enc.sentences_seen[0]
    assert visto == PASSAGE_PREFIX + texto
    assert "ã" in visto and "í" in visto


def test_denso_nao_usa_a_mesma_normalizacao_do_bm25():
    texto = "configuração do proxy"
    embedder, enc = make_embedder()
    embedder.embed_passages([texto])
    assert enc.sentences_seen[0] != PASSAGE_PREFIX + fold_accents(texto)
    assert fold_accents(texto) == "configuracao do proxy"


def test_consulta_densa_tambem_preserva_acento():
    embedder, enc = make_embedder()
    embedder.embed_query("emissão de boleto")
    assert enc.sentences_seen[0] == QUERY_PREFIX + "emissão de boleto"


# --------------------------------------------------------------------------
# batch size
# --------------------------------------------------------------------------

def test_batch_size_do_config_e_repassado():
    embedder, enc = make_embedder(batch_size=7)
    embedder.embed_passages(["a", "b"])
    assert enc.calls[0]["batch_size"] == 7


def test_batch_size_pode_ser_sobreposto_na_chamada():
    embedder, enc = make_embedder(batch_size=7)
    embedder.embed_passages(["a"], batch_size=64)
    assert enc.calls[0]["batch_size"] == 64


def test_consulta_usa_lote_de_um():
    embedder, enc = make_embedder(batch_size=128)
    embedder.embed_query("uma consulta só")
    assert enc.calls[0]["batch_size"] == 1


def test_lista_vazia_nao_chama_o_encoder():
    embedder, enc = make_embedder()
    assert embedder.embed_passages([]) == []
    assert enc.calls == []


def test_iter_batches_particiona_sem_perder_item():
    assert list(iter_batches([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(iter_batches([], 3)) == []


# --------------------------------------------------------------------------
# determinismo dos IDs: é o que faz o update casar com o ponto existente
# --------------------------------------------------------------------------

def test_chunk_id_e_deterministico_entre_execucoes():
    assert chunk_id_for("jira:VENDAS-14993", 0) == chunk_id_for("jira:VENDAS-14993", 0)
    assert chunk_id_for("jira:VENDAS-14993", 0) != chunk_id_for("jira:VENDAS-14993", 1)


def test_refatiar_o_mesmo_documento_reproduz_os_mesmos_ids():
    """O `embed` refatia do store e faz UPDATE nos pontos que o `index` criou.

    Se os ids não casassem, o update não encontraria ponto nenhum e o vetor
    denso se perderia sem erro.
    """
    corpo = "\n\n".join(f"# Seção {i}\n\nconteúdo da seção {i}" for i in range(6))
    primeira = chunk_document("confluence:page:123", "Título", corpo)
    segunda = chunk_document("confluence:page:123", "Título", corpo)
    assert [c.chunk_id for c in primeira] == [c.chunk_id for c in segunda]
    assert len(primeira) >= 1


def test_chunk_id_muda_quando_o_documento_muda():
    a = chunk_document("confluence:page:123", "Título", "corpo original")
    b = chunk_document("confluence:page:124", "Título", "corpo original")
    assert a[0].chunk_id != b[0].chunk_id


# --------------------------------------------------------------------------
# operação offline: falha dizendo o que falta, nunca baixa em silêncio
# --------------------------------------------------------------------------

def test_cache_ausente_falha_com_instrucao_de_precache(tmp_path):
    from indexer.embeddings import _load_encoder

    cfg = make_config(cache_dir=tmp_path / "vazio", allow_download=False)
    with pytest.raises(EmbeddingError) as exc:
        _load_encoder(cfg)
    mensagem = str(exc.value)
    assert "models--intfloat--multilingual-e5-large" in mensagem
    assert "precache_models" in mensagem
    assert "ALLOW_MODEL_DOWNLOAD" in mensagem


def test_encoder_nao_e_carregado_na_construcao(tmp_path):
    """Construir o embedder não pode exigir modelo em disco nem GPU."""
    embedder = DenseEmbedder(make_config(cache_dir=tmp_path / "vazio"))
    assert embedder.device == "cpu"
    assert embedder.batch_size == 16
    with pytest.raises(EmbeddingError):
        embedder.embed_query("só agora tenta carregar")


# --------------------------------------------------------------------------
# configuração
# --------------------------------------------------------------------------

def test_device_invalido_e_recusado_pelo_config(monkeypatch):
    monkeypatch.setenv("EMBED_DEVICE", "rocm")
    monkeypatch.setenv("JIRA_URL", "https://jira.exemplo")
    monkeypatch.setenv("JIRA_PAT", "x")
    monkeypatch.setenv("JIRA_PROJECTS", "OPS")
    cfg = load_config(dotenv=False)
    assert any("EMBED_DEVICE" in erro for erro in cfg._errors)


def test_cuda_e_o_nome_do_device_tambem_no_rocm(monkeypatch):
    """O PyTorch com build ROCm expõe a GPU da AMD pela API "cuda"."""
    monkeypatch.setenv("EMBED_DEVICE", "cuda")
    monkeypatch.setenv("JIRA_URL", "https://jira.exemplo")
    monkeypatch.setenv("JIRA_PAT", "x")
    monkeypatch.setenv("JIRA_PROJECTS", "OPS")
    cfg = load_config(dotenv=False)
    assert cfg.embedding.device == "cuda"
    assert not any("EMBED_DEVICE" in erro for erro in cfg._errors)


def test_batch_size_nao_numerico_e_recusado(monkeypatch):
    monkeypatch.setenv("EMBED_BATCH_SIZE", "grande")
    monkeypatch.setenv("JIRA_URL", "https://jira.exemplo")
    monkeypatch.setenv("JIRA_PAT", "x")
    monkeypatch.setenv("JIRA_PROJECTS", "OPS")
    cfg = load_config(dotenv=False)
    assert any("EMBED_BATCH_SIZE" in erro for erro in cfg._errors)


# --------------------------------------------------------------------------
# modos de busca
# --------------------------------------------------------------------------

def test_modos_de_busca_declarados():
    assert SEARCH_MODES == ("bm25", "dense", "hybrid", "auto")


def test_modo_desconhecido_aborta_antes_de_qualquer_rede(tmp_path):
    index = KnowledgeIndex("http://127.0.0.1:1", "coletanea", tmp_path)
    with pytest.raises(IndexError_, match="desconhecido"):
        index.search("qualquer coisa", mode="semantico")


def test_consulta_vazia_nao_vai_ao_servidor(tmp_path):
    index = KnowledgeIndex("http://127.0.0.1:1", "coletanea", tmp_path)
    assert index.search("   ", mode="hybrid") == []


def test_denso_sem_configuracao_de_embedding_da_mensagem_util(tmp_path):
    index = KnowledgeIndex("http://127.0.0.1:1", "coletanea", tmp_path)
    with pytest.raises(IndexError_, match="load_config"):
        _ = index.embedder


def test_nome_do_vetor_denso_bate_com_o_schema():
    assert DENSE_VECTOR_NAME == "dense"


# --------------------------------------------------------------------------
# progresso incremental do embed no document store
# --------------------------------------------------------------------------

def documento(doc_id: str, corpo: str = "corpo") -> Document:
    return Document(
        doc_id=doc_id,
        source="jira",
        title="Título",
        body_text=corpo,
        url="https://jira.exemplo/browse/OPS-1",
    )


def test_documento_novo_entra_como_pendente_de_denso(tmp_path):
    with DocumentStore(tmp_path / "s.sqlite3") as store:
        doc = documento("jira:OPS-1")
        store.upsert(doc)
        store.mark_indexed(doc.doc_id, doc.content_hash())
        store.commit()
        assert store.count_pending_embed() == 1
        assert [d.doc_id for d in store.iter_pending_embed()] == ["jira:OPS-1"]


def test_documento_nao_indexado_nao_entra_na_fila_do_denso(tmp_path):
    """O embed faz UPDATE de vetor: sem ponto no Qdrant não há o que atualizar."""
    with DocumentStore(tmp_path / "s.sqlite3") as store:
        store.upsert(documento("jira:OPS-1"))
        store.commit()
        assert store.count_pending_embed() == 0
        assert list(store.iter_pending_embed()) == []


def test_segunda_rodada_de_embed_nao_processa_nada(tmp_path):
    with DocumentStore(tmp_path / "s.sqlite3") as store:
        doc = documento("jira:OPS-1")
        store.upsert(doc)
        store.mark_indexed(doc.doc_id, doc.content_hash())
        store.mark_embedded(doc.doc_id, doc.content_hash())
        store.commit()
        assert store.count_pending_embed() == 0


def test_conteudo_alterado_volta_para_a_fila_do_denso(tmp_path):
    with DocumentStore(tmp_path / "s.sqlite3") as store:
        doc = documento("jira:OPS-1")
        store.upsert(doc)
        store.mark_indexed(doc.doc_id, doc.content_hash())
        store.mark_embedded(doc.doc_id, doc.content_hash())
        store.commit()

        novo = documento("jira:OPS-1", corpo="corpo revisado")
        store.upsert(novo)
        store.mark_indexed(novo.doc_id, novo.content_hash())
        store.commit()
        assert store.count_pending_embed() == 1


def test_progresso_do_embed_sobrevive_a_queda_no_meio(tmp_path):
    """Metade embedada e o processo morre: a próxima rodada pega só o resto."""
    caminho = tmp_path / "s.sqlite3"
    with DocumentStore(caminho) as store:
        for i in range(10):
            doc = documento(f"jira:OPS-{i}")
            store.upsert(doc)
            store.mark_indexed(doc.doc_id, doc.content_hash())
        store.commit()
        for i in range(5):
            doc = documento(f"jira:OPS-{i}")
            store.mark_embedded(doc.doc_id, doc.content_hash())
        store.commit()

    with DocumentStore(caminho) as store:
        assert store.count_pending_embed() == 5
        restantes = sorted(d.doc_id for d in store.iter_pending_embed())
        assert restantes == [f"jira:OPS-{i}" for i in range(5, 10)]


def test_falha_em_um_documento_nao_represa_a_fila(tmp_path):
    """O cursor pagina por doc_id crescente, então avança mesmo sem marcar."""
    with DocumentStore(tmp_path / "s.sqlite3") as store:
        for i in range(5):
            doc = documento(f"jira:OPS-{i}")
            store.upsert(doc)
            store.mark_indexed(doc.doc_id, doc.content_hash())
        store.commit()
        vistos = [d.doc_id for d in store.iter_pending_embed(batch_size=2)]
        assert len(vistos) == 5
        assert len(set(vistos)) == 5


def test_reembed_all_recoloca_tudo_na_fila(tmp_path):
    with DocumentStore(tmp_path / "s.sqlite3") as store:
        for i in range(3):
            doc = documento(f"jira:OPS-{i}")
            store.upsert(doc)
            store.mark_indexed(doc.doc_id, doc.content_hash())
            store.mark_embedded(doc.doc_id, doc.content_hash())
        store.commit()
        assert store.count_pending_embed() == 0
        store.mark_all_unembedded()
        store.commit()
        assert store.count_pending_embed() == 3


def test_denso_e_sparse_tem_progresso_independente(tmp_path):
    """Reindexar o sparse não pode obrigar a reembedar, e vice-versa."""
    with DocumentStore(tmp_path / "s.sqlite3") as store:
        doc = documento("jira:OPS-1")
        store.upsert(doc)
        store.mark_indexed(doc.doc_id, doc.content_hash())
        store.mark_embedded(doc.doc_id, doc.content_hash())
        store.commit()

        store.mark_all_unembedded()
        store.commit()
        assert store.count_pending_index() == 0
        assert store.count_pending_embed() == 1


def test_store_antigo_ganha_a_coluna_do_denso_sem_perder_documento(tmp_path):
    """Migração de store em produção: 33 mil documentos não são recriados."""
    import sqlite3

    caminho = tmp_path / "antigo.sqlite3"
    conn = sqlite3.connect(caminho)
    conn.executescript(
        """
        CREATE TABLE documents (
            doc_id TEXT PRIMARY KEY, source TEXT NOT NULL, content_type TEXT,
            title TEXT NOT NULL, body_text TEXT NOT NULL, url TEXT NOT NULL,
            updated TEXT, project TEXT, status TEXT, issue_type TEXT,
            space_key TEXT, labels_json TEXT NOT NULL DEFAULT '[]',
            content_hash TEXT NOT NULL, extracted_at TEXT NOT NULL,
            indexed_hash TEXT
        );
        INSERT INTO documents (doc_id, source, title, body_text, url,
                               content_hash, extracted_at, indexed_hash)
        VALUES ('jira:OPS-1', 'jira', 'T', 'corpo', 'https://x', 'h', 'agora', 'h');
        """
    )
    conn.commit()
    conn.close()

    with DocumentStore(caminho) as store:
        assert store.stats().documents == 1
        colunas = {
            row["name"] for row in store._conn.execute("PRAGMA table_info(documents)")
        }
        assert "embedded_hash" in colunas
        # indexed_hash preservado, embedded_hash nulo: entra na fila do denso.
        assert store.count_pending_index() == 0
        assert store.count_pending_embed() == 1


# --------------------------------------------------------------------------
# escolha de GPU: a armadilha que custou uma execução inteira
# --------------------------------------------------------------------------

class FakeProps:
    def __init__(self, arch: str, cus: int, vram_gib: float = 16.0) -> None:
        self.gcnArchName = arch
        self.multi_processor_count = cus
        self.total_memory = int(vram_gib * 2**30)


class FakeTorch:
    """Dublê do módulo torch, só com o que _resolve_device consulta."""

    __version__ = "2.14.0+rocm7.2"

    class version:
        hip = "7.2.53211"

    def __init__(self, devices: list[FakeProps]) -> None:
        self._devices = devices
        outer = self

        class cuda:
            @staticmethod
            def is_available() -> bool:
                return bool(outer._devices)

            @staticmethod
            def device_count() -> int:
                return len(outer._devices)

            @staticmethod
            def get_device_properties(index: int) -> FakeProps:
                return outer._devices[index]

        self.cuda = cuda


DISCRETA = FakeProps("gfx1030", 40, 15.98)
IGPU = FakeProps("gfx1036", 1, 15.11)


def test_cpu_e_devolvido_sem_consultar_gpu():
    from indexer.embeddings import _resolve_device

    assert _resolve_device("cpu", FakeTorch([])) == "cpu"


def test_escolhe_a_discreta_quando_a_igpu_vem_primeiro():
    """A iGPU reporta 15,11 GiB de "VRAM" contra 15,98 da discreta.

    Tamanho de memória não distingue as duas; compute units distinguem.
    """
    from indexer.embeddings import _resolve_device

    assert _resolve_device("cuda", FakeTorch([IGPU, DISCRETA])) == "cuda:1"


def test_escolhe_a_discreta_quando_ela_vem_primeiro():
    from indexer.embeddings import _resolve_device

    assert _resolve_device("cuda", FakeTorch([DISCRETA, IGPU])) == "cuda:0"


def test_recusa_rodar_so_na_igpu():
    """Aconteceu de verdade: rodou 7x mais lento, sem erro, e ninguém notou."""
    from indexer.embeddings import _resolve_device

    with pytest.raises(EmbeddingError) as exc:
        _resolve_device("cuda", FakeTorch([IGPU]))
    mensagem = str(exc.value)
    assert "INTEGRADA" in mensagem
    assert "gfx1036" in mensagem
    assert "EMBED_DEVICE=cpu" in mensagem


def test_sem_gpu_nenhuma_da_mensagem_com_versao_do_torch():
    from indexer.embeddings import _resolve_device

    with pytest.raises(EmbeddingError, match="rocminfo"):
        _resolve_device("cuda", FakeTorch([]))


def test_indice_explicito_e_respeitado():
    from indexer.embeddings import _resolve_device

    assert _resolve_device("cuda:1", FakeTorch([DISCRETA, DISCRETA])) == "cuda:1"


def test_indice_explicito_fora_da_faixa_aborta():
    from indexer.embeddings import _resolve_device

    with pytest.raises(EmbeddingError, match="só há 1 GPU"):
        _resolve_device("cuda:3", FakeTorch([DISCRETA]))


def test_indice_explicito_apontando_para_a_igpu_tambem_aborta():
    """Fixar o índice não deve permitir cair na integrada por engano."""
    from indexer.embeddings import _resolve_device

    with pytest.raises(EmbeddingError, match="INTEGRADA"):
        _resolve_device("cuda:0", FakeTorch([IGPU, DISCRETA]))


def test_config_aceita_indice_explicito(monkeypatch):
    monkeypatch.setenv("EMBED_DEVICE", "cuda:1")
    monkeypatch.setenv("JIRA_URL", "https://jira.exemplo")
    monkeypatch.setenv("JIRA_PAT", "x")
    monkeypatch.setenv("JIRA_PROJECTS", "OPS")
    cfg = load_config(dotenv=False)
    assert cfg.embedding.device == "cuda:1"
    assert not any("EMBED_DEVICE" in erro for erro in cfg._errors)


# --------------------------------------------------------------------------
# fusão RRF: o peso não é igual de propósito
# --------------------------------------------------------------------------

class FakeQdrant:
    """Captura o que seria enviado ao servidor, sem servidor."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def query_points(self, **kwargs: object):
        self.calls.append(kwargs)

        class Resposta:
            points: list[object] = []

        return Resposta()

    def close(self) -> None:
        pass


class FakeSparse:
    """Dublê do BM25 do fastembed."""

    class Vetor:
        def __init__(self) -> None:
            import numpy as np

            self.indices = np.array([1, 2, 3])
            self.values = np.array([0.5, 0.5, 0.5])

    def query_embed(self, text: str):
        return [self.Vetor()]


def index_com_dubles(tmp_path):
    index = KnowledgeIndex("http://127.0.0.1:1", "coletanea", tmp_path)
    fake = FakeQdrant()
    index._client = fake  # type: ignore[assignment]
    index._model = FakeSparse()
    index._embedder = DenseEmbedder(
        make_config(device="cpu"), encoder=FakeEncoder()
    )
    return index, fake


def test_hybrid_usa_rrf_ponderado_no_servidor(tmp_path):
    """A fusão é do Qdrant, não do cliente: prefetch + RrfQuery numa viagem."""
    from qdrant_client import models

    index, fake = index_com_dubles(tmp_path)
    index.search("boleto do pedido", limit=5, mode="hybrid")

    assert len(fake.calls) == 1, "híbrido tem que ser UMA requisição, não duas"
    enviado = fake.calls[0]
    assert isinstance(enviado["query"], models.RrfQuery)
    assert enviado["query"].rrf.weights is not None


def test_peso_do_bm25_e_maior_e_na_ordem_do_prefetch(tmp_path):
    """A ordem dos pesos tem que casar com a ordem dos prefetch: bm25 primeiro.

    Invertida, o peso maior iria para o denso e o identificador exato voltaria
    a perder o topo — medido: 1/3 de acerto contra 3/3.
    """
    index, fake = index_com_dubles(tmp_path)
    index.search("VENDAS-14993", limit=5, mode="hybrid")

    enviado = fake.calls[0]
    prefetch = enviado["prefetch"]
    pesos = enviado["query"].rrf.weights

    assert len(prefetch) == len(pesos) == 2
    assert prefetch[0].using == "bm25"
    assert prefetch[1].using == "dense"
    assert pesos[0] > pesos[1], "o BM25 precisa pesar mais que o denso"


def test_pesos_sao_sobreponiveis(tmp_path):
    index, fake = index_com_dubles(tmp_path)
    index.search("x", limit=5, mode="hybrid", bm25_weight=7.0, dense_weight=3.0)
    assert fake.calls[0]["query"].rrf.weights == [7.0, 3.0]


def test_filtro_vai_dentro_de_cada_prefetch(tmp_path):
    """Filtrar só no topo deixaria cada perna gastar vagas fora do escopo."""
    index, fake = index_com_dubles(tmp_path)
    index.search("x", limit=5, mode="hybrid", source="jira")
    for perna in fake.calls[0]["prefetch"]:
        assert perna.filter is not None


def test_perna_densa_busca_muito_mais_fundo_que_a_do_bm25(tmp_path):
    """A assimetria é o ajuste que mais importa na fusão.

    O RRF soma 1/(k+rank), então cada candidato que o BM25 traz ocupa posição
    boa mesmo sendo irrelevante para uma consulta em prosa, e dilui o acerto do
    denso. Medido: bm25=50/denso=50 põe o alvo em 46; bm25=5/denso=100 põe em
    17, sem perder o identificador exato.
    """
    index, fake = index_com_dubles(tmp_path)
    index.search("x", limit=5, mode="hybrid")
    bm25_leg, dense_leg = fake.calls[0]["prefetch"]
    assert dense_leg.limit >= 100
    assert dense_leg.limit > bm25_leg.limit * 5


def test_prefetch_limit_explicito_iguala_as_duas_pernas(tmp_path):
    """Escape hatch para experimento A/B: força a mesma profundidade."""
    index, fake = index_com_dubles(tmp_path)
    index.search("x", limit=5, mode="hybrid", prefetch_limit=77)
    for perna in fake.calls[0]["prefetch"]:
        assert perna.limit == 77


def test_hnsw_ef_alto_na_perna_densa(tmp_path):
    """Com o ef padrão, duas execuções da mesma consulta divergiam."""
    index, fake = index_com_dubles(tmp_path)
    index.search("x", limit=5, mode="hybrid")
    densa = fake.calls[0]["prefetch"][1]
    assert densa.params is not None and densa.params.hnsw_ef >= 256


def test_bm25_puro_nao_toca_no_denso(tmp_path):
    """Modo bm25 não pode exigir GPU nem carregar o e5."""
    index = KnowledgeIndex("http://127.0.0.1:1", "coletanea", tmp_path)
    fake = FakeQdrant()
    index._client = fake  # type: ignore[assignment]
    index._model = FakeSparse()
    # nenhum embedder injetado: se o bm25 tocar no denso, levanta IndexError_
    index.search("VENDAS-14993", limit=5, mode="bm25")
    assert fake.calls[0]["using"] == "bm25"
    assert "prefetch" not in fake.calls[0]


def test_denso_puro_nao_usa_prefetch_nem_fusao(tmp_path):
    index, fake = index_com_dubles(tmp_path)
    index.search("boleto", limit=5, mode="dense")
    assert fake.calls[0]["using"] == "dense"
    assert "prefetch" not in fake.calls[0]


# --------------------------------------------------------------------------
# deduplicação por documento
# --------------------------------------------------------------------------

def ponto(doc_id: str, chunk: str, score: float):
    class Ponto:
        id = chunk
        payload = {"doc_id": doc_id, "text": "t", "title": "T",
                   "url": "https://x", "source": "jira"}
    Ponto.score = score
    return Ponto()


class FakeQdrantComPontos(FakeQdrant):
    def __init__(self, pontos) -> None:
        super().__init__()
        self._pontos = pontos

    def query_points(self, **kwargs):
        self.calls.append(kwargs)
        limite = kwargs.get("limit", 10)

        class Resposta:
            points = self._pontos[:limite]

        return Resposta()


def test_dedupe_devolve_a_melhor_fatia_de_cada_documento(tmp_path):
    """Documento longo não pode ocupar várias vagas com fatias vizinhas.

    Medido na coleção real: sem isto, 3 de 5 resultados vinham de 3 documentos
    só, e o restante do contexto do modelo ia para texto repetido.
    """
    pontos = [
        ponto("jira:A", "a1", 0.9), ponto("jira:A", "a2", 0.89),
        ponto("jira:A", "a3", 0.88), ponto("jira:B", "b1", 0.80),
        ponto("jira:C", "c1", 0.70), ponto("jira:D", "d1", 0.60),
    ]
    index = KnowledgeIndex("http://127.0.0.1:1", "c", tmp_path)
    index._client = FakeQdrantComPontos(pontos)  # type: ignore[assignment]
    index._model = FakeSparse()
    hits = index.search("x", limit=3, mode="bm25")
    assert [h.doc_id for h in hits] == ["jira:A", "jira:B", "jira:C"]
    assert [h.chunk_id for h in hits] == ["a1", "b1", "c1"], "a melhor fatia"


def test_dedupe_busca_mais_fundo_do_que_o_limite_pedido(tmp_path):
    index = KnowledgeIndex("http://127.0.0.1:1", "c", tmp_path)
    fake = FakeQdrantComPontos([])
    index._client = fake  # type: ignore[assignment]
    index._model = FakeSparse()
    index.search("x", limit=5, mode="bm25")
    assert fake.calls[0]["limit"] > 5


def test_dedupe_pode_ser_desligado(tmp_path):
    pontos = [ponto("jira:A", "a1", 0.9), ponto("jira:A", "a2", 0.8)]
    index = KnowledgeIndex("http://127.0.0.1:1", "c", tmp_path)
    index._client = FakeQdrantComPontos(pontos)  # type: ignore[assignment]
    index._model = FakeSparse()
    hits = index.search("x", limit=2, mode="bm25", dedupe_by_document=False)
    assert [h.chunk_id for h in hits] == ["a1", "a2"]


def test_dedupe_nunca_devolve_mais_que_o_limite(tmp_path):
    pontos = [ponto(f"jira:{i}", f"c{i}", 1.0 - i / 100) for i in range(20)]
    index = KnowledgeIndex("http://127.0.0.1:1", "c", tmp_path)
    index._client = FakeQdrantComPontos(pontos)  # type: ignore[assignment]
    index._model = FakeSparse()
    assert len(index.search("x", limit=4, mode="bm25")) == 4


# --------------------------------------------------------------------------
# modo auto: a consulta escolhe o instrumento
# --------------------------------------------------------------------------

def test_identificador_sozinho_vai_para_o_lexical():
    """BM25 acerta identificador em 1º; o híbrido perdia PRUPSYNCPRODUTOS."""
    from indexer.index import classify_query

    assert classify_query("VENDAS-14993") == "bm25"
    assert classify_query("PRUPSYNCPRODUTOS") == "bm25"
    assert classify_query("ERR-4012") == "bm25"
    assert classify_query("  ECOMMERCE-240  ") == "bm25"


def test_prosa_vai_para_o_semantico():
    """Medido: o denso acha os 3 alvos de sinônimo, o BM25 nenhum."""
    from indexer.index import classify_query

    assert classify_query("dados da fatura para pagamento em banco") == "dense"
    assert classify_query("como renovar o certificado do gateway") == "dense"
    assert classify_query("boleto bancário do pedido") == "dense"


def test_identificador_no_meio_de_frase_usa_os_dois():
    from indexer.index import classify_query

    assert classify_query("erro no PRUPSYNCPRODUTOS ao sincronizar pedido") == "hybrid"
    assert classify_query("VENDAS-14993 já está resolvido?") == "hybrid"


def test_sigla_curta_nao_e_confundida_com_identificador():
    """"APP", "SQL", "ERP" são palavras da base, não chaves."""
    from indexer.index import classify_query

    assert classify_query("erro ao gerar o APP") == "dense"
    assert classify_query("script SQL de integração") == "dense"


def test_auto_delega_para_o_modo_escolhido(tmp_path):
    index, fake = index_com_dubles(tmp_path)
    index.search("VENDAS-14993", limit=5, mode="auto")
    assert fake.calls[0]["using"] == "bm25"
    assert "prefetch" not in fake.calls[0]

    index, fake = index_com_dubles(tmp_path)
    index.search("dados da fatura para pagamento", limit=5, mode="auto")
    assert fake.calls[0]["using"] == "dense"


def test_auto_esta_entre_os_modos_aceitos():
    assert "auto" in SEARCH_MODES


def test_auto_com_consulta_vazia_nao_classifica_nem_busca(tmp_path):
    index, fake = index_com_dubles(tmp_path)
    assert index.search("   ", mode="auto") == []
    assert fake.calls == []
