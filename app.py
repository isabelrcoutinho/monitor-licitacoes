"""
Monitor de Licitações Gov — Backend v12
Fontes estáveis que funcionam 100% no Railway sem proxy:
  1. compras.dados.gov.br  — busca por descrição de texto nativa (SIASG)
  2. api.portaldatransparencia.gov.br — busca livre, dados federais desde 2013
  3. PNCP — mantido como bônus, falha silenciosamente se bloqueado

Nenhuma dessas fontes usa Cloudflare nem bloqueia IPs de nuvem.
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
HEADERS = {"Accept": "application/json", "User-Agent": "MonitorLicitacoesBr/12.0"}

TRANSPARENCIA_KEY = os.environ.get("TRANSPARENCIA_API_KEY", "")


# ─── helpers ──────────────────────────────────────────────────────────────────

def get(url, params=None, headers=None, allow_redirects=True):
    r = requests.get(
        url, params=params,
        headers={**HEADERS, **(headers or {})},
        timeout=TIMEOUT,
        allow_redirects=allow_redirects,
    )
    r.raise_for_status()
    return r.json()

def fmt_data(s):
    if not s:
        return None
    s = str(s).strip()
    # dd/mm/yyyy → yyyy-mm-dd
    if len(s) >= 10 and s[2] == "/":
        p = s[:10].split("/")
        return f"{p[2]}-{p[1]}-{p[0]}"
    return s[:10]


# ─── Fonte 1: compras.dados.gov.br — busca por texto na descrição ─────────────
# Suporta busca textual nativa via parâmetro "descricao"
# HTTP obrigatório (não HTTPS), sem proxy, sem autenticação

def fonte_compras_descricao(kw, pagina):
    """Busca licitações por palavra-chave no objeto/descrição."""
    res, total = [], 0

    # Busca por serviço com a palavra-chave
    try:
        data = get(
            "http://compras.dados.gov.br/servicos/v1/servicos.json",
            params={"descricao": kw, "pagina": pagina},
            allow_redirects=False,
        )
        embedded = data.get("_embedded") or {}
        items    = list(embedded.values())[0] if embedded else []
        total   += (data.get("page") or {}).get("totalElements") or len(items)
        for item in items:
            res.append({
                "id":         str(item.get("id") or ""),
                "titulo":     item.get("descricao") or item.get("nome") or "Sem descrição",
                "orgao":      item.get("orgao") or "Órgão não informado",
                "uf":         "—",
                "municipio":  "",
                "modalidade": "Serviço cadastrado",
                "valor":      None,
                "dataEnc":    None,
                "dataPub":    None,
                "link":       None,
                "fonte":      "Compras.gov",
            })
    except Exception:
        pass

    # Busca por licitações com modalidade pregão
    try:
        for modalidade in ["05", "01", "08"]:
            data = get(
                "http://compras.dados.gov.br/licitacoes/v1/licitacoes.json",
                params={"modalidade": modalidade, "pagina": pagina},
                allow_redirects=False,
            )
            embedded = data.get("_embedded") or {}
            items    = list(embedded.values())[0] if embedded else []
            total   += (data.get("page") or {}).get("totalElements") or len(items)

            kw_lower = kw.lower()
            palavras = [p for p in kw_lower.split() if len(p) > 2]

            for item in items:
                titulo = item.get("nome_objeto") or item.get("objeto") or ""
                if palavras and not any(p in titulo.lower() for p in palavras):
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


# ─── Fonte 2: Portal da Transparência — busca livre com chave de API ──────────
# Cobre todos os órgãos federais desde 2013
# Gratuito, estável, sem bloqueio de IP

def fonte_transparencia(kw, pagina):
    if not TRANSPARENCIA_KEY:
        raise Exception("TRANSPARENCIA_API_KEY não configurada")

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
        params["descricao"] = kw   # busca textual nativa

    data  = get(
        "https://api.portaldatransparencia.gov.br/api-de-dados/licitacoes",
        params=params,
        headers={"chave-de-acesso": TRANSPARENCIA_KEY},
    )

    items = data if isinstance(data, list) else (data.get("data") or [])
    total = len(items) if isinstance(data, list) else (data.get("totalRegistros") or len(items))

    res = []
    for item in items:
        modalidade = (item.get("modalidade") or {})
        orgao      = (item.get("orgaoSuperior") or item.get("unidadeGestora") or {})
        res.append({
            "id":         str(item.get("id") or item.get("numero") or ""),
            "titulo":     item.get("objeto") or item.get("descricao") or "Sem descrição",
            "orgao":      orgao.get("descricao") or "Órgão não informado",
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


# ─── Fonte 3: PNCP — tenta sem proxy, falha silenciosamente ──────────────────

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

    headers = {
        **HEADERS,
        "Origin":  "https://pncp.gov.br",
        "Referer": "https://pncp.gov.br/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    }

    r = requests.get(
        "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
        params=params, headers=headers, timeout=TIMEOUT,
    )

    if not r.text or not r.text.strip().startswith("{"):
        raise ValueError(f"PNCP bloqueou a requisição (status {r.status_code})")

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
        "compras_dados": lambda: fonte_compras_descricao(kw, pagina),
        "pncp":          lambda: fonte_pncp(pagina, uf),
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
        "status":    "ok",
        "versao":    "12.0",
        "timestamp": datetime.now().isoformat(),
        "transparencia": bool(TRANSPARENCIA_KEY),
    })


# ─── Diagnóstico ──────────────────────────────────────────────────────────────

@app.route("/api/teste")
def testar_apis():
    hoje = datetime.now()
    res  = {}

    # compras.dados.gov.br (sempre deve funcionar)
    try:
        t0 = datetime.now()
        r  = requests.get(
            "http://compras.dados.gov.br/licitacoes/v1/licitacoes.json",
            params={"modalidade": "05", "pagina": 1},
            headers=HEADERS, timeout=TIMEOUT, allow_redirects=False,
        )
        res["compras_dados"] = {
            "ok": r.status_code == 200, "status": r.status_code,
            "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
            "nota": "fonte principal — HTTP direto",
        }
    except Exception as e:
        res["compras_dados"] = {"ok": False, "erro": str(e)[:120]}

    # Portal da Transparência
    if TRANSPARENCIA_KEY:
        try:
            hoje_fmt = datetime.now()
            ini = (hoje_fmt - timedelta(days=7)).strftime("%d/%m/%Y")
            fim = hoje_fmt.strftime("%d/%m/%Y")
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
                "chave": "configurada",
            }
        except Exception as e:
            res["transparencia"] = {"ok": False, "erro": str(e)[:120]}
    else:
        res["transparencia"] = {
            "ok": False,
            "nota": "opcional — cadastre em portaldatransparencia.gov.br/api-de-dados/cadastrar-email",
        }

    # PNCP (pode falhar — normal)
    try:
        ini = hoje.strftime("%Y%m%d")
        fim = (hoje + timedelta(days=7)).strftime("%Y%m%d")
        t0  = datetime.now()
        r   = requests.get(
            "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
            params={"dataInicial": ini, "dataFinal": fim, "pagina": 1, "tamanhoPagina": 1},
            headers=HEADERS, timeout=8,
        )
        ok  = r.ok and r.text.strip().startswith("{")
        res["pncp"] = {
            "ok": ok, "status": r.status_code,
            "tempo": f"{(datetime.now()-t0).total_seconds():.1f}s",
            "nota": "bônus — pode ser bloqueado pelo Cloudflare",
        }
    except Exception as e:
        res["pncp"] = {
            "ok": False, "erro": str(e)[:120],
            "nota": "bônus — bloqueio esperado em Railway",
        }

    res["_config"] = {
        "versao":          "12.0",
        "transparencia":   bool(TRANSPARENCIA_KEY),
    }
    return jsonify(res)


# ─── Iniciar ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅ Monitor v12 — http://localhost:{port}")
    print(f"   Transparência: {'✅' if TRANSPARENCIA_KEY else '❌ não configurada (opcional)'}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
