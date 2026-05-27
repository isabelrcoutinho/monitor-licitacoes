"""
Monitor de Licitações Gov — Backend v13
Fontes estáveis confirmadas:
  1. dadosabertos.compras.gov.br (API nova HTTPS) — materiais e contratações
  2. api.portaldatransparencia.gov.br — licitações federais com busca textual
  3. PNCP — bônus, falha silenciosamente se bloqueado pelo Cloudflare

IMPORTANTE: Se TRANSPARENCIA_API_KEY estiver configurada, essa fonte
retorna resultados reais com busca textual. Cadastro gratuito em:
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
HEADERS = {"Accept": "application/json", "User-Agent": "MonitorLicitacoesBr/13.0"}
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
    if not kw:
        return True
    t = (texto or "").lower()
    return any(p in t for p in kw.lower().split() if len(p) > 2)


# ─── Fonte 1: dadosabertos.compras.gov.br (nova API HTTPS) ───────────────────

def fonte_dadosabertos(kw, pagina):
    """
    Nova API do Compras.gov.br — HTTPS, sem autenticação.
    Endpoint de materiais com busca por descrição.
    """
    res, total = [], 0

    try:
        r = requests.get(
            "https://dadosabertos.compras.gov.br/modulo-material/1_consultarMaterial",
            params={"pagina": pagina, "descricao": kw},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        if r.ok:
            data  = r.json()
            items = data.get("resultado") or []
            total = data.get("totalRegistros") or len(items)
            for item in items:
                desc = item.get("descricaoMaterial") or item.get("descricao") or ""
                if not filtrar(desc, kw):
                    continue
                res.append({
                    "id":         str(item.get("codigoMaterial") or item.get("id") or ""),
                    "titulo":     desc or "Sem descrição",
                    "orgao":      "Catálogo de Materiais — Gov.br",
                    "uf":         "—",
                    "municipio":  "",
                    "modalidade": "Material catalogado",
                    "valor":      None,
                    "dataEnc":    None,
                    "dataPub":    None,
                    "link":       "https://www.gov.br/compras/pt-br",
                    "fonte":      "Compras.gov",
                })
    except Exception:
        pass

    # Tenta endpoint de contratações
    try:
        r = requests.get(
            "https://dadosabertos.compras.gov.br/modulo-compra/2_consultarCompra",
            params={"pagina": pagina, "descricao": kw},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        if r.ok:
            data  = r.json()
            items = data.get("resultado") or []
            total = max(total, data.get("totalRegistros") or len(items))
            for item in items:
                titulo = item.get("objeto") or item.get("descricao") or ""
                if not filtrar(titulo, kw):
                    continue
                res.append({
                    "id":         str(item.get("numeroCompra") or item.get("id") or ""),
                    "titulo":     titulo or "Sem descrição",
                    "orgao":      item.get("nomeUnidadeCompradora") or "Órgão não informado",
                    "uf":         (item.get("uf") or "—").upper(),
                    "municipio":  "",
                    "modalidade": item.get("modalidade") or "Não informada",
                    "valor":      item.get("valorTotal") or item.get("valorEstimado"),
                    "dataEnc":    fmt_data(item.get("dataEncerramentoProposta")),
                    "dataPub":    fmt_data(item.get("dataPublicacao")),
                    "link":       "https://www.gov.br/compras/pt-br",
                    "fonte":      "Compras.gov",
                })
    except Exception:
        pass

    return res, total


# ─── Fonte 2: Portal da Transparência (se chave configurada) ──────────────────

def fonte_transparencia(kw, pagina):
    hoje = datetime.now()
    ini  = (hoje - timedelta(days=60)).strftime("%d/%m/%Y")
    fim  = hoje.strftime("%d/%m/%Y")

    params = {
        "dataInicial":   ini,
        "dataFinal":     fim,
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
            "orgao":      orgao.get("descricao") if isinstance(orgao, dict) else str(orgao),
            "uf":         "—",
            "municipio":  "",
            "modalidade": modalidade.get("descricao") if isinstance(modalidade, dict) else str(modalidade),
            "valor":      item.get("valorLicitacao") or item.get("valor"),
            "dataEnc":    fmt_data(item.get("dataResultadoCompra") or item.get("dataFim")),
            "dataPub":    fmt_data(item.get("dataPublicacao") or item.get("dataAbertura")),
            "link":       "https://portaldatransparencia.gov.br/licitacoes/consulta",
            "fonte":      "Transparência",
        })
    return res, total


# ─── Fonte 3: PNCP (bônus — pode ser bloqueado pelo Cloudflare) ──────────────

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
        params=params,
        headers={**HEADERS,
                 "Origin":  "https://pncp.gov.br",
                 "Referer": "https://pncp.gov.br/",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"},
        timeout=10,
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
            "dataEnc":    str(item.get("dataEncerramentoProposta") or "")[:10] or None,
            "dataPub":    str(item.get("dataPublicacaoPncp") or "")[:10] or None,
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
        "dadosabertos": lambda: fonte_dadosabertos(kw, pagina),
        "pncp":         lambda: fonte_pncp(pagina, uf),
    }
    if TRANSPARENCIA_KEY:
        tarefas["transparencia"] = lambda: fonte_transparencia(kw, pagina)

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
                erros.append(f"{nome}: {type(e).__name__}: {str(e)[:120]}")

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
        "versao":        "13.0",
        "transparencia": bool(TRANSPARENCIA_KEY),
        "timestamp":     datetime.now().isoformat(),
    })


# ─── Diagnóstico ──────────────────────────────────────────────────────────────

@app.route("/api/teste")
def testar_apis():
    hoje = datetime.now()
    res  = {}

    # dadosabertos.compras.gov.br
    try:
        t0 = datetime.now()
        r  = requests.get(
            "https://dadosabertos.compras.gov.br/modulo-compra/2_consultarCompra",
            params={"pagina": 1, "descricao": "consultoria"},
            headers=HEADERS, timeout=TIMEOUT,
        )
        res["dadosabertos"] = {
            "ok": r.ok, "status": r.status_code,
            "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
            "nota": "nova API HTTPS — fonte principal",
        }
    except Exception as e:
        res["dadosabertos"] = {"ok": False, "erro": str(e)[:120]}

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
            res["transparencia"] = {"ok": False, "erro": str(e)[:120]}
    else:
        res["transparencia"] = {
            "ok":   False,
            "nota": "⚠ chave não configurada — cadastre em portaldatransparencia.gov.br/api-de-dados/cadastrar-email",
        }

    # PNCP
    try:
        t0  = datetime.now()
        ini = hoje.strftime("%Y%m%d")
        fim = (hoje + timedelta(days=7)).strftime("%Y%m%d")
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
        "versao":        "13.0",
        "transparencia": bool(TRANSPARENCIA_KEY),
        "dica":          "Configure TRANSPARENCIA_API_KEY para dados federais completos",
    }
    return jsonify(res)


# ─── Iniciar ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅ Monitor v13 — http://localhost:{port}")
    print(f"   Transparência: {'✅' if TRANSPARENCIA_KEY else '❌ não configurada'}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
