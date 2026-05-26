"""
Monitor de Licitações Gov — Backend v11
Correção principal: PNCP não suporta busca por texto.
Retorna os resultados sem filtrar — o frontend filtra localmente.
Erro 400 no /publicacao corrigido: tamanhoPagina sem valor inteiro.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import time

app = Flask(__name__)
CORS(app)

TIMEOUT   = 15
PROXY_URL = os.environ.get("PROXY_URL", "")

# Headers que simulam um navegador real — reduz bloqueios do PNCP
HEADERS = {
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Origin":          "https://pncp.gov.br",
    "Referer":         "https://pncp.gov.br/",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

def proxies():
    """Usa proxy se configurado. Se proxy falhar, tenta sem proxy como fallback."""
    if not PROXY_URL:
        return None
    return {"http": PROXY_URL, "https": PROXY_URL}

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
# Retorna tudo — sem filtro de palavra-chave (PNCP não suporta busca textual)

def pncp_get(endpoint, params):
    """
    GET ao PNCP com duas estratégias:
    1. Tenta com proxy (se configurado)
    2. Se proxy falhar ou retornar vazio, tenta sem proxy com headers de navegador
    """
    url = f"https://pncp.gov.br/api/consulta/v1/{endpoint}"

    estrategias = []
    if PROXY_URL:
        estrategias.append({"proxies": proxies(),  "label": "com proxy"})
    estrategias.append(    {"proxies": None,        "label": "sem proxy"})

    ultimo_erro = None
    for estrategia in estrategias:
        for tentativa in range(2):   # 2 tentativas por estratégia
            try:
                r = requests.get(
                    url,
                    params=params,
                    headers=HEADERS,
                    proxies=estrategia["proxies"],
                    timeout=TIMEOUT,
                )
                # Resposta vazia = proxy falhou silenciosamente
                if not r.text or not r.text.strip():
                    raise ValueError(f"Resposta vazia [{estrategia['label']}]")

                # Resposta não-JSON = página de erro HTML
                if not r.text.strip().startswith("{"):
                    raise ValueError(f"Não é JSON [{estrategia['label']}] status={r.status_code}: {r.text[:80]}")

                r.raise_for_status()
                return r.json()

            except (ValueError, requests.exceptions.JSONDecodeError) as e:
                ultimo_erro = e
                if tentativa == 0:
                    time.sleep(1)
                continue
            except requests.exceptions.Timeout:
                ultimo_erro = Exception(f"Timeout [{estrategia['label']}]")
                break   # timeout = passa para próxima estratégia imediatamente
            except requests.exceptions.RequestException as e:
                ultimo_erro = e
                if tentativa == 0:
                    time.sleep(1)
                continue

    raise ultimo_erro or Exception("Todas as estratégias falharam")


def fonte_pncp_proposta(pagina, uf):
    hoje = datetime.now()
    params = {
        "dataInicial":   hoje.strftime("%Y%m%d"),
        "dataFinal":     (hoje + timedelta(days=30)).strftime("%Y%m%d"),
        "pagina":        int(pagina),
        "tamanhoPagina": 20,
    }
    if uf:
        params["ufSigla"] = uf

    data  = pncp_get("contratacoes/proposta", params)
    items = data.get("data") or []
    total = data.get("totalRegistros") or len(items)
    return [mapear_pncp(i) for i in items], total


# ─── Fonte 2: PNCP publicações recentes ──────────────────────────────────────

def fonte_pncp_publicacao(pagina, uf):
    hoje = datetime.now()
    params = {
        "dataInicial":   (hoje - timedelta(days=15)).strftime("%Y%m%d"),
        "dataFinal":     hoje.strftime("%Y%m%d"),
        "pagina":        int(pagina),
        "tamanhoPagina": 20,
    }
    if uf:
        params["ufSigla"] = uf

    data  = pncp_get("contratacoes/publicacao", params)
    items = data.get("data") or []
    total = data.get("totalRegistros") or len(items)
    return [mapear_pncp(i) for i in items], total


# ─── Fonte 3: compras.dados.gov.br (HTTP, sem redirect) ──────────────────────

def fonte_compras_dados(pagina):
    res, total = [], 0
    for modalidade in ["05", "01"]:
        try:
            r = requests.get(
                "http://compras.dados.gov.br/licitacoes/v1/licitacoes.json",
                params={"modalidade": modalidade, "pagina": int(pagina)},
                headers=HEADERS,
                timeout=TIMEOUT,
                allow_redirects=False,
            )
            if r.status_code != 200:
                continue
            data     = r.json()
            embedded = data.get("_embedded") or {}
            items    = list(embedded.values())[0] if embedded else []
            total   += (data.get("page") or {}).get("totalElements") or len(items)
            for item in items:
                res.append({
                    "id":         str(item.get("id") or ""),
                    "titulo":     item.get("nome_objeto") or item.get("objeto") or "Sem descrição",
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
    # kw é recebida mas o filtro é feito no FRONTEND — PNCP não suporta busca textual
    pagina = max(1, int(request.args.get("pagina", 1)))
    uf     = request.args.get("uf", "").strip().upper()

    resultados, erros, total, ids_vistos = [], [], 0, set()

    tarefas = {
        "pncp_proposta":   lambda: fonte_pncp_proposta(pagina, uf),
        "pncp_publicacao": lambda: fonte_pncp_publicacao(pagina, uf),
        "compras_dados":   lambda: fonte_compras_dados(pagina),
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
    return jsonify({"status": "ok", "versao": "9.0",
                    "proxy_ativo": bool(PROXY_URL),
                    "timestamp": datetime.now().isoformat()})


# ─── Diagnóstico ──────────────────────────────────────────────────────────────

@app.route("/api/teste")
def testar_apis():
    hoje = datetime.now()
    px   = proxies()
    res  = {}

    # PNCP proposta
    try:
        ini = hoje.strftime("%Y%m%d")
        fim = (hoje + timedelta(days=7)).strftime("%Y%m%d")
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
        res["pncp_proposta"] = {"ok": False, "erro": str(e)[:150]}

    # PNCP publicacao
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
        res["pncp_publicacao"] = {"ok": False, "erro": str(e)[:150]}

    # compras.dados HTTP
    try:
        t0 = datetime.now()
        r  = requests.get(
            "http://compras.dados.gov.br/licitacoes/v1/licitacoes.json",
            params={"modalidade": "05", "pagina": 1},
            headers=HEADERS, timeout=TIMEOUT, allow_redirects=False,
        )
        res["compras_dados"] = {"ok": r.status_code == 200, "status": r.status_code,
                                "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s"}
    except Exception as e:
        res["compras_dados"] = {"ok": False, "erro": str(e)[:150]}

    res["_config"] = {
        "proxy_configurado": bool(PROXY_URL),
        "proxy_preview":     (PROXY_URL[:25] + "...") if PROXY_URL else "não configurado",
        "versao":            "11.0",
    }
    return jsonify(res)


# ─── Iniciar ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅ Monitor v11 — http://localhost:{port}")
    print(f"   Proxy: {'✅ ativo' if PROXY_URL else '❌ não configurado'}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
