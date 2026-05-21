"""
Monitor de Licitações Gov — Backend v5
Mesma lógica da versão original, mas com proxy residencial
para contornar o bloqueio do PNCP a IPs de nuvem (Railway/Render).
 
Proxy configurado via variável de ambiente PROXY_URL no Railway.
Se não houver proxy, tenta direto (funciona para compras.dados.gov.br).
"""
 
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
 
app = Flask(__name__)
CORS(app)
 
TIMEOUT = 12
HEADERS = {"Accept": "application/json", "User-Agent": "MonitorLicitacoesBr/5.0"}
 
# ── Proxy residencial (Webshare ou similar)
# Formato esperado: http://usuario:senha@ip:porta
# Ex: http://user123:pass456@proxy.webshare.io:80
PROXY_URL = os.environ.get("PROXY_URL", "")
 
def get_proxies():
    if not PROXY_URL:
        return None
    return {"http": PROXY_URL, "https": PROXY_URL}
 
 
# ─── helpers ──────────────────────────────────────────────────────────────────
 
def get_json(url, params=None):
    return requests.get(
        url,
        params=params,
        headers=HEADERS,
        proxies=get_proxies(),
        timeout=TIMEOUT
    ).json()
 
def get_json_sem_proxy(url, params=None):
    """Algumas APIs não precisam de proxy (compras.dados.gov.br)."""
    return requests.get(
        url,
        params=params,
        headers=HEADERS,
        timeout=TIMEOUT
    ).json()
 
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
 
 
# ─── Fonte 1: PNCP propostas abertas (via proxy) ──────────────────────────────
 
def fonte_pncp_proposta(kw, pagina, uf):
    hoje = datetime.now()
    fim  = (hoje + timedelta(days=120)).strftime("%Y%m%d")
 
    r = requests.get(
        "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
        params={"dataFinal": fim, "pagina": pagina, "tamanhoPagina": 20},
        headers=HEADERS,
        proxies=get_proxies(),
        timeout=TIMEOUT
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
 
    if not res and items and not kw:
        res = [mapear_pncp(i) for i in items[:10]]
 
    return res, total
 
 
# ─── Fonte 2: PNCP publicações recentes (via proxy) ───────────────────────────
 
def fonte_pncp_publicacao(kw, pagina, uf):
    hoje = datetime.now()
    ini  = (hoje - timedelta(days=30)).strftime("%Y%m%d")
    fim  = (hoje + timedelta(days=120)).strftime("%Y%m%d")
 
    r = requests.get(
        "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao",
        params={"dataInicial": ini, "dataFinal": fim,
                "pagina": pagina, "tamanhoPagina": 20},
        headers=HEADERS,
        proxies=get_proxies(),
        timeout=TIMEOUT
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
 
 
# ─── Fonte 3: compras.dados.gov.br (HTTP — sem proxy necessário) ──────────────
 
def fonte_compras_dados(kw, pagina):
    res   = []
    total = 0
 
    for modalidade in ["05", "01", "08"]:
        try:
            # Deve ser HTTP (não HTTPS) — o servidor não suporta HTTPS
            r = requests.get(
                "http://compras.dados.gov.br/licitacoes/v1/licitacoes.json",
                params={"modalidade": modalidade, "pagina": pagina},
                headers=HEADERS,
                timeout=TIMEOUT
            )
            r.raise_for_status()
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
 
    resultados = []
    erros      = []
    total      = 0
    ids_vistos = set()
 
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
        "resultados":      resultados,
        "total":           total or len(resultados),
        "pagina":          pagina,
        "erros":           erros,
        "proxy_ativo":     bool(PROXY_URL),
        "timestamp":       datetime.now().isoformat(),
    })
 
 
# ─── Health ───────────────────────────────────────────────────────────────────
 
@app.route("/health")
def health():
    return jsonify({
        "status":      "ok",
        "versao":      "5.0",
        "proxy_ativo": bool(PROXY_URL),
        "timestamp":   datetime.now().isoformat(),
    })
 
 
# ─── Diagnóstico ──────────────────────────────────────────────────────────────
 
@app.route("/api/teste")
def testar_apis():
    hoje     = datetime.now()
    proxies  = get_proxies()
    ini_pncp = (hoje - timedelta(days=3)).strftime("%Y%m%d")
    fim_pncp = (hoje + timedelta(days=30)).strftime("%Y%m%d")
    result   = {}
 
    testes = {
        "pncp_proposta": {
            "url":    "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
            "params": {"dataFinal": fim_pncp, "pagina": 1, "tamanhoPagina": 1},
            "proxy":  True,
        },
        "pncp_publicacao": {
            "url":    "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao",
            "params": {"dataInicial": ini_pncp, "dataFinal": fim_pncp,
                       "pagina": 1, "tamanhoPagina": 1},
            "proxy":  True,
        },
        "compras_dados": {
            "url":    "http://compras.dados.gov.br/licitacoes/v1/licitacoes.json",
            "params": {"modalidade": "05", "pagina": 1},
            "proxy":  False,  # não precisa de proxy
        },
    }
 
    for nome, cfg in testes.items():
        t0 = datetime.now()
        try:
            r = requests.get(
                cfg["url"],
                params=cfg["params"],
                headers=HEADERS,
                proxies=proxies if cfg["proxy"] else None,
                timeout=TIMEOUT
            )
            result[nome] = {
                "ok":     r.ok,
                "status": r.status_code,
                "tempo":  f"{(datetime.now()-t0).total_seconds():.1f}s",
                "proxy":  "sim" if (cfg["proxy"] and proxies) else "não",
            }
        except Exception as e:
            result[nome] = {
                "ok":    False,
                "erro":  str(e)[:150],
                "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
                "proxy": "sim" if (cfg["proxy"] and proxies) else "não",
            }
 
    result["_config"] = {
        "proxy_configurado": bool(PROXY_URL),
        "proxy_url_preview": (PROXY_URL[:20] + "...") if PROXY_URL else "não configurado",
    }
 
    return jsonify(result)
 
 
# ─── Iniciar ──────────────────────────────────────────────────────────────────
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅ Monitor de Licitações v5 — http://localhost:{port}")
    print(f"   Proxy: {'✅ ' + PROXY_URL[:30] if PROXY_URL else '❌ não configurado'}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
