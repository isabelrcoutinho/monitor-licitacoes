"""
Monitor de Licitações Gov — Backend v7
Correções:
- PNCP /proposta: adicionado dataInicial (obrigatório)
- PNCP /publicacao: janela de 30 dias (máximo aceito)
- compras.dados.gov.br: HTTP forçado via allow_redirects=False
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from requests.adapters import HTTPAdapter
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import os

app = Flask(__name__)
CORS(app)

TIMEOUT   = 12
HEADERS   = {"Accept": "application/json", "User-Agent": "MonitorLicitacoesBr/7.0"}
PROXY_URL = os.environ.get("PROXY_URL", "")

def proxies():
    return {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

def contem_kw(texto, kw):
    if not kw:
        return True
    t = (texto or "").lower()
    return any(p.strip() in t for p in kw.lower().split() if p.strip())

def fmt_data(s):
    return str(s)[:10] if s else None

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


# ─── Fonte 1: PNCP propostas abertas ─────────────────────────────────────────

def fonte_pncp_proposta(kw, pagina, uf):
    hoje = datetime.now()
    ini  = hoje.strftime("%Y%m%d")                          # dataInicial = hoje
    fim  = (hoje + timedelta(days=30)).strftime("%Y%m%d")   # dataFinal = hoje + 30 dias

    r = requests.get(
        "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
        params={"dataInicial": ini, "dataFinal": fim,
                "pagina": pagina, "tamanhoPagina": 20},
        headers=HEADERS, proxies=proxies(), timeout=TIMEOUT,
    )
    r.raise_for_status()
    data  = r.json()
    items = data.get("data") or []
    total = data.get("totalRegistros") or len(items)

    res = []
    for i in items:
        uf_i = ((i.get("unidadeOrgao") or {}).get("ufSigla") or "").upper()
        if contem_kw(i.get("objetoCompra"), kw) and (not uf or uf_i == uf):
            res.append(mapear_pncp(i))

    # fallback: se nenhum match de kw, devolve os 10 primeiros
    if not res and items and not kw:
        res = [mapear_pncp(i) for i in items[:10]]

    return res, total


# ─── Fonte 2: PNCP publicações recentes ──────────────────────────────────────

def fonte_pncp_publicacao(kw, pagina, uf):
    hoje = datetime.now()
    ini  = (hoje - timedelta(days=20)).strftime("%Y%m%d")   # últimos 20 dias
    fim  = hoje.strftime("%Y%m%d")                          # até hoje

    r = requests.get(
        "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao",
        params={"dataInicial": ini, "dataFinal": fim,
                "pagina": pagina, "tamanhoPagina": 20},
        headers=HEADERS, proxies=proxies(), timeout=TIMEOUT,
    )
    r.raise_for_status()
    data  = r.json()
    items = data.get("data") or []
    total = data.get("totalRegistros") or len(items)

    res = []
    for i in items:
        uf_i = ((i.get("unidadeOrgao") or {}).get("ufSigla") or "").upper()
        if contem_kw(i.get("objetoCompra"), kw) and (not uf or uf_i == uf):
            res.append(mapear_pncp(i))

    return res, total


# ─── Fonte 3: compras.dados.gov.br (HTTP puro, sem redirect) ─────────────────

def fonte_compras_dados(kw, pagina):
    res   = []
    total = 0

    session = requests.Session()
    # força HTTP — não segue redirect para HTTPS
    session.max_redirects = 0

    for modalidade in ["05", "01", "08"]:
        try:
            r = session.get(
                "http://compras.dados.gov.br/licitacoes/v1/licitacoes.json",
                params={"modalidade": modalidade, "pagina": pagina},
                headers=HEADERS, timeout=TIMEOUT, allow_redirects=False,
            )
            if r.status_code not in (200, 201):
                continue
            data     = r.json()
            embedded = data.get("_embedded") or {}
            items    = list(embedded.values())[0] if embedded else []
            total   += (data.get("page") or {}).get("totalElements") or len(items)

            for item in items:
                titulo = item.get("nome_objeto") or item.get("objeto") or ""
                if not contem_kw(titulo, kw):
                    continue
                res.append({
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
                    "fonte":      "Compras.gov",
                })
        except Exception:
            pass

    return res, total


# ─── Rota principal ───────────────────────────────────────────────────────────

@app.route("/api/licitacoes")
def buscar_licitacoes():
    kw     = request.args.get("q", "").strip()
    pagina = max(1, int(request.args.get("pagina", 1)))
    uf     = request.args.get("uf", "").strip().upper()

    resultados, erros, total, ids_vistos = [], [], 0, set()

    tarefas = {
        "pncp_proposta":   lambda: fonte_pncp_proposta(kw, pagina, uf),
        "pncp_publicacao": lambda: fonte_pncp_publicacao(kw, pagina, uf),
        "compras_dados":   lambda: fonte_compras_dados(kw, pagina),
    }

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fn): nome for nome, fn in tarefas.items()}
        for fut in as_completed(futures, timeout=TIMEOUT + 3):
            nome = futures[fut]
            try:
                res, tot = fut.result(timeout=0)
                for item in res:
                    uid = item["id"] or f"{nome}-{item['titulo'][:30]}"
                    if uid not in ids_vistos:
                        ids_vistos.add(uid)
                        resultados.append(item)
                if tot > total:
                    total = tot
            except Exception as e:
                erros.append(f"{nome}: {type(e).__name__}: {str(e)[:150]}")

    resultados.sort(key=lambda r: r.get("dataEnc") or "9999-12-31")

    return jsonify({
        "resultados":  resultados,
        "total":       total or len(resultados),
        "pagina":      pagina,
        "erros":       erros,
        "proxy_ativo": bool(PROXY_URL),
        "timestamp":   datetime.now().isoformat(),
    })


# ─── Health ───────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "versao": "7.0",
                    "proxy_ativo": bool(PROXY_URL),
                    "timestamp": datetime.now().isoformat()})


# ─── Diagnóstico ──────────────────────────────────────────────────────────────

@app.route("/api/teste")
def testar_apis():
    hoje = datetime.now()
    px   = proxies()
    res  = {}

    # PNCP proposta (com dataInicial e dataFinal)
    try:
        ini = hoje.strftime("%Y%m%d")
        fim = (hoje + timedelta(days=30)).strftime("%Y%m%d")
        t0  = datetime.now()
        r   = requests.get(
            "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
            params={"dataInicial": ini, "dataFinal": fim,
                    "pagina": 1, "tamanhoPagina": 1},
            headers=HEADERS, proxies=px, timeout=TIMEOUT,
        )
        res["pncp_proposta"] = {"ok": r.ok, "status": r.status_code,
                                "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
                                "proxy": "sim" if px else "não"}
    except Exception as e:
        res["pncp_proposta"] = {"ok": False, "erro": str(e)[:150],
                                "proxy": "sim" if px else "não"}

    # PNCP publicacao (últimos 7 dias)
    try:
        ini = (hoje - timedelta(days=7)).strftime("%Y%m%d")
        fim = hoje.strftime("%Y%m%d")
        t0  = datetime.now()
        r   = requests.get(
            "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao",
            params={"dataInicial": ini, "dataFinal": fim,
                    "pagina": 1, "tamanhoPagina": 1},
            headers=HEADERS, proxies=px, timeout=TIMEOUT,
        )
        res["pncp_publicacao"] = {"ok": r.ok, "status": r.status_code,
                                  "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
                                  "proxy": "sim" if px else "não"}
    except Exception as e:
        res["pncp_publicacao"] = {"ok": False, "erro": str(e)[:150],
                                  "proxy": "sim" if px else "não"}

    # compras.dados.gov.br HTTP
    try:
        t0 = datetime.now()
        r  = requests.get(
            "http://compras.dados.gov.br/licitacoes/v1/licitacoes.json",
            params={"modalidade": "05", "pagina": 1},
            headers=HEADERS, timeout=TIMEOUT, allow_redirects=False,
        )
        res["compras_dados"] = {"ok": r.status_code == 200,
                                "status": r.status_code,
                                "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
                                "proxy": "não (HTTP direto)"}
    except Exception as e:
        res["compras_dados"] = {"ok": False, "erro": str(e)[:150]}

    res["_config"] = {
        "proxy_configurado": bool(PROXY_URL),
        "proxy_preview":     (PROXY_URL[:25] + "...") if PROXY_URL else "não configurado",
        "versao":            "7.0",
    }
    return jsonify(res)


# ─── Iniciar ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅ Monitor v7 — http://localhost:{port}")
    print(f"   Proxy: {'✅ ativo' if PROXY_URL else '❌ não configurado'}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
