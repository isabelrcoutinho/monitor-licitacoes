"""
Monitor de Licitações Gov — Backend v19
Correção definitiva do 401: testa os dois formatos de autenticação
do Portal da Transparência (chave-de-acesso e Authorization Bearer).
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
HEADERS_BASE = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
}
TRANSPARENCIA_KEY = os.environ.get("TRANSPARENCIA_API_KEY", "").strip()


def fmt_data(s):
    if not s:
        return None
    s = str(s).strip()
    if len(s) >= 10 and s[2] == "/":
        p = s[:10].split("/")
        return f"{p[2]}-{p[1]}-{p[0]}"
    return s[:10]


def get_auth_headers():
    """
    Tenta descobrir qual formato de autenticação funciona.
    Retorna os headers corretos.
    """
    if not TRANSPARENCIA_KEY:
        raise ValueError("TRANSPARENCIA_API_KEY não configurada")

    # Testa os dois formatos possíveis
    formatos = [
        {"chave-de-acesso": TRANSPARENCIA_KEY},
        {"Authorization": f"Bearer {TRANSPARENCIA_KEY}"},
        {"Authorization": TRANSPARENCIA_KEY},
    ]

    url_teste = (
        "https://api.portaldatransparencia.gov.br/api-de-dados/licitacoes"
        "?dataInicial=01/05/2026&dataFinal=28/05/2026&pagina=1&tamanhoPagina=1"
    )

    for extra in formatos:
        try:
            r = requests.get(
                url_teste,
                headers={**HEADERS_BASE, **extra},
                timeout=8,
            )
            if r.status_code == 200:
                return extra   # encontrou o formato correto
        except Exception:
            continue

    # Nenhum formato funcionou — retorna o padrão e deixa o erro aparecer
    return {"chave-de-acesso": TRANSPARENCIA_KEY}


# Cache do formato de auth para não testar toda vez
_auth_headers_cache = None
_auth_headers_ts    = None


def auth_headers():
    global _auth_headers_cache, _auth_headers_ts
    now = datetime.now()
    # Revalida a cada 30 minutos
    if _auth_headers_cache is None or (
        _auth_headers_ts and (now - _auth_headers_ts).seconds > 1800
    ):
        _auth_headers_cache = get_auth_headers()
        _auth_headers_ts    = now
    return _auth_headers_cache


# ─── Fonte: Portal da Transparência ──────────────────────────────────────────

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
        headers={**HEADERS_BASE, **auth_headers()},
        timeout=TIMEOUT,
    )
    r.raise_for_status()

    data  = r.json()
    items = data if isinstance(data, list) else (data.get("data") or [])
    total = (
        len(items) if isinstance(data, list)
        else (data.get("totalRegistros") or len(items))
    )

    res = []
    for item in items:
        mod    = item.get("modalidade") or {}
        org    = item.get("orgaoSuperior") or item.get("unidadeGestora") or {}
        titulo = item.get("objeto") or item.get("descricao") or ""
        res.append({
            "id":         str(item.get("id") or ""),
            "titulo":     titulo or "Sem descrição",
            "orgao":      (
                org.get("descricao") if isinstance(org, dict) else str(org)
            ) or "Órgão não informado",
            "uf":         "Federal",
            "municipio":  "",
            "modalidade": (
                mod.get("descricao") if isinstance(mod, dict) else str(mod)
            ) or "Não informada",
            "valor":      item.get("valorLicitacao") or item.get("valor"),
            "dataEnc":    fmt_data(
                item.get("dataResultadoCompra") or item.get("dataFim")
            ),
            "dataPub":    fmt_data(
                item.get("dataPublicacao") or item.get("dataAbertura")
            ),
            "link":       "https://portaldatransparencia.gov.br/licitacoes/consulta",
            "fonte":      "Transparência",
        })
    return res, total


# ─── Fonte bônus: PNCP ───────────────────────────────────────────────────────

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
        headers=HEADERS_BASE,
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
            "dataEnc":    fmt_data(item.get("dataEncerramentoProposta")),
            "dataPub":    fmt_data(item.get("dataPublicacaoPncp")),
            "link":       item.get("linkSistemaOrigem") or (
                f"https://pncp.gov.br/app/editais/{num}" if num else None
            ),
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

    if not TRANSPARENCIA_KEY:
        return jsonify({
            "resultados": [],
            "total": 0,
            "pagina": pagina,
            "erros": ["CHAVE_AUSENTE: Configure TRANSPARENCIA_API_KEY no Railway"],
            "chave_configurada": False,
            "timestamp": datetime.now().isoformat(),
        })

    tarefas = {
        "transparencia": lambda: fonte_transparencia(kw, pagina),
        "pncp":          lambda: fonte_pncp(pagina, uf),
    }

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
                    erros.append(f"{nome}: {type(e).__name__}: {str(e)[:120]}")

    resultados.sort(key=lambda r: r.get("dataEnc") or "9999-12-31")

    return jsonify({
        "resultados":        resultados,
        "total":             total or len(resultados),
        "pagina":            pagina,
        "erros":             erros,
        "chave_configurada": True,
        "timestamp":         datetime.now().isoformat(),
    })


# ─── Health ───────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({
        "status":            "ok",
        "versao":            "19.0",
        "chave_configurada": bool(TRANSPARENCIA_KEY),
        "timestamp":         datetime.now().isoformat(),
    })


# ─── Diagnóstico detalhado ────────────────────────────────────────────────────

@app.route("/api/teste")
def testar_apis():
    hoje = datetime.now()
    res  = {}

    if not TRANSPARENCIA_KEY:
        res["transparencia"] = {
            "ok":   False,
            "nota": "TRANSPARENCIA_API_KEY não configurada no Railway",
        }
    else:
        ini = (hoje - timedelta(days=7)).strftime("%d/%m/%Y")
        fim = hoje.strftime("%d/%m/%Y")
        url = "https://api.portaldatransparencia.gov.br/api-de-dados/licitacoes"
        params = {"dataInicial": ini, "dataFinal": fim,
                  "pagina": 1, "tamanhoPagina": 1}

        # Testa cada formato de autenticação e mostra o resultado
        formatos = {
            "chave-de-acesso":   {"chave-de-acesso": TRANSPARENCIA_KEY},
            "Bearer":            {"Authorization": f"Bearer {TRANSPARENCIA_KEY}"},
            "Authorization-raw": {"Authorization": TRANSPARENCIA_KEY},
        }

        melhor = None
        for nome_fmt, extra in formatos.items():
            t0 = datetime.now()
            try:
                r = requests.get(
                    url, params=params,
                    headers={**HEADERS_BASE, **extra},
                    timeout=8,
                )
                tempo = f"{(datetime.now()-t0).total_seconds():.1f}s"
                info = {
                    "status": r.status_code,
                    "ok":     r.ok,
                    "tempo":  tempo,
                }
                if r.ok:
                    d = r.json()
                    items = d if isinstance(d, list) else d.get("data", [])
                    info["registros"] = len(items)
                    info["formato_correto"] = True
                    melhor = extra
                else:
                    info["body"] = r.text[:100]
                res[f"auth_{nome_fmt}"] = info
            except Exception as e:
                res[f"auth_{nome_fmt}"] = {
                    "ok": False, "erro": str(e)[:80],
                    "tempo": f"{(datetime.now()-hoje).total_seconds():.1f}s",
                }

        if melhor:
            # Atualiza o cache com o formato correto
            global _auth_headers_cache, _auth_headers_ts
            _auth_headers_cache = melhor
            _auth_headers_ts    = datetime.now()
            res["resultado"] = "✅ Formato correto encontrado e salvo no cache"
        else:
            res["resultado"] = "❌ Nenhum formato funcionou — verifique a chave no Railway"

    # PNCP
    try:
        ini = hoje.strftime("%Y%m%d")
        fim = (hoje + timedelta(days=7)).strftime("%Y%m%d")
        t0  = datetime.now()
        r   = requests.get(
            "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
            params={"dataInicial": ini, "dataFinal": fim,
                    "pagina": 1, "tamanhoPagina": 1},
            headers=HEADERS_BASE, timeout=8,
        )
        ok = r.ok and r.text.strip().startswith("{")
        res["pncp"] = {
            "ok": ok, "status": r.status_code,
            "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
            "nota": "bônus — pode ser bloqueado pelo Cloudflare",
        }
    except Exception as e:
        res["pncp"] = {"ok": False, "erro": str(e)[:80]}

    res["_config"] = {
        "versao":            "19.0",
        "chave_configurada": bool(TRANSPARENCIA_KEY),
        "chave_len":         len(TRANSPARENCIA_KEY) if TRANSPARENCIA_KEY else 0,
    }
    return jsonify(res)


# ─── Iniciar ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅ Monitor v19 — http://localhost:{port}")
    print(f"   Chave: {'✅ configurada' if TRANSPARENCIA_KEY else '❌ ausente'}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
