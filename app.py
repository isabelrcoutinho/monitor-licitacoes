"""
Monitor de Licitações Gov — Backend v18

FONTE PRINCIPAL: Portal da Transparência (licitações federais abertas)
Requer TRANSPARENCIA_API_KEY — cadastro GRATUITO em:
https://portaldatransparencia.gov.br/api-de-dados/cadastrar-email

SEM CHAVE: retorna dados de referência de preços praticados (útil para 
benchmarking) e tenta PNCP se não estiver bloqueado.

Configure a variável no Railway → Variables → TRANSPARENCIA_API_KEY
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


# ─── Fonte 1: Portal da Transparência ────────────────────────────────────────
# Licitações ABERTAS federais com busca textual por descrição.
# Requer chave gratuita: portaldatransparencia.gov.br/api-de-dados/cadastrar-email

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
        mod    = item.get("modalidade") or {}
        org    = item.get("orgaoSuperior") or item.get("unidadeGestora") or {}
        titulo = item.get("objeto") or item.get("descricao") or ""
        res.append({
            "id":         str(item.get("id") or ""),
            "titulo":     titulo or "Sem descrição",
            "orgao":      (org.get("descricao") if isinstance(org, dict) else str(org)) or "Órgão não informado",
            "uf":         "Federal",
            "municipio":  "",
            "modalidade": (mod.get("descricao") if isinstance(mod, dict) else str(mod)) or "Não informada",
            "valor":      item.get("valorLicitacao") or item.get("valor"),
            "dataEnc":    fmt_data(item.get("dataResultadoCompra") or item.get("dataFim")),
            "dataPub":    fmt_data(item.get("dataPublicacao") or item.get("dataAbertura")),
            "link":       "https://portaldatransparencia.gov.br/licitacoes/consulta",
            "fonte":      "Transparência",
        })
    return res, total


# ─── Fonte 2: PNCP (bônus — pode ser bloqueado pelo Cloudflare) ──────────────

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
            "link":       item.get("linkSistemaOrigem") or
                          (f"https://pncp.gov.br/app/editais/{num}" if num else None),
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

    # Sem chave: avisa e tenta só PNCP
    if not TRANSPARENCIA_KEY:
        erros.append(
            "CHAVE_AUSENTE: Configure TRANSPARENCIA_API_KEY no Railway para ver licitações abertas. "
            "Cadastro grátis: portaldatransparencia.gov.br/api-de-dados/cadastrar-email"
        )

    tarefas = {"pncp": lambda: fonte_pncp(pagina, uf)}
    if TRANSPARENCIA_KEY:
        tarefas["transparencia"] = lambda: fonte_transparencia(kw, pagina)

    with ThreadPoolExecutor(max_workers=2) as ex:
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
                if nome != "pncp":
                    erros.append(f"{nome}: {type(e).__name__}: {str(e)[:100]}")

    resultados.sort(key=lambda r: r.get("dataEnc") or "9999-12-31")

    return jsonify({
        "resultados":         resultados,
        "total":              total or len(resultados),
        "pagina":             pagina,
        "erros":              erros,
        "chave_configurada":  bool(TRANSPARENCIA_KEY),
        "timestamp":          datetime.now().isoformat(),
    })


# ─── Health ───────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({
        "status":             "ok",
        "versao":             "18.0",
        "chave_configurada":  bool(TRANSPARENCIA_KEY),
        "instrucao":          "" if TRANSPARENCIA_KEY else
            "Configure TRANSPARENCIA_API_KEY. Cadastro grátis: "
            "portaldatransparencia.gov.br/api-de-dados/cadastrar-email",
        "timestamp":          datetime.now().isoformat(),
    })


# ─── Diagnóstico ──────────────────────────────────────────────────────────────

@app.route("/api/teste")
def testar_apis():
    hoje = datetime.now()
    res  = {}

    # Portal da Transparência
    if TRANSPARENCIA_KEY:
        try:
            ini = (hoje - timedelta(days=7)).strftime("%d/%m/%Y")
            fim = hoje.strftime("%d/%m/%Y")
            t0  = datetime.now()
            r   = requests.get(
                "https://api.portaldatransparencia.gov.br/api-de-dados/licitacoes",
                params={"dataInicial": ini, "dataFinal": fim,
                        "pagina": 1, "tamanhoPagina": 3},
                headers={**HEADERS, "chave-de-acesso": TRANSPARENCIA_KEY},
                timeout=TIMEOUT,
            )
            body = ""
            if r.ok:
                d = r.json()
                items = d if isinstance(d, list) else d.get("data", [])
                body = f"{len(items)} registros retornados"
            res["transparencia"] = {
                "ok": r.ok, "status": r.status_code,
                "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
                "chave": "✅ configurada",
                "resultado": body,
            }
        except Exception as e:
            res["transparencia"] = {"ok": False, "erro": str(e)[:120]}
    else:
        res["transparencia"] = {
            "ok":  False,
            "nota": "⚠ CHAVE AUSENTE — cadastre em portaldatransparencia.gov.br/api-de-dados/cadastrar-email",
            "instrucao": "Adicione TRANSPARENCIA_API_KEY nas variáveis do Railway",
        }

    # PNCP
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
        ok = r.ok and r.text.strip().startswith("{")
        res["pncp"] = {
            "ok": ok, "status": r.status_code,
            "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
            "nota": "bônus — pode ser bloqueado pelo Cloudflare do Railway",
        }
    except Exception as e:
        res["pncp"] = {
            "ok": False, "erro": str(e)[:80],
            "nota": "bloqueio pelo Cloudflare — normal no Railway",
        }

    res["_config"] = {
        "versao":             "18.0",
        "chave_configurada":  bool(TRANSPARENCIA_KEY),
        "proximos_passos":    "Configure TRANSPARENCIA_API_KEY" if not TRANSPARENCIA_KEY else "Tudo OK!",
    }
    return jsonify(res)


# ─── Iniciar ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n{'✅' if TRANSPARENCIA_KEY else '⚠'} Monitor v18 — http://localhost:{port}")
    if not TRANSPARENCIA_KEY:
        print("  ⚠ TRANSPARENCIA_API_KEY não configurada!")
        print("  → Cadastre em: portaldatransparencia.gov.br/api-de-dados/cadastrar-email")
        print("  → Adicione como variável de ambiente no Railway\n")
    app.run(host="0.0.0.0", port=port, debug=False)
