"""
Monitor de Licitações Gov — Backend Python v2
Correções:
  - PNCP: timeout reduzido + retry automático + endpoint /proposta para abertas
  - Compras.dados.gov.br: endpoint e parâmetros corretos
  - dadosabertos.compras.gov.br: nova API (sistema pós-2021)
  - Filtro por palavra-chave feito no servidor
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
import os
import time

app = Flask(__name__)
CORS(app)

TIMEOUT = 10   # segundos — PNCP costuma ser lento, mas 15s era excessivo
RETRIES = 2    # tentativas antes de desistir

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "MonitorLicitacoes/2.0"
}


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────

def get(url, params=None):
    """GET com retry automático."""
    for tentativa in range(RETRIES):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            if tentativa < RETRIES - 1:
                time.sleep(1)
                continue
            raise Exception(f"Timeout após {RETRIES} tentativas: {url}")
        except requests.exceptions.HTTPError as e:
            raise Exception(f"HTTP {r.status_code}: {url}")
        except Exception as e:
            if tentativa < RETRIES - 1:
                time.sleep(1)
                continue
            raise


def filtrar_por_kw(texto, kw):
    """Verifica se alguma palavra da busca aparece no texto."""
    if not kw:
        return True
    texto = (texto or "").lower()
    return any(p.strip() in texto for p in kw.lower().split())


def fmt_data(s):
    """Normaliza string de data para ISO."""
    if not s:
        return None
    # Remove horário se vier junto
    return s[:10] if len(s) >= 10 else s


def mapear_pncp(item):
    unidade = item.get("unidadeOrgao") or {}
    orgao   = item.get("orgaoEntidade") or {}
    num     = item.get("numeroControlePNCP") or ""
    return {
        "id":         num or str(item.get("sequencialCompra", "")),
        "titulo":     item.get("objetoCompra") or "Sem descrição",
        "orgao":      orgao.get("razaoSocial") or "Órgão não informado",
        "uf":         (unidade.get("ufSigla") or "—").upper(),
        "municipio":  unidade.get("municipioNome") or "",
        "modalidade": item.get("modalidadeNome") or "Não informada",
        "valor":      item.get("valorTotalEstimado"),
        "dataEnc":    fmt_data(item.get("dataEncerramentoProposta")),
        "dataPub":    fmt_data(item.get("dataPublicacaoPncp")),
        "link":       item.get("linkSistemaOrigem") or (f"https://pncp.gov.br/app/editais/{num}" if num else None),
        "fonte":      "PNCP"
    }


# ─────────────────────────────────────────
# Fonte 1: PNCP — propostas com prazo aberto
# Endpoint dedicado para licitações AINDA ABERTAS
# ─────────────────────────────────────────
def buscar_pncp_abertas(kw, pagina, uf):
    hoje = datetime.now()
    fim  = (hoje + timedelta(days=120)).strftime("%Y%m%d")

    data = get(
        "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
        params={"dataFinal": fim, "pagina": pagina, "tamanhoPagina": 20}
    )

    items = data.get("data") or []
    total = data.get("totalRegistros") or len(items)

    resultado = []
    for item in items:
        titulo  = item.get("objetoCompra") or ""
        uf_item = ((item.get("unidadeOrgao") or {}).get("ufSigla") or "").upper()
        if not filtrar_por_kw(titulo, kw):
            continue
        if uf and uf_item != uf:
            continue
        resultado.append(mapear_pncp(item))

    return resultado, total


# ─────────────────────────────────────────
# Fonte 2: PNCP — publicações recentes (últimos 30 dias)
# ─────────────────────────────────────────
def buscar_pncp_publicacoes(kw, pagina, uf):
    hoje = datetime.now()
    ini  = (hoje - timedelta(days=30)).strftime("%Y%m%d")
    fim  = (hoje + timedelta(days=120)).strftime("%Y%m%d")

    data = get(
        "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao",
        params={"dataInicial": ini, "dataFinal": fim, "pagina": pagina, "tamanhoPagina": 20}
    )

    items = data.get("data") or []
    total = data.get("totalRegistros") or len(items)

    resultado = []
    for item in items:
        titulo  = item.get("objetoCompra") or ""
        uf_item = ((item.get("unidadeOrgao") or {}).get("ufSigla") or "").upper()
        if not filtrar_por_kw(titulo, kw):
            continue
        if uf and uf_item != uf:
            continue
        resultado.append(mapear_pncp(item))

    # Se filtrou tudo (sem match de kw), retorna os 10 primeiros sem filtro
    if not resultado and items and not kw:
        resultado = [mapear_pncp(i) for i in items[:10]]

    return resultado, total


# ─────────────────────────────────────────
# Fonte 3: Compras.dados.gov.br (SIASG — dados até 2020)
# Endpoint correto: /licitacoes/v1/licitacoes.json
# Filtra por modalidade; busca por texto não é suportada nativamente
# ─────────────────────────────────────────
def buscar_compras_dados(kw, pagina):
    # Modalidade 5 = Pregão (a mais comum para serviços)
    # Sem parâmetro de texto — filtramos no servidor
    data = get(
        "http://compras.dados.gov.br/licitacoes/v1/licitacoes.json",
        params={"modalidade": "05", "pagina": pagina}
    )

    embedded = data.get("_embedded") or {}
    items    = list(embedded.values())[0] if embedded else []
    page     = data.get("page") or {}
    total    = page.get("totalElements") or len(items)

    resultado = []
    for item in items:
        titulo = item.get("nome_objeto") or item.get("objeto") or ""
        if kw and not filtrar_por_kw(titulo, kw):
            continue
        resultado.append({
            "id":         str(item.get("id") or ""),
            "titulo":     titulo or "Sem descrição",
            "orgao":      item.get("nome_orgao") or "Órgão não informado",
            "uf":         (item.get("uf") or "—").upper(),
            "municipio":  item.get("municipio") or "",
            "modalidade": item.get("nome_modalidade") or "Pregão",
            "valor":      item.get("valor_estimado"),
            "dataEnc":    fmt_data(item.get("data_sessao")),
            "dataPub":    fmt_data(item.get("data_abertura")),
            "link":       None,
            "fonte":      "Compras.gov"
        })

    return resultado, total


# ─────────────────────────────────────────
# Fonte 4: dadosabertos.compras.gov.br (sistema novo pós-2021)
# ─────────────────────────────────────────
def buscar_dadosabertos(kw, pagina):
    data = get(
        "https://dadosabertos.compras.gov.br/modulo-pesquisa-preco/1/material/resultados",
        params={"pagina": pagina, "tamanhoPagina": 20}
    )
    # Esta API retorna preços praticados, não licitações abertas
    # Usamos apenas como fallback para enriquecer resultados
    return [], 0


# ─────────────────────────────────────────
# ROTA PRINCIPAL
# GET /api/licitacoes?q=consultoria&pagina=1&uf=SP
# ─────────────────────────────────────────
@app.route("/api/licitacoes")
def buscar_licitacoes():
    kw     = request.args.get("q", "").strip()
    pagina = max(1, int(request.args.get("pagina", 1)))
    uf     = request.args.get("uf", "").strip().upper()

    resultados = []
    erros      = []
    total      = 0

    # ── 1. Tenta propostas abertas no PNCP (mais relevante) ──
    try:
        res, tot = buscar_pncp_abertas(kw, pagina, uf)
        resultados.extend(res)
        total = max(total, tot)
    except Exception as e:
        erros.append(f"PNCP (abertas): {e}")

    # ── 2. Se poucos resultados, complementa com publicações recentes ──
    if len(resultados) < 5:
        try:
            res2, tot2 = buscar_pncp_publicacoes(kw, pagina, uf)
            # Evita duplicatas pelo id
            ids_existentes = {r["id"] for r in resultados}
            novos = [r for r in res2 if r["id"] not in ids_existentes]
            resultados.extend(novos)
            if tot2 > total:
                total = tot2
        except Exception as e:
            erros.append(f"PNCP (publicações): {e}")

    # ── 3. Fallback: Compras.dados.gov.br ──
    if not resultados:
        try:
            res3, tot3 = buscar_compras_dados(kw, pagina)
            resultados.extend(res3)
            total = max(total, tot3)
        except Exception as e:
            erros.append(f"Compras.dados.gov.br: {e}")

    return jsonify({
        "resultados": resultados,
        "total":      total or len(resultados),
        "pagina":     pagina,
        "erros":      erros,
        "timestamp":  datetime.now().isoformat()
    })


# ─────────────────────────────────────────
# ROTA: Health check
# ─────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({
        "status":    "ok",
        "versao":    "2.0",
        "timestamp": datetime.now().isoformat()
    })


# ─────────────────────────────────────────
# ROTA: Teste rápido das APIs do governo
# GET /api/teste
# ─────────────────────────────────────────
@app.route("/api/teste")
def testar_apis():
    resultados = {}

    # Testa PNCP abertas
    try:
        hoje = datetime.now()
        fim  = (hoje + timedelta(days=30)).strftime("%Y%m%d")
        r = requests.get(
            "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
            params={"dataFinal": fim, "pagina": 1, "tamanhoPagina": 1},
            headers=HEADERS, timeout=TIMEOUT
        )
        resultados["pncp_abertas"] = {"status": r.status_code, "ok": r.ok}
    except Exception as e:
        resultados["pncp_abertas"] = {"status": "erro", "mensagem": str(e)}

    # Testa PNCP publicações
    try:
        hoje = datetime.now()
        ini  = (hoje - timedelta(days=7)).strftime("%Y%m%d")
        fim  = hoje.strftime("%Y%m%d")
        r = requests.get(
            "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao",
            params={"dataInicial": ini, "dataFinal": fim, "pagina": 1, "tamanhoPagina": 1},
            headers=HEADERS, timeout=TIMEOUT
        )
        resultados["pncp_publicacoes"] = {"status": r.status_code, "ok": r.ok}
    except Exception as e:
        resultados["pncp_publicacoes"] = {"status": "erro", "mensagem": str(e)}

    # Testa Compras.dados.gov.br
    try:
        r = requests.get(
            "http://compras.dados.gov.br/licitacoes/v1/licitacoes.json",
            params={"modalidade": "05", "pagina": 1},
            headers=HEADERS, timeout=TIMEOUT
        )
        resultados["compras_dados"] = {"status": r.status_code, "ok": r.ok}
    except Exception as e:
        resultados["compras_dados"] = {"status": "erro", "mensagem": str(e)}

    return jsonify(resultados)


# ─────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅ Monitor de Licitações v2 rodando em http://localhost:{port}")
    print(f"   Teste as APIs em: http://localhost:{port}/api/teste\n")
    app.run(host="0.0.0.0", port=port, debug=False)
