"""
Monitor de Licitações Gov — Backend Python v3
- Chamadas paralelas às APIs (mais rápido)
- Timeout agressivo (8s) com fallback imediato
- Compras.dados.gov.br: endpoint e parâmetros corretos
- Filtro de palavras-chave feito no servidor
- Rota /api/teste para diagnóstico
"""
 
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
import os
 
app = Flask(__name__)
CORS(app)
 
TIMEOUT = 8
HEADERS = {"Accept": "application/json", "User-Agent": "MonitorLicitacoesBr/3.0"}
 
 
# ─── helpers ───────────────────────────────────────────
 
def get_json(url, params=None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()
 
def contem_kw(texto, kw):
    if not kw:
        return True
    t = (texto or "").lower()
    return any(p.strip() in t for p in kw.lower().split() if p.strip())
 
def fmt_data(s):
    if not s:
        return None
    return str(s)[:10]
 
def mapear_pncp(item):
    uo  = item.get("unidadeOrgao") or {}
    org = item.get("orgaoEntidade") or {}
    num = item.get("numeroControlePNCP") or ""
    return {
        "id":         num or str(item.get("sequencialCompra", id(item))),
        "titulo":     item.get("objetoCompra") or "Sem descrição",
        "orgao":      org.get("razaoSocial") or "Órgão não informado",
        "uf":         (uo.get("ufSigla") or "—").upper(),
        "municipio":  uo.get("municipioNome") or "",
        "modalidade": item.get("modalidadeNome") or "Não informada",
        "valor":      item.get("valorTotalEstimado"),
        "dataEnc":    fmt_data(item.get("dataEncerramentoProposta")),
        "dataPub":    fmt_data(item.get("dataPublicacaoPncp")),
        "link":       item.get("linkSistemaOrigem")
                      or (f"https://pncp.gov.br/app/editais/{num}" if num else None),
        "fonte":      "PNCP",
    }
 
 
# ─── fontes de dados ────────────────────────────────────
 
def fonte_pncp_proposta(kw, pagina, uf):
    """Licitações com prazo de proposta ainda aberto."""
    hoje = datetime.now()
    fim  = (hoje + timedelta(days=120)).strftime("%Y%m%d")
    data = get_json(
        "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
        {"dataFinal": fim, "pagina": pagina, "tamanhoPagina": 20},
    )
    items = data.get("data") or []
    total = data.get("totalRegistros") or len(items)
    res = []
    for i in items:
        uf_i = ((i.get("unidadeOrgao") or {}).get("ufSigla") or "").upper()
        if contem_kw(i.get("objetoCompra"), kw) and (not uf or uf_i == uf):
            res.append(mapear_pncp(i))
    # sem match de kw: devolve os primeiros mesmo assim (fallback visual)
    if not res and items and not kw:
        res = [mapear_pncp(i) for i in items[:10]]
    return res, total, None
 
 
def fonte_pncp_publicacao(kw, pagina, uf):
    """Publicações recentes (30 dias)."""
    hoje = datetime.now()
    ini  = (hoje - timedelta(days=30)).strftime("%Y%m%d")
    fim  = (hoje + timedelta(days=120)).strftime("%Y%m%d")
    data = get_json(
        "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao",
        {"dataInicial": ini, "dataFinal": fim, "pagina": pagina, "tamanhoPagina": 20},
    )
    items = data.get("data") or []
    total = data.get("totalRegistros") or len(items)
    res = []
    for i in items:
        uf_i = ((i.get("unidadeOrgao") or {}).get("ufSigla") or "").upper()
        if contem_kw(i.get("objetoCompra"), kw) and (not uf or uf_i == uf):
            res.append(mapear_pncp(i))
    return res, total, None
 
 
def fonte_compras_dados(kw, pagina):
    """
    Compras.dados.gov.br (SIASG — dados até 2020).
    Não suporta busca por texto: filtramos no servidor.
    Busca pregões (modalidade 05) e concorrências (01).
    """
    resultados = []
    total = 0
    for modalidade in ["05", "01"]:
        try:
            data = get_json(
                "http://compras.dados.gov.br/licitacoes/v1/licitacoes.json",
                {"modalidade": modalidade, "pagina": pagina},
            )
            embedded = data.get("_embedded") or {}
            items = list(embedded.values())[0] if embedded else []
            total += (data.get("page") or {}).get("totalElements") or len(items)
            for i in items:
                titulo = i.get("nome_objeto") or i.get("objeto") or ""
                if contem_kw(titulo, kw):
                    resultados.append({
                        "id":         str(i.get("id") or ""),
                        "titulo":     titulo or "Sem descrição",
                        "orgao":      i.get("nome_orgao") or "Órgão não informado",
                        "uf":         (i.get("uf") or "—").upper(),
                        "municipio":  "",
                        "modalidade": i.get("nome_modalidade") or "Pregão",
                        "valor":      i.get("valor_estimado"),
                        "dataEnc":    fmt_data(i.get("data_sessao")),
                        "dataPub":    fmt_data(i.get("data_abertura")),
                        "link":       None,
                        "fonte":      "Compras.gov",
                    })
        except Exception:
            pass
    return resultados, total, None
 
 
# ─── rota principal ─────────────────────────────────────
 
@app.route("/api/licitacoes")
def buscar_licitacoes():
    kw     = request.args.get("q", "").strip()
    pagina = max(1, int(request.args.get("pagina", 1)))
    uf     = request.args.get("uf", "").strip().upper()
 
    resultados = []
    erros      = []
    total      = 0
    ids_vistos = set()
 
    # ── dispara as 3 fontes em paralelo ──
    tarefas = {
        "pncp_proposta":   lambda: fonte_pncp_proposta(kw, pagina, uf),
        "pncp_publicacao": lambda: fonte_pncp_publicacao(kw, pagina, uf),
        "compras_dados":   lambda: fonte_compras_dados(kw, pagina),
    }
 
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fn): nome for nome, fn in tarefas.items()}
        for fut in as_completed(futures, timeout=TIMEOUT + 2):
            nome = futures[fut]
            try:
                res, tot, _ = fut.result(timeout=0)
                for item in res:
                    if item["id"] not in ids_vistos:
                        ids_vistos.add(item["id"])
                        resultados.append(item)
                if tot > total:
                    total = tot
            except Exception as e:
                erros.append(f"{nome}: {type(e).__name__}: {str(e)[:120]}")
 
    # ordena: urgentes primeiro (dataEnc mais próxima)
    def sort_key(r):
        d = r.get("dataEnc") or "9999-12-31"
        return d
 
    resultados.sort(key=sort_key)
 
    return jsonify({
        "resultados": resultados,
        "total":      total or len(resultados),
        "pagina":     pagina,
        "erros":      erros,
        "timestamp":  datetime.now().isoformat(),
    })
 
 
# ─── health ─────────────────────────────────────────────
 
@app.route("/health")
def health():
    return jsonify({"status": "ok", "versao": "3.0", "timestamp": datetime.now().isoformat()})
 
 
# ─── diagnóstico ────────────────────────────────────────
 
@app.route("/api/teste")
def testar_apis():
    hoje = datetime.now()
    testes = {
        "pncp_proposta": (
            "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
            {"dataFinal": (hoje + timedelta(days=30)).strftime("%Y%m%d"), "pagina": 1, "tamanhoPagina": 1},
        ),
        "pncp_publicacao": (
            "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao",
            {"dataInicial": (hoje - timedelta(days=7)).strftime("%Y%m%d"),
             "dataFinal": hoje.strftime("%Y%m%d"),
             "pagina": 1, "tamanhoPagina": 1},
        ),
        "compras_dados": (
            "http://compras.dados.gov.br/licitacoes/v1/licitacoes.json",
            {"modalidade": "05", "pagina": 1},
        ),
    }
 
    resultados = {}
    for nome, (url, params) in testes.items():
        inicio = datetime.now()
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            tempo = round((datetime.now() - inicio).total_seconds(), 2)
            resultados[nome] = {
                "ok":     r.ok,
                "status": r.status_code,
                "tempo":  f"{tempo}s",
                "url":    r.url,
            }
        except Exception as e:
            tempo = round((datetime.now() - inicio).total_seconds(), 2)
            resultados[nome] = {
                "ok":      False,
                "erro":    f"{type(e).__name__}: {str(e)[:120]}",
                "tempo":   f"{tempo}s",
            }
 
    return jsonify(resultados)
 
 
# ─── iniciar ────────────────────────────────────────────
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅ Monitor de Licitações v3 — http://localhost:{port}")
    print(f"   Diagnóstico: http://localhost:{port}/api/teste\n")
    app.run(host="0.0.0.0", port=port, debug=False)
