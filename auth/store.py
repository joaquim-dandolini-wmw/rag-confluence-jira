"""Estado do OAuth, em SQLite próprio.

Mesmo sem tabela de usuários — quem sabe quem é gente é o LDAP — o OAuth
precisa lembrar de três coisas: os clientes que se registraram sozinhos, os
códigos de autorização em trânsito e os tokens emitidos.

Fica num arquivo **separado** do document store, e isso é decisão, não acaso:
o store viaja entre máquinas nas migrações e é reconstruído por reindexação.
Token não pode viajar junto numa cópia de índice, nem ser tocado por um
`--reindex-all`.

Código e token são gravados como **hash**, nunca em claro. Um vazamento deste
arquivo não entrega credencial utilizável a ninguém, do mesmo jeito que um
vazamento de tabela de senhas bem feita não entrega senha.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

LOG = logging.getLogger("auth.store")

BUSY_TIMEOUT_MS = 10_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id  TEXT PRIMARY KEY,
    dados_json TEXT NOT NULL,
    criado_em  TEXT NOT NULL
);

-- Códigos de autorização. Vivem segundos, mas precisam sobreviver a um
-- restart no meio do login: a pessoa está com o navegador aberto.
CREATE TABLE IF NOT EXISTS codigos (
    code_hash      TEXT PRIMARY KEY,
    client_id      TEXT NOT NULL,
    usuario        TEXT NOT NULL,
    redirect_uri   TEXT NOT NULL,
    redirect_explicito INTEGER NOT NULL DEFAULT 0,
    code_challenge TEXT NOT NULL,
    scopes_json    TEXT NOT NULL,
    resource       TEXT,
    expira_em      REAL NOT NULL,
    criado_em      TEXT NOT NULL,
    usado_em       TEXT
);

CREATE TABLE IF NOT EXISTS tokens (
    token_hash  TEXT PRIMARY KEY,
    tipo        TEXT NOT NULL,
    client_id   TEXT NOT NULL,
    usuario     TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    resource    TEXT,
    expira_em   REAL,
    criado_em   TEXT NOT NULL,
    revogado_em TEXT,
    ultimo_uso  TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens_usuario ON tokens(usuario);
CREATE INDEX IF NOT EXISTS idx_tokens_client  ON tokens(client_id);

-- O registro que hoje não existe: quem perguntou o quê começa por quem entrou.
CREATE TABLE IF NOT EXISTS logins (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario   TEXT NOT NULL,
    client_id TEXT,
    ip        TEXT,
    resultado TEXT NOT NULL,
    em        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logins_em ON logins(em);

-- Chaves do próprio servidor (HMAC do pedido de login). Ficam aqui para
-- sobreviver a restart: se mudassem a cada boot, todo login em andamento
-- morreria no meio, e o refresh do dia seguinte também.
CREATE TABLE IF NOT EXISTS segredos (
    nome      TEXT PRIMARY KEY,
    valor     TEXT NOT NULL,
    criado_em TEXT NOT NULL
);
"""


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_segredo(valor: str) -> str:
    return hashlib.sha256(valor.encode("utf-8")).hexdigest()


def novo_segredo(bytes_: int = 32) -> str:
    return secrets.token_urlsafe(bytes_)


@dataclass(frozen=True)
class CodigoGravado:
    client_id: str
    usuario: str
    redirect_uri: str
    redirect_explicito: bool
    code_challenge: str
    scopes: list[str]
    resource: str | None
    expira_em: float


@dataclass(frozen=True)
class TokenGravado:
    tipo: str
    client_id: str
    usuario: str
    scopes: list[str]
    resource: str | None
    expira_em: float | None


class AuthStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conectar() as conexao:
            conexao.executescript(_SCHEMA)

    def _conectar(self) -> sqlite3.Connection:
        conexao = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000)
        conexao.row_factory = sqlite3.Row
        conexao.execute("PRAGMA journal_mode=WAL")
        conexao.execute("PRAGMA synchronous=NORMAL")
        conexao.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return conexao

    # --- chaves do servidor ----------------------------------------------
    def chave(self, nome: str) -> str:
        """Devolve a chave, criando na primeira chamada. Estável depois.

        O INSERT OR IGNORE seguido de SELECT é de propósito: dois processos
        subindo ao mesmo tempo não podem acabar com chaves diferentes, senão
        um invalida os pedidos de login do outro.
        """
        with self._conectar() as conexao:
            conexao.execute(
                "INSERT OR IGNORE INTO segredos (nome, valor, criado_em) VALUES (?,?,?)",
                (nome, novo_segredo(32), _agora_iso()),
            )
            linha = conexao.execute(
                "SELECT valor FROM segredos WHERE nome=?", (nome,)
            ).fetchone()
        return linha["valor"]

    # --- clientes registrados via DCR ------------------------------------
    def salvar_cliente(self, client_id: str, dados_json: str) -> None:
        with self._conectar() as conexao:
            conexao.execute(
                "INSERT INTO clients (client_id, dados_json, criado_em) VALUES (?,?,?) "
                "ON CONFLICT(client_id) DO UPDATE SET dados_json=excluded.dados_json",
                (client_id, dados_json, _agora_iso()),
            )

    def ler_cliente(self, client_id: str) -> str | None:
        with self._conectar() as conexao:
            linha = conexao.execute(
                "SELECT dados_json FROM clients WHERE client_id=?", (client_id,)
            ).fetchone()
        return linha["dados_json"] if linha else None

    def listar_clientes(self) -> list[dict[str, Any]]:
        with self._conectar() as conexao:
            return [dict(l) for l in conexao.execute(
                "SELECT client_id, dados_json, criado_em FROM clients ORDER BY criado_em DESC"
            )]

    # --- códigos de autorização ------------------------------------------
    def gravar_codigo(self, codigo: str, dados: CodigoGravado) -> None:
        with self._conectar() as conexao:
            conexao.execute(
                "INSERT INTO codigos (code_hash, client_id, usuario, redirect_uri, "
                "redirect_explicito, code_challenge, scopes_json, resource, expira_em, "
                "criado_em) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (hash_segredo(codigo), dados.client_id, dados.usuario, dados.redirect_uri,
                 int(dados.redirect_explicito), dados.code_challenge,
                 json.dumps(dados.scopes), dados.resource, dados.expira_em, _agora_iso()),
            )

    def consumir_codigo(self, codigo: str) -> CodigoGravado | None:
        """Lê e marca como usado na MESMA transação.

        Código de autorização é de uso único, e "único" tem que valer também
        quando dois pedidos chegam ao mesmo tempo — é o que impede que um
        código interceptado seja trocado por token em paralelo com o dono.
        O UPDATE condicional faz o SQLite decidir quem chegou primeiro.
        """
        chave = hash_segredo(codigo)
        with self._conectar() as conexao:
            cursor = conexao.execute(
                "UPDATE codigos SET usado_em=? WHERE code_hash=? AND usado_em IS NULL",
                (_agora_iso(), chave),
            )
            if cursor.rowcount != 1:
                return None
            linha = conexao.execute(
                "SELECT * FROM codigos WHERE code_hash=?", (chave,)
            ).fetchone()
        if linha is None or linha["expira_em"] < time.time():
            return None
        return CodigoGravado(
            client_id=linha["client_id"],
            usuario=linha["usuario"],
            redirect_uri=linha["redirect_uri"],
            redirect_explicito=bool(linha["redirect_explicito"]),
            code_challenge=linha["code_challenge"],
            scopes=json.loads(linha["scopes_json"]),
            resource=linha["resource"],
            expira_em=linha["expira_em"],
        )

    # --- tokens -----------------------------------------------------------
    def gravar_token(self, token: str, dados: TokenGravado) -> None:
        with self._conectar() as conexao:
            conexao.execute(
                "INSERT INTO tokens (token_hash, tipo, client_id, usuario, scopes_json, "
                "resource, expira_em, criado_em) VALUES (?,?,?,?,?,?,?,?)",
                (hash_segredo(token), dados.tipo, dados.client_id, dados.usuario,
                 json.dumps(dados.scopes), dados.resource, dados.expira_em, _agora_iso()),
            )

    def ler_token(self, token: str, tipo: str) -> TokenGravado | None:
        chave = hash_segredo(token)
        with self._conectar() as conexao:
            linha = conexao.execute(
                "SELECT * FROM tokens WHERE token_hash=? AND tipo=? AND revogado_em IS NULL",
                (chave, tipo),
            ).fetchone()
            if linha is None:
                return None
            if linha["expira_em"] is not None and linha["expira_em"] < time.time():
                return None
            conexao.execute(
                "UPDATE tokens SET ultimo_uso=? WHERE token_hash=?", (_agora_iso(), chave)
            )
        return TokenGravado(
            tipo=linha["tipo"],
            client_id=linha["client_id"],
            usuario=linha["usuario"],
            scopes=json.loads(linha["scopes_json"]),
            resource=linha["resource"],
            expira_em=linha["expira_em"],
        )

    def revogar_token(self, token: str) -> None:
        with self._conectar() as conexao:
            conexao.execute(
                "UPDATE tokens SET revogado_em=? WHERE token_hash=? AND revogado_em IS NULL",
                (_agora_iso(), hash_segredo(token)),
            )

    def revogar_do_usuario(self, usuario: str) -> int:
        """Corta o acesso de uma pessoa agora, em todos os clientes dela.

        É o botão que o painel oferece, e o motivo de os tokens serem opacos e
        guardados aqui em vez de JWT: com JWT, revogar exige lista de bloqueio;
        aqui é um UPDATE.
        """
        with self._conectar() as conexao:
            cursor = conexao.execute(
                "UPDATE tokens SET revogado_em=? WHERE usuario=? AND revogado_em IS NULL",
                (_agora_iso(), usuario),
            )
            return cursor.rowcount

    def limpar_expirados(self) -> int:
        agora = time.time()
        with self._conectar() as conexao:
            n = conexao.execute(
                "DELETE FROM codigos WHERE expira_em < ?", (agora - 3600,)
            ).rowcount
            n += conexao.execute(
                "DELETE FROM tokens WHERE expira_em IS NOT NULL AND expira_em < ?",
                (agora - 86400,),
            ).rowcount
            return n

    # --- registro de acesso ----------------------------------------------
    def registrar_login(self, usuario: str, client_id: str | None, ip: str | None,
                        resultado: str) -> None:
        with self._conectar() as conexao:
            conexao.execute(
                "INSERT INTO logins (usuario, client_id, ip, resultado, em) VALUES (?,?,?,?,?)",
                (usuario, client_id, ip, resultado, _agora_iso()),
            )

    def ultimos_logins(self, limite: int = 100) -> list[dict[str, Any]]:
        with self._conectar() as conexao:
            return [dict(l) for l in conexao.execute(
                "SELECT usuario, client_id, ip, resultado, em FROM logins "
                "ORDER BY id DESC LIMIT ?", (limite,)
            )]

    def sessoes_ativas(self) -> list[dict[str, Any]]:
        with self._conectar() as conexao:
            return [dict(l) for l in conexao.execute(
                "SELECT usuario, client_id, tipo, criado_em, ultimo_uso, expira_em "
                "FROM tokens WHERE revogado_em IS NULL AND tipo='access' "
                "AND (expira_em IS NULL OR expira_em > ?) ORDER BY criado_em DESC",
                (time.time(),),
            )]
