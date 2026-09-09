#!/usr/bin/env bash
# Gera uma CSR para pedir certificado à CA da empresa.
#
#   ./deploy/gerar-csr.sh rag.wmw.com.br 10.2.1.132
#
# Entregue o .csr para quem administra a CA (normalmente a TI). A chave privada
# NAO sai desta maquina: a CSR contem so a parte publica.
#
# Um certificado da CA da empresa e o cenario ideal porque as maquinas do
# dominio JA confiam nela: ninguem precisa instalar CA nenhuma.
set -euo pipefail

NOME="${1:?uso: $0 <hostname> [ip ...]}"; shift
DESTINO="$(cd "$(dirname "$0")/.." && pwd)/certs"
mkdir -p "$DESTINO"

SAN="DNS:${NOME}"
for extra in "$@"; do
    if [[ "$extra" =~ ^[0-9.]+$ ]]; then SAN="${SAN},IP:${extra}"; else SAN="${SAN},DNS:${extra}"; fi
done

openssl req -new -newkey rsa:2048 -nodes \
    -keyout "$DESTINO/mcp-empresa.key" \
    -out    "$DESTINO/mcp-empresa.csr" \
    -subj   "/CN=${NOME}" \
    -addext "subjectAltName=${SAN}" \
    -addext "keyUsage=digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth"

chmod 600 "$DESTINO/mcp-empresa.key"
echo
echo "CSR:            $DESTINO/mcp-empresa.csr   <- entregue este"
echo "chave privada:  $DESTINO/mcp-empresa.key   <- NUNCA entregue"
echo "SAN pedido:     ${SAN}"
echo
echo "Quando a TI devolver o certificado assinado, aponte o .env para ele:"
echo "  MCP_TLS_CERT=$DESTINO/mcp-empresa.crt"
echo "  MCP_TLS_KEY=$DESTINO/mcp-empresa.key"
echo "e reinicie:  sudo systemctl restart rag-mcp"
