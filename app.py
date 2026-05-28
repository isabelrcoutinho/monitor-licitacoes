"""
Monitor de Licitações Gov — Backend v17
Endpoints 100% confirmados e funcionando no Railway:

  1. dadosabertos.compras.gov.br/modulo-pesquisa-preco — busca preços praticados
     por material ou serviço (busca textual real, funciona sem autenticação)

  2. api.portaldatransparencia.gov.br/api-de-dados/licitacoes — licitações
     federais com busca por descrição (requer TRANSPARENCIA_API_KEY gratuita)

  3. pncp.gov.br — bônus, falha silenciosamente se bloqueado pelo Cloudflare

Cadestre sua chave gratuita em:
https://portaldatransparencia.gov.br/api-de-dados/cadastrar-email
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
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
}
TRANSPARENCIA_KEY = os.environ.get("TRANSPARENCIA_API_KEY", "")


def fmt_data(s):
    if not s:
        return None
    s = str(s).strip()
    if len(s) >= 10 and s[2] == "/":
        p = s[:10].split("/")
        return f"{p[2]}-{p[1]}-{p[0]}"
    return s[:10]


def filtrar(texto, kw):
    if not kw or not kw.strip():
        return True
    t = (texto or "").lower()
    palavras = [p for p in kw.lower().split() if len(p) > 2]
    return not palavras or any(p in t for p in palavras)


def safe_get(url, params=None, extra_headers=None):
    h = {**HEADERS, **(extra_headers or {})}
    r = requests.get(url, params=params, headers=h, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ─── Fonte 1: Pesquisa de Preço — Material ────────────────────────────────────
# Retorna materiais com preços praticados, filtráveis por descrição.
# Endpoint confirmado no manual oficial (seção 6).

def fonte_pesquisa_material(kw, pagina):
    data = safe_get(
        "https://dadosabertos.compras.gov.br/modulo-pesquisa-preco/1/material/resultados",
        {"pagina": pagina, "tamanhoPagina": 20, "descricaoItem": kw},
    )
    items = data.get("resultado") or []
    total = data.get("totalRegistros") or len(items)
    res = []
    for item in items:
        desc = (item.get("descricaoItem") or item.get("descricao") or "")
        if not filtrar(desc, kw):
            continue
        res.append({
            "id":         str(item.get("id") or item.get("codigoItem") or id(item)),
            "titulo":     desc or "Sem descrição",
            "orgao":      item.get("nomeOrgao") or item.get("orgao") or "Órgão não informado",
            "uf":         (item.get("uf") or "—").upper(),
            "municipio":  item.get("municipio") or "",
            "modalidade": item.get("modalidade") or "Compra realizada",
            "valor":      item.get("precoUnitario") or item.get("valorTotal"),
            "dataEnc":    fmt_data(item.get("dataCompra") or item.get("data")),
            "dataPub":    fmt_data(item.get("dataCompra") or item.get("data")),
            "link":       "https://www.gov.br/compras/pt-br",
            "fonte":      "Compras.gov",
        })
    return res, total


# ─── Fonte 2: Pesquisa de Preço — Serviço ────────────────────────────────────

def fonte_pesquisa_servico(kw, pagina):
    data = safe_get(
        "https://dadosabertos.compras.gov.br/modulo-pesquisa-preco/1/servico/resultados",
        {"pagina": pagina, "tamanhoPagina": 20, "descricaoServico": kw},
    )
    items = data.get("resultado") or []
    total = data.get("totalRegistros") or len(items)
    res = []
    for item in items:
        desc = (item.get("descricaoServico") or item.get("descricao") or "")
        if not filtrar(desc, kw):
            continue
        res.append({
            "id":         str(item.get("id") or item.get("codigoServico") or id(item)),
            "titulo":     desc or "Sem descrição",
            "orgao":      item.get("nomeOrgao") or item.get("orgao") or "Órgão não informado",
            "uf":         (item.get("uf") or "—").upper(),
            "municipio":  item.get("municipio") or "",
            "modalidade": item.get("modalidade") or "Contratação de serviço",
            "valor":      item.get("precoUnitario") or item.get("valorTotal"),
            "dataEnc":    fmt_data(item.get("dataCompra") or item.get("data")),
            "dataPub":    fmt_data(item.get("dataCompra") or item.get("data")),
            "link":       "https://www.gov.br/compras/pt-br",
            "fonte":      "Compras.gov",
        })
    return res, total


# ─── Fonte 3: Portal da Transparência (se chave configurada) ──────────────────

def fonte_transparencia(kw, pagina):
    hoje = datetime.now()
    params = {
        "dataInicial":   (hoje - timedelta(days=60)).strftime("%d/%m/%Y"),
        "dataFinal":     hoje.strftime("%d/%m/%Y"),
        "pagina":        pagina,
        "tamanhoPagina": 20,
    }
    if kw:
        params["descricao"] = kw

    data = safe_get(
        "https://api.portaldatransparencia.gov.br/api-de-dados/licitacoes",
        params=params,
        extra_headers={"chave-de-acesso": TRANSPARENCIA_KEY},
    )
    items = data if isinstance(data, list) else (data.get("data") or [])
    total = len(items) if isinstance(data, list) else (data.get("totalRegistros") or len(items))

    res = []
    for item in items:
        mod  = item.get("modalidade") or {}
        org  = item.get("orgaoSuperior") or item.get("unidadeGestora") or {}
        titulo = item.get("objeto") or item.get("descricao") or ""
        res.append({
            "id":         str(item.get("id") or ""),
            "titulo":     titulo or "Sem descrição",
            "orgao":      (org.get("descricao") if isinstance(org, dict) else str(org)) or "Órgão não informado",
            "uf":         "—",
            "municipio":  "",
            "modalidade": (mod.get("descricao") if isinstance(mod, dict) else str(mod)) or "Não informada",
            "valor":      item.get("valorLicitacao") or item.get("valor"),
            "dataEnc":    fmt_data(item.get("dataResultadoCompra") or item.get("dataFim")),
            "dataPub":    fmt_data(item.get("dataPublicacao") or item.get("dataAbertura")),
            "link":       "https://portaldatransparencia.gov.br/licitacoes/consulta",
            "fonte":      "Transparência",
        })
    return res, total


# ─── Fonte 4: PNCP (bônus — pode ser bloqueado) ──────────────────────────────

def fonte_pncp(pagina, uf):
    hoje = datetime.now()
    params = {
        "dataInicial":   hoje.strftime("%Y%m%d"),
        "dataFinal":     (hoje + timedelta(days=30)).strftime("%Y%m%d"),
        "pagina":        int(pagina),
        "tamanhoPagina": 20,
    }
    if uf:
        params["ufSigla"] = uf
    r = requests.get(
        "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
        params=params, headers=HEADERS, timeout=10,
    )
    if not r.text or not r.text.strip().startswith("{"):
        raise ValueError(f"PNCP bloqueado (status {r.status_code})")
    r.raise_for_status()
    data  = r.json()
    items = data.get("data") or []
    total = data.get("totalRegistros") or len(items)
    res = []
    for item in items:
        uo  = item.get("unidadeOrgao") or {}
        org = item.get("orgaoEntidade") or {}
        num = item.get("numeroControlePNCP") or ""
        res.append({
            "id":         num or str(item.get("sequencialCompra", id(item))),
            "titulo":     item.get("objetoCompra") or "Sem descrição",
            "orgao":      org.get("razaoSocial") or "Órgão não informado",
            "uf":         (uo.get("ufSigla") or "—").upper(),
            "municipio":  uo.get("municipioNome") or "",
            "modalidade": item.get("modalidadeNome") or "Não informada",
            "valor":      item.get("valorTotalEstimado"),
            "dataEnc":    fmt_data(item.get("dataEncerramentoProposta")),
            "dataPub":    fmt_data(item.get("dataPublicacaoPncp")),
            "link":       item.get("linkSistemaOrigem") or (f"https://pncp.gov.br/app/editais/{num}" if num else None),
            "fonte":      "PNCP",
        })
    return res, total


# ─── Rota principal ───────────────────────────────────────────────────────────

@app.route("/api/licitacoes")
def buscar_licitacoes():
    kw     = request.args.get("q", "").strip()
    pagina = max(1, int(request.args.get("pagina", 1)))
    uf     = request.args.get("uf", "").strip().upper()

    resultados, erros, total, ids_vistos = [], [], 0, set()

    tarefas = {
        "material": lambda: fonte_pesquisa_material(kw, pagina),
        "servico":  lambda: fonte_pesquisa_servico(kw, pagina),
        "pncp":     lambda: fonte_pncp(pagina, uf),
    }
    if TRANSPARENCIA_KEY:
        tarefas["transparencia"] = lambda: fonte_transparencia(kw, pagina)

    with ThreadPoolExecutor(max_workers=4) as ex:
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
                if nome != "pncp":   # erros PNCP são silenciosos
                    erros.append(f"{nome}: {type(e).__name__}: {str(e)[:100]}")

    resultados.sort(key=lambda r: r.get("dataEnc") or "9999-12-31")

    return jsonify({
        "resultados":  resultados,
        "total":       total or len(resultados),
        "pagina":      pagina,
        "erros":       erros,
        "timestamp":   datetime.now().isoformat(),
    })


# ─── Health ───────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({
        "status":        "ok",
        "versao":        "17.0",
        "transparencia": bool(TRANSPARENCIA_KEY),
        "timestamp":     datetime.now().isoformat(),
    })


# ─── Diagnóstico ──────────────────────────────────────────────────────────────

@app.route("/api/teste")
def testar_apis():
    hoje = datetime.now()
    res  = {}

    # Pesquisa de preço material
    try:
        t0 = datetime.now()
        r  = requests.get(
            "https://dadosabertos.compras.gov.br/modulo-pesquisa-preco/1/material/resultados",
            params={"pagina": 1, "tamanhoPagina": 1, "descricaoItem": "caneta"},
            headers=HEADERS, timeout=TIMEOUT,
        )
        res["pesquisa_material"] = {
            "ok": r.ok, "status": r.status_code,
            "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
            "nota": "busca por material com descrição",
        }
    except Exception as e:
        res["pesquisa_material"] = {"ok": False, "erro": str(e)[:100]}

    # Pesquisa de preço serviço
    try:
        t0 = datetime.now()
        r  = requests.get(
            "https://dadosabertos.compras.gov.br/modulo-pesquisa-preco/1/servico/resultados",
            params={"pagina": 1, "tamanhoPagina": 1, "descricaoServico": "consultoria"},
            headers=HEADERS, timeout=TIMEOUT,
        )
        res["pesquisa_servico"] = {
            "ok": r.ok, "status": r.status_code,
            "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
            "nota": "busca por serviço com descrição",
        }
    except Exception as e:
        res["pesquisa_servico"] = {"ok": False, "erro": str(e)[:100]}

    # Portal da Transparência
    if TRANSPARENCIA_KEY:
        try:
            ini = (hoje - timedelta(days=7)).strftime("%d/%m/%Y")
            fim = hoje.strftime("%d/%m/%Y")
            t0  = datetime.now()
            r   = requests.get(
                "https://api.portaldatransparencia.gov.br/api-de-dados/licitacoes",
                params={"dataInicial": ini, "dataFinal": fim, "pagina": 1, "tamanhoPagina": 1},
                headers={**HEADERS, "chave-de-acesso": TRANSPARENCIA_KEY},
                timeout=TIMEOUT,
            )
            res["transparencia"] = {
                "ok": r.ok, "status": r.status_code,
                "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
                "chave": "configurada ✅",
            }
        except Exception as e:
            res["transparencia"] = {"ok": False, "erro": str(e)[:100]}
    else:
        res["transparencia"] = {
            "ok": False,
            "nota": "⚠ configure TRANSPARENCIA_API_KEY — cadastre em portaldatransparencia.gov.br/api-de-dados/cadastrar-email",
        }

    # PNCP
    try:
        ini = hoje.strftime("%Y%m%d")
        fim = (hoje + timedelta(days=7)).strftime("%Y%m%d")
        t0  = datetime.now()
        r   = requests.get(
            "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
            params={"dataInicial": ini, "dataFinal": fim, "pagina": 1, "tamanhoPagina": 1},
            headers=HEADERS, timeout=8,
        )
        ok = r.ok and r.text.strip().startswith("{")
        res["pncp"] = {
            "ok": ok, "status": r.status_code,
            "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
            "nota": "bônus — pode ser bloqueado pelo Cloudflare",
        }
    except Exception as e:
        res["pncp"] = {"ok": False, "erro": str(e)[:80], "nota": "bloqueio esperado"}

    res["_config"] = {
        "versao":        "17.0",
        "transparencia": bool(TRANSPARENCIA_KEY),
        "dica":          "Configure TRANSPARENCIA_API_KEY para licitações federais abertas",
    }
    return jsonify(res)


# ─── Iniciar ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅ Monitor v17 — http://localhost:{port}")
    print(f"   Transparência: {'✅' if TRANSPARENCIA_KEY else '❌ não configurada'}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
