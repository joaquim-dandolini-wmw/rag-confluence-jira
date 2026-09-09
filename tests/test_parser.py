"""Testes do parser de storage format do Confluence 4.2.4.

Todas as amostras são montadas à mão: nada aqui toca uma instância real.
Cada uma das sete armadilhas conhecidas tem pelo menos um teste dedicado.
"""

from __future__ import annotations

import re

import pytest

from connectors.confluence_legacy import storage_to_text, wiki_to_text


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# Armadilha 1: fragmento sem elemento raiz
# --------------------------------------------------------------------------

def test_fragmento_com_varios_irmaos_nao_perde_conteudo() -> None:
    storage = (
        "<p>Primeiro parágrafo.</p>"
        "<p>Segundo parágrafo.</p>"
        "<h2>Uma seção</h2>"
        "<p>Terceiro parágrafo.</p>"
    )
    text = storage_to_text(storage)
    assert "Primeiro parágrafo." in text
    assert "Segundo parágrafo." in text
    assert "Terceiro parágrafo." in text
    assert "## Uma seção" in text


def test_namespaces_ac_e_ri_nao_quebram_o_parse() -> None:
    storage = (
        '<p>antes</p>'
        '<ac:structured-macro ac:name="info">'
        '<ac:rich-text-body><p>conteúdo do painel</p></ac:rich-text-body>'
        "</ac:structured-macro>"
        "<p>depois</p>"
    )
    text = storage_to_text(storage)
    assert "antes" in text
    assert "conteúdo do painel" in text
    assert "depois" in text


# --------------------------------------------------------------------------
# Armadilha 2: entidades HTML nomeadas destruindo acentos
# --------------------------------------------------------------------------

def test_entidades_nomeadas_preservam_a_letra_acentuada() -> None:
    storage = "<p>Voc&ecirc; precisa da informa&ccedil;&atilde;o completa.</p>"
    text = storage_to_text(storage)
    assert "Você precisa da informação completa." in text
    assert "Voc " not in text
    assert "&" not in text


def test_nbsp_vira_espaco_normal_e_nao_u00a0() -> None:
    storage = "<p>certificado&nbsp;vencido</p>"
    text = storage_to_text(storage)
    assert "certificado vencido" in text
    assert " " not in text


def test_entidades_acentuadas_em_massa() -> None:
    storage = (
        "<p>&aacute;&eacute;&iacute;&oacute;&uacute; &agrave; &atilde;&otilde; "
        "&ccedil; &acirc;&ecirc;&ocirc; &uuml; &Aacute;&Ccedil;&Otilde;</p>"
    )
    text = storage_to_text(storage)
    assert "áéíóú à ãõ ç âêô ü ÁÇÕ" in text


def test_markup_escapado_continua_sendo_texto() -> None:
    # O unescape é seletivo: se ele desfizesse &lt;/&gt; o parser passaria a
    # ler o exemplo como se fosse tag de verdade e o conteúdo sumiria.
    storage = "<p>Use &lt;service-account&gt; no arquivo &amp; reinicie.</p>"
    text = storage_to_text(storage)
    assert "<service-account>" in text
    assert "&" in text or "reinicie" in text
    assert "Use <service-account> no arquivo & reinicie." in text


# --------------------------------------------------------------------------
# Armadilha 3: cabeçalho quebrando em duas linhas
# --------------------------------------------------------------------------

@pytest.mark.parametrize("level", [1, 2, 3, 4, 5, 6])
def test_cabecalho_fica_em_uma_linha_unica(level: int) -> None:
    storage = f"<h{level}>Configuração do proxy</h{level}><p>corpo</p>"
    text = storage_to_text(storage)
    expected = "#" * level + " Configuração do proxy"
    assert expected in _lines(text)
    assert not any(line.strip() in {"#" * level, "#"} for line in text.splitlines())


def test_cabecalho_com_formatacao_interna_nao_quebra() -> None:
    storage = "<h2>Passo <strong>2</strong>: reiniciar</h2>"
    text = storage_to_text(storage)
    assert "## Passo 2: reiniciar" in _lines(text)


# --------------------------------------------------------------------------
# Armadilha 4: bloco de código dentro de CDATA
# --------------------------------------------------------------------------

def test_codigo_em_cdata_com_ac_macro_da_versao_4_2() -> None:
    # 4.0-4.2 usam <ac:macro>; <ac:structured-macro> só chegou na 4.3.
    storage = (
        "<p>Reinicie o serviço:</p>"
        '<ac:macro ac:name="code">'
        '<ac:parameter ac:name="language">bash</ac:parameter>'
        "<ac:plain-text-body><![CDATA[systemctl restart nginx\n"
        'echo "ok"]]></ac:plain-text-body>'
        "</ac:macro>"
    )
    text = storage_to_text(storage)
    assert "systemctl restart nginx" in text
    assert 'echo "ok"' in text
    assert "```bash" in text


def test_codigo_em_cdata_com_ac_structured_macro() -> None:
    storage = (
        '<ac:structured-macro ac:name="code">'
        '<ac:parameter ac:name="language">sql</ac:parameter>'
        "<ac:plain-text-body><![CDATA[SELECT 1 FROM dual;]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )
    text = storage_to_text(storage)
    assert "SELECT 1 FROM dual;" in text
    assert "```sql" in text


def test_codigo_preserva_indentacao_interna() -> None:
    storage = (
        '<ac:macro ac:name="code"><ac:plain-text-body><![CDATA[def f():\n'
        "    return 1]]></ac:plain-text-body></ac:macro>"
    )
    text = storage_to_text(storage)
    assert "    return 1" in text


def test_noformat_tambem_preserva_o_corpo() -> None:
    storage = (
        '<ac:macro ac:name="noformat"><ac:plain-text-body>'
        "<![CDATA[ERR-4012: timeout no gateway]]>"
        "</ac:plain-text-body></ac:macro>"
    )
    text = storage_to_text(storage)
    assert "ERR-4012: timeout no gateway" in text


def test_macro_nao_code_e_desembrulhada_sem_perder_texto() -> None:
    storage = (
        '<ac:structured-macro ac:name="warning">'
        '<ac:parameter ac:name="title">Atenção</ac:parameter>'
        "<ac:rich-text-body><p>não faça isso em produção</p></ac:rich-text-body>"
        "</ac:structured-macro>"
    )
    text = storage_to_text(storage)
    assert "não faça isso em produção" in text
    assert "Atenção" in text


# --------------------------------------------------------------------------
# Armadilha 5: palavras colando quando a tag não é fechada
# --------------------------------------------------------------------------

def test_paragrafo_nao_fechado_nao_cola_palavras() -> None:
    storage = "<p>texto solto<p>outro"
    text = storage_to_text(storage)
    assert "soltooutro" not in text
    assert "texto solto" in text
    assert "outro" in text


def test_div_nao_fechada_nao_cola_palavras() -> None:
    storage = "<div>bloco um<div>bloco dois</div>"
    text = storage_to_text(storage)
    assert "umbloco" not in text


def test_br_vira_quebra_de_linha() -> None:
    storage = "<p>linha um<br/>linha dois</p>"
    text = storage_to_text(storage)
    assert "umlinha" not in text
    assert "linha um" in text and "linha dois" in text


def test_formatacao_inline_nao_ganha_espaco_espurio() -> None:
    storage = "<p>o valor <strong>máximo</strong> permitido</p>"
    text = storage_to_text(storage)
    assert "o valor máximo permitido" in text


# --------------------------------------------------------------------------
# Armadilha 6: fallback de parser
# --------------------------------------------------------------------------

def test_xml_mal_formado_cai_no_fallback_e_nao_levanta() -> None:
    # `&` solto e tag cruzada: inválido em XML, tolerável em html.parser.
    storage = "<p>vendas & marketing <b>importante</p></b><p>segundo</p>"
    text = storage_to_text(storage)
    assert "vendas" in text
    assert "importante" in text
    assert "segundo" in text


def test_fallback_ainda_extrai_codigo_em_cdata() -> None:
    storage = (
        "<p>quebrado & sem fechar"
        '<ac:macro ac:name="code"><ac:plain-text-body>'
        "<![CDATA[cat /etc/hosts]]></ac:plain-text-body></ac:macro>"
    )
    text = storage_to_text(storage)
    assert "cat /etc/hosts" in text


def test_lixo_binario_nao_derruba_a_extracao() -> None:
    assert isinstance(storage_to_text("<<<>>> \x0c ]]> <p"), str)


# --------------------------------------------------------------------------
# Tabelas
# --------------------------------------------------------------------------

def test_tabela_sem_tbody() -> None:
    storage = (
        "<table>"
        "<tr><th>Ambiente</th><th>Porta</th></tr>"
        "<tr><td>produção</td><td>8443</td></tr>"
        "<tr><td>homologação</td><td>8080</td></tr>"
        "</table>"
    )
    lines = _lines(storage_to_text(storage))
    assert "| Ambiente | Porta |" in lines
    assert "| --- | --- |" in lines
    assert "| produção | 8443 |" in lines
    assert "| homologação | 8080 |" in lines


def test_tabela_com_tbody_e_thead() -> None:
    storage = (
        "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
        "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
    )
    lines = _lines(storage_to_text(storage))
    assert "| A | B |" in lines
    assert "| 1 | 2 |" in lines


def test_celula_com_markup_interno_vira_texto_plano() -> None:
    storage = "<table><tr><td><p>um</p><p>dois</p></td><td>três</td></tr></table>"
    lines = _lines(storage_to_text(storage))
    assert "| um dois | três |" in lines


# --------------------------------------------------------------------------
# Listas
# --------------------------------------------------------------------------

def test_lista_aninhada_preserva_hierarquia() -> None:
    storage = (
        "<ul>"
        "<li>primeiro<ul><li>primeiro.a</li><li>primeiro.b</li></ul></li>"
        "<li>segundo</li>"
        "</ul>"
    )
    lines = _lines(storage_to_text(storage))
    assert "- primeiro" in lines
    assert "  - primeiro.a" in lines
    assert "  - primeiro.b" in lines
    assert "- segundo" in lines


def test_lista_ordenada() -> None:
    storage = "<ol><li>abrir chamado</li><li>anexar log</li></ol>"
    lines = _lines(storage_to_text(storage))
    assert "1. abrir chamado" in lines
    assert "2. anexar log" in lines


# --------------------------------------------------------------------------
# Página vazia
# --------------------------------------------------------------------------

@pytest.mark.parametrize("storage", ["", "   ", "\n\n\t ", "<p></p>", "<p>&nbsp;</p>", None])
def test_pagina_vazia_ou_so_whitespace_retorna_string_vazia(storage: str | None) -> None:
    assert storage_to_text(storage) == ""


# --------------------------------------------------------------------------
# Links internos
# --------------------------------------------------------------------------

def test_ac_link_com_ri_page_preserva_o_titulo_da_pagina() -> None:
    storage = (
        "<p>Veja também "
        "<ac:link><ri:page ri:content-title=\"Política de Backup\" />"
        "<ac:plain-text-link-body><![CDATA[a política]]></ac:plain-text-link-body>"
        "</ac:link>.</p>"
    )
    text = storage_to_text(storage)
    assert "Política de Backup" in text
    assert "a política" in text


def test_ac_link_sem_corpo_usa_o_titulo() -> None:
    storage = '<p>Ver <ac:link><ri:page ri:content-title="Runbook do Proxy" /></ac:link></p>'
    text = storage_to_text(storage)
    assert "Runbook do Proxy" in text


def test_ac_link_para_anexo_usa_o_nome_do_arquivo() -> None:
    storage = '<p><ac:link><ri:attachment ri:filename="topologia.png" /></ac:link></p>'
    assert "topologia.png" in storage_to_text(storage)


def test_ac_image_registra_o_anexo() -> None:
    storage = '<p><ac:image><ri:attachment ri:filename="diagrama.png" /></ac:image></p>'
    assert "diagrama.png" in storage_to_text(storage)


def test_link_externo_mantem_texto_e_href() -> None:
    storage = '<p><a href="https://intranet/erro">ERR-4012</a></p>'
    text = storage_to_text(storage)
    assert "ERR-4012" in text


# --------------------------------------------------------------------------
# Wiki markup residual pré-4.0 não migrado
# --------------------------------------------------------------------------

def test_wiki_residual_cabecalho_e_code() -> None:
    storage = (
        "h2. Procedimento de rollback\n"
        "Pare o serviço antes.\n"
        "{code:bash}\n"
        "systemctl stop app\n"
        "{code}\n"
        "h3. Validação\n"
    )
    text = storage_to_text(storage)
    lines = _lines(text)
    assert "## Procedimento de rollback" in lines
    assert "### Validação" in lines
    assert "systemctl stop app" in text
    assert "{code" not in text


def test_wiki_residual_dentro_de_macro_unmigrated() -> None:
    storage = (
        '<ac:macro ac:name="unmigrated-wiki-markup"><ac:plain-text-body>'
        "<![CDATA[h2. Título antigo\n{noformat}\nvalor=1\n{noformat}]]>"
        "</ac:plain-text-body></ac:macro>"
    )
    text = storage_to_text(storage)
    assert "Título antigo" in text
    assert "valor=1" in text


def test_wiki_to_text_direto() -> None:
    out = wiki_to_text("h1. Topo\n{quote}citação{quote}\n{code}x=1{code}")
    assert "# Topo" in out
    assert "citação" in out
    assert "x=1" in out
    assert "{quote}" not in out


def test_wiki_residual_nao_estraga_texto_normal() -> None:
    storage = "<p>O custo é h2. Não confundir com a versão 2.0 do sistema.</p>"
    text = storage_to_text(storage)
    assert "O custo é h2. Não confundir" in text


# --------------------------------------------------------------------------
# Robustez geral
# --------------------------------------------------------------------------

def test_pagina_longa_mantem_estrutura_de_secoes() -> None:
    storage = "".join(
        f"<h2>Seção {i}</h2><p>Conteúdo da seção {i}.</p>" for i in range(1, 21)
    )
    text = storage_to_text(storage)
    assert len(re.findall(r"^## Seção \d+$", text, re.MULTILINE)) == 20


def test_sem_linhas_em_branco_consecutivas_em_excesso() -> None:
    storage = "<p>a</p><p></p><p></p><p></p><p>b</p>"
    assert "\n\n\n" not in storage_to_text(storage)


def test_saida_nao_tem_espacos_no_fim_das_linhas() -> None:
    storage = "<p>alguma coisa   </p><h2>  Título  </h2>"
    text = storage_to_text(storage)
    assert all(line == line.rstrip() for line in text.splitlines())


def test_sdk_mcp_nao_e_sombreado_por_pacote_local() -> None:
    # O diretório mcp/ da spec original sombrearia o SDK no sys.path.
    import mcp

    assert "site-packages" in (mcp.__file__ or ""), (
        "o pacote 'mcp' importado não é o SDK instalado - há sombreamento local"
    )
