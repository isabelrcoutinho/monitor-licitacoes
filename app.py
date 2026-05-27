"""
Monitor de Licitações Gov — Backend v15
Fontes via dadosabertos.compras.gov.br (funciona no Railway):
  1. Módulo Legado — Pregão (dados históricos + recentes)
  2. Módulo Contratações PNCP 14133 — contratações pela nova lei
  3. PNCP direto — bônus se não bloqueado

Filtro por palavra-chave feito no servidor após buscar os dados.
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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
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
    if not palavras:
        return True
    return any(p in t for p in palavras)


def safe_get(url, params=None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ─── Fonte 1: Contratações PNCP 14133 via dadosabertos ───────────────────────

def fonte_contratacoes_pncp(kw, pagina, uf):
    hoje = datetime.now()
    params = {
        "pagina": pagina,
        "tamanhoPagina": 20,
        "dataPublicacaoInicio": (hoje - timedelta(days=30)).strftime("%Y-%m-%d"),
        "dataPublicacaoFim": hoje.strftime("%Y-%m-%d"),
    }
    if uf:
        params["ufSigla"] = uf

    data  = safe_get(
        "https://dadosabertos.compras.gov.br/modulo-contratacao/1_consultarContratacao",
        params
    )
    items = data.get("resultado") or data.get("data") or []
    total = data.get("totalRegistros") or len(items)

    res = []
    for item in items:
        titulo = (item.get("objetoCompra") or item.get("objeto") or
                  item.get("descricao") or "")
        if not filtrar(titulo, kw):
            continue
        num = item.get("numeroControlePNCP") or ""
        res.append({
            "id":         num or str(item.get("id", id(item))),
            "titulo":     titulo or "Sem descrição",
            "orgao":      (item.get("orgaoEntidade") or {}).get("razaoSocial")
                          or item.get("nomeOrgao") or "Órgão não informado",
            "uf":         (item.get("ufSigla") or item.get("uf") or "—").upper(),
            "municipio":  item.get("municipioNome") or "",
            "modalidade": item.get("modalidadeNome") or item.get("modalidade") or "Não informada",
            "valor":      item.get("valorTotalEstimado") or item.get("valor"),
            "dataEnc":    fmt_data(item.get("dataEncerramentoProposta")),
            "dataPub":    fmt_data(item.get("dataPublicacaoPncp") or item.get("dataPublicacao")),
            "link":       item.get("linkSistemaOrigem")
                          or (f"https://pncp.gov.br/app/editais/{num}" if num else None),
            "fonte":      "Compras.gov",
        })
    return res, total


# ─── Fonte 2: Pregão (módulo legado) via dadosabertos ────────────────────────

def fonte_pregao_legado(kw, pagina):
    hoje = datetime.now()
    params = {
        "pagina": pagina,
        "tamanhoPagina": 20,
        "dataAberturaInicio": (hoje - timedelta(days=20)).strftime("%Y-%m-%d"),
        "dataAberturaFim":    (hoje + timedelta(days=30)).strftime("%Y-%m-%d"),
    }

    data  = safe_get(
        "https://dadosabertos.compras.gov.br/modulo-pregao/1_consultarPregao",
        params
    )
    items = data.get("resultado") or data.get("data") or []
    total = data.get("totalRegistros") or len(items)

    res = []
    for item in items:
        titulo = item.get("objetoPregao") or item.get("objeto") or ""
        if not filtrar(titulo, kw):
            continue
        res.append({
            "id":         str(item.get("numeroAviso") or item.get("id") or id(item)),
            "titulo":     titulo or "Sem descrição",
            "orgao":      item.get("nomeOrgao") or item.get("orgao") or "Órgão não informado",
            "uf":         (item.get("uf") or "—").upper(),
            "municipio":  item.get("municipio") or "",
            "modalidade": "Pregão Eletrônico",
            "valor":      item.get("valorEstimado") or item.get("valor"),
            "dataEnc":    fmt_data(item.get("dataAbertura") or item.get("dataEncerramentoProposta")),
            "dataPub":    fmt_data(item.get("dataPublicacao")),
            "link":       item.get("link") or "https://www.gov.br/compras/pt-br",
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

    r = requests.get(
        "https://api.portaldatransparencia.gov.br/api-de-dados/licitacoes",
        params=params,
        headers={**HEADERS, "chave-de-acesso": TRANSPARENCIA_KEY},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data  = r.json()
    items = data if isinstance(data, list) else (data.get("data") or [])
    total = len(items) if isinstance(data, list) else (data.get("totalRegistros") or len(items))

    res = []
    for item in items:
        modalidade = item.get("modalidade") or {}
        orgao      = item.get("orgaoSuperior") or item.get("unidadeGestora") or {}
        titulo     = item.get("objeto") or item.get("descricao") or ""
        res.append({
            "id":         str(item.get("id") or ""),
            "titulo":     titulo or "Sem descrição",
            "orgao":      (orgao.get("descricao") if isinstance(orgao, dict) else str(orgao)) or "Órgão não informado",
            "uf":         "—",
            "municipio":  "",
            "modalidade": (modalidade.get("descricao") if isinstance(modalidade, dict) else str(modalidade)) or "Não informada",
            "valor":      item.get("valorLicitacao") or item.get("valor"),
            "dataEnc":    fmt_data(item.get("dataResultadoCompra") or item.get("dataFim")),
            "dataPub":    fmt_data(item.get("dataPublicacao") or item.get("dataAbertura")),
            "link":       "https://portaldatransparencia.gov.br/licitacoes/consulta",
            "fonte":      "Transparência",
        })
    return res, total


# ─── Fonte 4: PNCP direto (bônus — pode ser bloqueado) ───────────────────────

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
        "contratacoes": lambda: fonte_contratacoes_pncp(kw, pagina, uf),
        "pregao":       lambda: fonte_pregao_legado(kw, pagina),
        "pncp":         lambda: fonte_pncp(pagina, uf),
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
                # Erros do PNCP são esperados — não mostrar no frontend
                if nome != "pncp":
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
        "versao":        "15.0",
        "transparencia": bool(TRANSPARENCIA_KEY),
        "timestamp":     datetime.now().isoformat(),
    })


# ─── Diagnóstico ──────────────────────────────────────────────────────────────

@app.route("/api/teste")
def testar_apis():
    hoje = datetime.now()
    res  = {}

    testes = {
        "contratacoes_pncp14133": (
            "https://dadosabertos.compras.gov.br/modulo-contratacao/1_consultarContratacao",
            {"pagina": 1, "tamanhoPagina": 1,
             "dataPublicacaoInicio": (hoje - timedelta(days=7)).strftime("%Y-%m-%d"),
             "dataPublicacaoFim": hoje.strftime("%Y-%m-%d")}
        ),
        "pregao_legado": (
            "https://dadosabertos.compras.gov.br/modulo-pregao/1_consultarPregao",
            {"pagina": 1, "tamanhoPagina": 1,
             "dataAberturaInicio": (hoje - timedelta(days=7)).strftime("%Y-%m-%d"),
             "dataAberturaFim": (hoje + timedelta(days=7)).strftime("%Y-%m-%d")}
        ),
    }

    for nome, (url, params) in testes.items():
        t0 = datetime.now()
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            res[nome] = {
                "ok": r.ok, "status": r.status_code,
                "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
                "body": r.text[:200],
            }
        except Exception as e:
            res[nome] = {"ok": False, "erro": str(e)[:120],
                         "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s"}

    if TRANSPARENCIA_KEY:
        t0 = datetime.now()
        try:
            r = requests.get(
                "https://api.portaldatransparencia.gov.br/api-de-dados/licitacoes",
                params={"dataInicial": (hoje-timedelta(days=7)).strftime("%d/%m/%Y"),
                        "dataFinal": hoje.strftime("%d/%m/%Y"), "pagina": 1, "tamanhoPagina": 1},
                headers={**HEADERS, "chave-de-acesso": TRANSPARENCIA_KEY},
                timeout=TIMEOUT,
            )
            res["transparencia"] = {"ok": r.ok, "status": r.status_code,
                                    "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
                                    "chave": "configurada ✅"}
        except Exception as e:
            res["transparencia"] = {"ok": False, "erro": str(e)[:120]}
    else:
        res["transparencia"] = {"ok": False,
                                "nota": "opcional — cadastre em portaldatransparencia.gov.br/api-de-dados/cadastrar-email"}

    res["_config"] = {"versao": "15.0", "transparencia": bool(TRANSPARENCIA_KEY)}
    return jsonify(res)


# ─── Iniciar ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅ Monitor v15 — http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
