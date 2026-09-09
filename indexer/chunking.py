"""Fatiamento de documentos para o índice.

Duas decisões carregam quase todo o ganho de recall:

  - respeitar fronteira de cabeçalho e de parágrafo, para que a fatia não
    corte um procedimento no meio;
  - prefixar o título do documento em TODA fatia. Sem isso uma fatia do meio
    de uma página fica órfã de contexto ("reinicie o serviço e valide o
    retorno" - serviço de quê?) e deixa de casar com a consulta.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Iterable, Sequence

LOG = logging.getLogger("indexer.chunking")

DEFAULT_MAX_CHARS = 1400
DEFAULT_OVERLAP = 200
MAX_TITLE_CHARS = 200
MIN_BUDGET_CHARS = 200

_HEADING_RE = re.compile(r"(?m)^(#{1,6})[ \t]+(.*\S)[ \t]*$")
_SENTENCE_RE = re.compile(r"(?<=[.!?…;:])\s+")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    index: int
    text: str
    heading: str | None

    @property
    def char_count(self) -> int:
        return len(self.text)


def chunk_id_for(doc_id: str, index: int) -> str:
    """ID determinístico: reindexar o mesmo documento sobrescreve, não duplica."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}#{index}"))


def _title_prefix(title: str) -> str:
    clean = re.sub(r"\s+", " ", (title or "").strip())
    if len(clean) > MAX_TITLE_CHARS:
        clean = clean[:MAX_TITLE_CHARS].rstrip() + "…"
    return f"{clean}\n\n" if clean else ""


def _sections(body: str) -> list[tuple[str | None, str]]:
    matches = list(_HEADING_RE.finditer(body))
    if not matches:
        return [(None, body.strip())] if body.strip() else []

    sections: list[tuple[str | None, str]] = []
    preamble = body[: matches[0].start()].strip()
    if preamble:
        sections.append((None, preamble))
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(body)
        heading_line = match.group(0).strip()
        rest = body[match.end() : end].strip()
        # Garante linha em branco após o cabeçalho para que ele seja um
        # parágrafo próprio na hora de empacotar.
        text = f"{heading_line}\n\n{rest}" if rest else heading_line
        sections.append((match.group(2).strip(), text))
    return sections


def _hard_split(text: str, budget: int) -> list[str]:
    pieces: list[str] = []
    for sentence in _SENTENCE_RE.split(text):
        current = sentence.strip()
        while len(current) > budget:
            cut = current.rfind(" ", 0, budget)
            if cut <= budget // 2:
                cut = budget
            pieces.append(current[:cut].strip())
            current = current[cut:].strip()
        if current:
            pieces.append(current)
    return pieces or [text[:budget]]


def _atoms(section: str, budget: int) -> list[tuple[str, str]]:
    """Unidades indivisíveis, com o separador que as reúne."""
    atoms: list[tuple[str, str]] = []
    for paragraph in re.split(r"\n{2,}", section):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= budget:
            atoms.append((paragraph, "\n\n"))
            continue
        for position, piece in enumerate(_hard_split(paragraph, budget)):
            atoms.append((piece, "\n\n" if position == 0 else " "))
    return atoms


def _tail(text: str, overlap: int) -> str:
    if overlap <= 0:
        return ""
    if len(text) <= overlap:
        return text
    tail = text[-overlap:]
    space = tail.find(" ")
    return tail[space + 1 :].strip() if space != -1 else tail.strip()


def _pack(atoms: Sequence[tuple[str, str]], budget: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for text, separator in atoms:
        if not current:
            current = text
            continue
        candidate = current + separator + text
        if len(candidate) <= budget:
            current = candidate
            continue
        chunks.append(current)
        carry = _tail(current, overlap)
        current = f"{carry}{separator}{text}" if carry else text
        if len(current) > budget:
            current = text
    if current:
        chunks.append(current)
    return chunks


def chunk_document(
    doc_id: str,
    title: str,
    body: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    prefix = _title_prefix(title)
    budget = max(MIN_BUDGET_CHARS, max_chars - len(prefix))
    effective_overlap = max(0, min(overlap, budget // 2))

    normalized = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()

    chunks: list[Chunk] = []
    for heading, section in _sections(normalized):
        for piece in _pack(_atoms(section, budget), budget, effective_overlap):
            if not piece.strip():
                continue
            index = len(chunks)
            chunks.append(
                Chunk(
                    chunk_id=chunk_id_for(doc_id, index),
                    doc_id=doc_id,
                    index=index,
                    text=prefix + piece,
                    heading=heading,
                )
            )

    if not chunks:
        # Documento sem corpo ainda precisa ser encontrável pelo título.
        text = prefix.strip() or title.strip()
        if not text:
            return []
        chunks.append(Chunk(chunk_id_for(doc_id, 0), doc_id, 0, text, None))
    return chunks
