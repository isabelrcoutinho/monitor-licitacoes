"""
Monitor de Licitações Gov — Backend v8
Diagnóstico aprimorado + fallback inteligente:
- Testa proxy com HTTP e HTTPS separadamente
- PNCP via proxy apenas se HTTPS funcionar
- compras.dados.gov.br via HTTP sem redirect
- Datas em formato correto YYYYMMDD
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import os

app = Flask(__name__)
CORS(app)

TIMEOUT   = 12
HEADERS   = {"Accept": "application/json", "User-Agent": "MonitorLicitacoesBr/8.0"}
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
    # Formato YYYYMMDD sem separadores — obrigatório no PNCP
    ini  = hoje.strftime("%Y%m%d")
    fim  = (hoje + timedelta(days=30)).strftime("%Y%m%d")

    r = requests.get(
        "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
        params={"dataInicial": ini, "dataFinal": fim,
                "pagina": pagina, "tamanhoPagina": 20},
        headers=HEADERS,
        proxies=proxies(),
        timeout=TIMEOUT,
        verify=True,
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


# ─── Fonte 2: PNCP publicações recentes ──────────────────────────────────────

def fonte_pncp_publicacao(kw, pagina, uf):
    hoje = datetime.now()
    ini  = (hoje - timedelta(days=20)).strftime("%Y%m%d")
    fim  = hoje.strftime("%Y%m%d")

    r = requests.get(
        "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao",
        params={"dataInicial": ini, "dataFinal": fim,
                "pagina": pagina, "tamanhoPagina": 20},
        headers=HEADERS,
        proxies=proxies(),
        timeout=TIMEOUT,
        verify=True,
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


# ─── Fonte 3: compras.dados.gov.br (HTTP puro, sem seguir redirect) ───────────

def fonte_compras_dados(kw, pagina):
    res   = []
    total = 0

    for modalidade in ["05", "01", "08"]:
        try:
            r = requests.get(
                "http://compras.dados.gov.br/licitacoes/v1/licitacoes.json",
                params={"modalidade": modalidade, "pagina": pagina},
                headers=HEADERS,
                timeout=TIMEOUT,
                allow_redirects=False,   # não segue redirect para HTTPS
            )
            if r.status_code != 200:
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
    return jsonify({"status": "ok", "versao": "8.0",
                    "proxy_ativo": bool(PROXY_URL),
                    "timestamp": datetime.now().isoformat()})


# ─── Diagnóstico DETALHADO ────────────────────────────────────────────────────
# Esta rota testa proxy com HTTP e HTTPS separadamente
# para identificar se o Webshare gratuito suporta HTTPS

@app.route("/api/teste")
def testar_apis():
    hoje = datetime.now()
    px   = proxies()
    res  = {}

    # ── Teste 1: proxy com HTTP simples (deve funcionar no plano grátis)
    try:
        t0 = datetime.now()
        r  = requests.get("http://httpbin.org/ip",
                          proxies=px, timeout=8)
        res["proxy_http"] = {
            "ok":    r.ok,
            "ip":    r.json().get("origin", "?") if r.ok else "—",
            "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
            "nota":  "IP que o servidor externo vê (deve ser IP do proxy)"
        }
    except Exception as e:
        res["proxy_http"] = {"ok": False, "erro": str(e)[:120]}

    # ── Teste 2: proxy com HTTPS (falha no plano grátis do Webshare)
    try:
        t0 = datetime.now()
        r  = requests.get("https://httpbin.org/ip",
                          proxies=px, timeout=8, verify=True)
        res["proxy_https"] = {
            "ok":    r.ok,
            "ip":    r.json().get("origin", "?") if r.ok else "—",
            "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
            "nota":  "HTTPS via proxy — necessário para PNCP"
        }
    except Exception as e:
        res["proxy_https"] = {
            "ok":   False,
            "erro": str(e)[:120],
            "nota": "❌ proxy não suporta HTTPS — upgrade necessário no Webshare"
        }

    # ── Teste 3: PNCP direto (sem proxy) para ver se ainda bloqueia
    try:
        ini = hoje.strftime("%Y%m%d")
        fim = (hoje + timedelta(days=7)).strftime("%Y%m%d")
        t0  = datetime.now()
        r   = requests.get(
            "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
            params={"dataInicial": ini, "dataFinal": fim,
                    "pagina": 1, "tamanhoPagina": 1},
            headers=HEADERS, timeout=8,
        )
        res["pncp_sem_proxy"] = {
            "ok": r.ok, "status": r.status_code,
            "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
        }
    except Exception as e:
        res["pncp_sem_proxy"] = {"ok": False, "erro": str(e)[:120]}

    # ── Teste 4: PNCP com proxy
    try:
        ini = hoje.strftime("%Y%m%d")
        fim = (hoje + timedelta(days=7)).strftime("%Y%m%d")
        t0  = datetime.now()
        r   = requests.get(
            "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
            params={"dataInicial": ini, "dataFinal": fim,
                    "pagina": 1, "tamanhoPagina": 1},
            headers=HEADERS, proxies=px, timeout=8,
        )
        res["pncp_com_proxy"] = {
            "ok": r.ok, "status": r.status_code,
            "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
        }
    except Exception as e:
        res["pncp_com_proxy"] = {"ok": False, "erro": str(e)[:120]}

    # ── Teste 5: compras.dados.gov.br HTTP sem redirect
    try:
        t0 = datetime.now()
        r  = requests.get(
            "http://compras.dados.gov.br/licitacoes/v1/licitacoes.json",
            params={"modalidade": "05", "pagina": 1},
            headers=HEADERS, timeout=8, allow_redirects=False,
        )
        res["compras_dados"] = {
            "ok":     r.status_code == 200,
            "status": r.status_code,
            "tempo":  f"{(datetime.now()-t0).total_seconds():.1f}s",
        }
    except Exception as e:
        res["compras_dados"] = {"ok": False, "erro": str(e)[:120]}

    res["_config"] = {
        "proxy_configurado": bool(PROXY_URL),
        "proxy_preview":     (PROXY_URL[:30] + "...") if PROXY_URL else "não configurado",
        "versao":            "8.0",
    }

    return jsonify(res)


# ─── Iniciar ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅ Monitor v8 — http://localhost:{port}")
    print(f"   Proxy: {'✅ ativo' if PROXY_URL else '❌ não configurado'}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
