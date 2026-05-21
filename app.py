"""
Monitor de Licitações Gov — Backend Python
Proxy para PNCP e Compras.gov.br com cache e filtros por palavra-chave

Deploy gratuito: Railway.app ou Render.com
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
import os

app = Flask(__name__)
CORS(app)  # Permite que o HTML acesse este servidor de qualquer domínio

# ── Configurações ──
PNCP_BASE     = "https://pncp.gov.br/api/consulta/v1"
COMPRAS_BASE  = "https://compras.dados.gov.br"
TIMEOUT       = 15  # segundos de espera para cada chamada às APIs do governo

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "MonitorLicitacoes/1.0 (contato@suaempresa.com.br)"
}


# ════════════════════════════════════════════════════
# ROTA PRINCIPAL — Busca de licitações
# GET /api/licitacoes?q=consultoria&pagina=1&uf=SP
# ════════════════════════════════════════════════════
@app.route("/api/licitacoes")
def buscar_licitacoes():
    kw     = request.args.get("q", "consultoria").strip()
    pagina = int(request.args.get("pagina", 1))
    uf     = request.args.get("uf", "").strip().upper()

    resultados = []
    erros      = []
    total      = 0

    # ── 1. Tenta PNCP (dados mais recentes, desde 2021) ──
    try:
        hoje = datetime.now()
        ini  = (hoje - timedelta(days=30)).strftime("%Y%m%d")
        fim  = (hoje + timedelta(days=120)).strftime("%Y%m%d")

        url = (
            f"{PNCP_BASE}/contratacoes/publicacao"
            f"?dataInicial={ini}&dataFinal={fim}"
            f"&pagina={pagina}&tamanhoPagina=20"
        )

        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        items = data.get("data", []) or []
        total = data.get("totalRegistros", len(items))

        kw_lower = kw.lower()
        for item in items:
            objeto = (item.get("objetoCompra") or "").lower()
            uf_item = (
                (item.get("unidadeOrgao") or {}).get("ufSigla") or ""
            ).upper()

            # Filtro por palavra-chave (qualquer palavra)
            palavras = kw_lower.split()
            match_kw = any(p in objeto for p in palavras)

            # Filtro por UF (opcional)
            match_uf = (not uf) or (uf_item == uf)

            if match_kw and match_uf:
                resultados.append({
                    "id":         item.get("numeroControlePNCP") or item.get("sequencialCompra"),
                    "titulo":     item.get("objetoCompra") or "Sem descrição",
                    "orgao":      (item.get("orgaoEntidade") or {}).get("razaoSocial") or "Órgão não informado",
                    "uf":         uf_item or "—",
                    "modalidade": item.get("modalidadeNome") or "Não informada",
                    "valor":      item.get("valorTotalEstimado"),
                    "dataEnc":    item.get("dataEncerramentoProposta"),
                    "dataPub":    item.get("dataPublicacaoPncp"),
                    "link":       item.get("linkSistemaOrigem") or _link_pncp(item),
                    "fonte":      "PNCP"
                })

        # Se nenhum resultado filtrado mas há dados, pega os primeiros 10 como fallback
        if not resultados and items:
            resultados = [_mapear_pncp(i) for i in items[:10]]

    except Exception as e:
        erros.append(f"PNCP: {str(e)}")

    # ── 2. Fallback: Compras.dados.gov.br (dados históricos mais ricos) ──
    if not resultados:
        try:
            url = (
                f"{COMPRAS_BASE}/licitacoes/v1/licitacoes.json"
                f"?nome_objeto={requests.utils.quote(kw)}&pagina={pagina}"
            )
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

            embedded = data.get("_embedded", {})
            items    = list(embedded.values())[0] if embedded else []
            total    = (data.get("page") or {}).get("totalElements", len(items))

            for item in items:
                uf_item = (item.get("uf") or "").upper()
                if uf and uf_item != uf:
                    continue
                resultados.append({
                    "id":         item.get("id"),
                    "titulo":     item.get("nome_objeto") or item.get("objeto") or "Sem descrição",
                    "orgao":      item.get("nome_orgao") or "Órgão não informado",
                    "uf":         uf_item or "—",
                    "modalidade": item.get("nome_modalidade") or "Não informada",
                    "valor":      item.get("valor_estimado"),
                    "dataEnc":    item.get("data_sessao"),
                    "dataPub":    item.get("data_abertura"),
                    "link":       None,
                    "fonte":      "Compras.gov"
                })

        except Exception as e:
            erros.append(f"Compras.gov: {str(e)}")

    return jsonify({
        "resultados": resultados,
        "total":      total,
        "pagina":     pagina,
        "erros":      erros,
        "timestamp":  datetime.now().isoformat()
    })


# ════════════════════════════════════════════════════
# ROTA — Propostas abertas (propostas com prazo vigente)
# GET /api/abertas?q=consultoria
# ════════════════════════════════════════════════════
@app.route("/api/abertas")
def propostas_abertas():
    kw     = request.args.get("q", "consultoria").strip()
    pagina = int(request.args.get("pagina", 1))

    try:
        url = (
            f"{PNCP_BASE}/contratacoes/proposta"
            f"?pagina={pagina}&tamanhoPagina=20"
        )
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        items = data.get("data", []) or []
        total = data.get("totalRegistros", len(items))

        kw_lower  = kw.lower()
        palavras  = kw_lower.split()
        resultado = []

        for item in items:
            objeto = (item.get("objetoCompra") or "").lower()
            if any(p in objeto for p in palavras):
                resultado.append(_mapear_pncp(item))

        if not resultado and items:
            resultado = [_mapear_pncp(i) for i in items[:10]]

        return jsonify({
            "resultados": resultado,
            "total":      total,
            "pagina":     pagina,
            "timestamp":  datetime.now().isoformat()
        })

    except Exception as e:
        return jsonify({"erro": str(e), "resultados": [], "total": 0}), 500


# ════════════════════════════════════════════════════
# ROTA — Health check (verifica se o servidor está vivo)
# GET /health
# ════════════════════════════════════════════════════
@app.route("/health")
def health():
    return jsonify({
        "status":    "ok",
        "timestamp": datetime.now().isoformat(),
        "versao":    "1.0"
    })


# ── Helpers ──
def _link_pncp(item):
    num = item.get("numeroControlePNCP")
    if num:
        return f"https://pncp.gov.br/app/editais/{num}"
    return None

def _mapear_pncp(item):
    uf = (item.get("unidadeOrgao") or {}).get("ufSigla") or "—"
    return {
        "id":         item.get("numeroControlePNCP") or item.get("sequencialCompra"),
        "titulo":     item.get("objetoCompra") or "Sem descrição",
        "orgao":      (item.get("orgaoEntidade") or {}).get("razaoSocial") or "Órgão não informado",
        "uf":         uf.upper(),
        "modalidade": item.get("modalidadeNome") or "Não informada",
        "valor":      item.get("valorTotalEstimado"),
        "dataEnc":    item.get("dataEncerramentoProposta"),
        "dataPub":    item.get("dataPublicacaoPncp"),
        "link":       _link_pncp(item),
        "fonte":      "PNCP"
    }


# ── Iniciar servidor ──
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅ Monitor de Licitações rodando em http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
