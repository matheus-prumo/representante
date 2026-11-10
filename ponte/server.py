#!/usr/bin/env python3
"""App do Representante - ponte no Termux (v1.2).

O app em si fica instalado pelo navegador e guarda os dados no celular.
Este servidor é opcional no dia a dia; quando está ligado ele:
  - entrega o app e suas atualizações;
  - recebe a cópia dos dados e envia ao OneDrive (rclone);
  - lê os prints com o tesseract do Termux (mais preciso).
Só usa a biblioteca padrão do Python.
"""
import csv
import datetime as dt
import io
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

VERSAO = "1.4"
BASE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
DADOS = os.environ.get("REP_DADOS", os.path.join(HOME, "rep-app-dados"))
BACKUPS = os.path.join(DADOS, "backups")
RECEBIDOS = os.path.join(DADOS, "recebidos")  # prints compartilhados do WhatsApp para o Termux
RE_NOME = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
DB_ANTIGO = os.path.join(DADOS, "dados.db")  # da versão 1.1
PORTA = int(os.environ.get("REP_PORTA", "8765"))
REMOTO = os.environ.get("REP_ONEDRIVE", "onedrive:RepresentanteApp")
PASTA_CELULAR = os.path.join(HOME, "storage", "shared", "Documents", "RepresentanteApp")
os.makedirs(BACKUPS, exist_ok=True)
os.makedirs(RECEBIDOS, exist_ok=True)

ST_LABEL = {
    "aguardando": "Aguardando", "armazenando": "Armazenando", "disponivel": "Disponível",
    "ofertado": "Ofertado", "vendido": "Vendido", "cancelado": "Cancelado",
}
ESTATICOS = {"/": "index.html", "/index.html": "index.html", "/manifest.json": "manifest.json", "/sw.js": "sw.js"}
PASTAS_ESTATICAS = ("/icons/", "/vendor/")
# sites que podem usar a ponte (o app publicado no GitHub)
ORIGENS = {"https://matheus-prumo.github.io"} | {o.strip() for o in os.environ.get("REP_ORIGENS", "").split(",") if o.strip()}

sync_lock = threading.Lock()
estado = {"alteradoEm": 0.0, "pendente": False, "ultimoSync": None, "erroSync": None, "ultimoRecebido": None}
_cache_remoto = {"em": 0.0, "ok": False}


def agora():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------- OneDrive ----------------
def remoto_ok(forcar=False):
    if not forcar and time.time() - _cache_remoto["em"] < 60:
        return _cache_remoto["ok"]
    ok = False
    if shutil.which("rclone"):
        try:
            r = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, timeout=20)
            ok = (REMOTO.split(":")[0] + ":") in r.stdout.split()
        except Exception:  # noqa: BLE001
            ok = False
    _cache_remoto.update(em=time.time(), ok=ok)
    return ok


def csv_faltas(itens):
    cli = {x.get("id"): x for x in itens if x.get("kind") == "cliente"}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_ALL)
    w.writerow(["Cliente", "Cidade", "Código", "Produto", "Marca", "Qtd", "Situação", "Anotado em", "Atualizado em", "Obs."])
    for f in sorted([x for x in itens if x.get("kind") == "falta"], key=lambda f: f.get("criadoEm") or ""):
        c = cli.get(f.get("clienteId"), {})
        w.writerow([c.get("nome", ""), c.get("cidade", ""), f.get("codigo", ""), f.get("produto", ""), f.get("marca", ""),
                    f.get("qtd") if f.get("qtd") is not None else "", ST_LABEL.get(f.get("status"), f.get("status", "")),
                    (f.get("criadoEm") or "")[:10], (f.get("atualizadoEm") or "")[:10], f.get("obs", "")])
    return "﻿" + buf.getvalue()


def receber_backup(pacote):
    itens = pacote.get("itens")
    if not isinstance(itens, list):
        raise ValueError("backup inválido")
    hoje = dt.date.today().isoformat()
    js = json.dumps(pacote, ensure_ascii=False)
    for nome, conteudo in (("dados-atual.json", js), (f"backup-{hoje}.json", js), ("faltas.csv", csv_faltas(itens))):
        tmp = os.path.join(BACKUPS, nome + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(conteudo)
        os.replace(tmp, os.path.join(BACKUPS, nome))
    diarios = sorted(f for f in os.listdir(BACKUPS) if f.startswith("backup-") and f.endswith(".json"))
    for velho in diarios[:-45]:
        try:
            os.remove(os.path.join(BACKUPS, velho))
        except OSError:
            pass
    if os.path.isdir(os.path.join(HOME, "storage", "shared")):
        try:
            os.makedirs(PASTA_CELULAR, exist_ok=True)
            for nome in ("dados-atual.json", "faltas.csv"):
                shutil.copy2(os.path.join(BACKUPS, nome), os.path.join(PASTA_CELULAR, nome))
        except OSError:
            pass
    estado["ultimoRecebido"] = agora()
    estado["alteradoEm"] = time.time()
    estado["pendente"] = True


def enviar_onedrive():
    with sync_lock:
        if not os.path.exists(os.path.join(BACKUPS, "dados-atual.json")):
            return False
        if not remoto_ok():
            return False
        estado["pendente"] = False
        try:
            r = subprocess.run(["rclone", "copy", BACKUPS, REMOTO], capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                estado["ultimoSync"] = agora()
                estado["erroSync"] = None
                return True
            estado["erroSync"] = ((r.stderr or "erro desconhecido").strip().splitlines() or ["erro"])[-1][:200]
        except subprocess.TimeoutExpired:
            estado["erroSync"] = "Sem resposta do OneDrive (internet?)"
        except Exception as e:  # noqa: BLE001
            estado["erroSync"] = str(e)[:200]
        estado["pendente"] = True
        return False


def laco_envio():
    falhas = 0
    while True:
        time.sleep(10)
        if estado["pendente"] and time.time() - estado["alteradoEm"] >= 5 and remoto_ok():
            if enviar_onedrive():
                falhas = 0
            else:
                falhas += 1
                time.sleep(min(600, 30 * falhas))


def dados_onedrive():
    if not remoto_ok(forcar=True):
        raise RuntimeError("OneDrive não configurado")
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(["rclone", "copy", REMOTO + "/dados-atual.json", tmp], capture_output=True, text=True, timeout=180)
        arq = os.path.join(tmp, "dados-atual.json")
        if r.returncode != 0 or not os.path.exists(arq):
            raise RuntimeError("Não encontrei dados-atual.json no OneDrive")
        with open(arq, encoding="utf-8") as fh:
            return json.load(fh)


def dados_antigos():
    """Itens da versão 1.1 (banco SQLite), para migrar para o app."""
    if not os.path.exists(DB_ANTIGO):
        return []
    out = []
    with sqlite3.connect(DB_ANTIGO) as c:
        try:
            for i, d in c.execute("SELECT id, dados FROM itens"):
                v = json.loads(d)
                v["id"] = i
                out.append(v)
        except sqlite3.Error:
            return []
    return out


def status():
    return {"versao": VERSAO, "onedrive": remoto_ok(), "remoto": REMOTO, "ultimoSync": estado["ultimoSync"],
            "erroSync": estado["erroSync"], "pendente": estado["pendente"], "ultimoRecebido": estado["ultimoRecebido"],
            "ocr": bool(shutil.which("tesseract")), "melhoraImagem": bool(shutil.which("magick") or shutil.which("convert")),
            "pastaCelular": os.path.isdir(os.path.join(HOME, "storage", "shared")), "temAntigo": os.path.exists(DB_ANTIGO),
            "recebidos": len(listar_recebidos())}


def listar_recebidos():
    try:
        return sorted(f for f in os.listdir(RECEBIDOS) if RE_NOME.match(f) and os.path.isfile(os.path.join(RECEBIDOS, f)))
    except OSError:
        return []


def caminho_recebido(nome):
    if not isinstance(nome, str) or not RE_NOME.match(nome) or nome not in listar_recebidos():
        raise ValueError("print não encontrado")
    return os.path.join(RECEBIDOS, nome)


# ---------------- leitura dos prints ----------------
RE_PROD = re.compile(r"^\D{0,3}?(\d{4,6})\s+(.{3,})$")
RE_DOCA = re.compile(r"^\D{0,3}?(\d{2,3})\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 .&/\-]{1,})$")


def interpretar(texto):
    produtos, marcas, vistos = [], [], set()
    for bruta in texto.splitlines():
        linha = re.sub(r"[|\[\]{}_]+", " ", bruta).strip()
        linha = re.sub(r"\s{2,}", " ", linha)
        if not linha:
            continue
        m = RE_PROD.match(linha)
        if m:
            if m.group(1) not in vistos:
                vistos.add(m.group(1))
                produtos.append({"codigo": m.group(1), "descricao": m.group(2).strip(), "complemento": ""})
            continue
        m = RE_DOCA.match(linha)
        if m:
            nome = m.group(2).strip(" .-")
            if len(nome) >= 2 and nome.upper() not in [x.upper() for x in marcas]:
                marcas.append(nome)
    return {"produtos": produtos, "marcas": marcas}


def ler_imagem(dados, tipo):
    if not shutil.which("tesseract"):
        raise RuntimeError("tesseract não instalado")
    ext = {"image/png": ".png", "image/webp": ".webp"}.get(tipo, ".jpg")
    with tempfile.TemporaryDirectory() as tmp:
        orig = os.path.join(tmp, "print" + ext)
        with open(orig, "wb") as fh:
            fh.write(dados)
        img = orig
        conv = shutil.which("magick") or shutil.which("convert")
        if conv:
            pre = os.path.join(tmp, "pre.png")
            # cinza + ampliar 3x + limiar 60%: testado nos prints da Ramada (26/26 códigos certos)
            r = subprocess.run([conv, orig, "-colorspace", "Gray", "-resize", "300%", "-threshold", "60%", pre],
                               capture_output=True, timeout=60)
            if r.returncode == 0 and os.path.exists(pre):
                img = pre
        r = subprocess.run(["tesseract", img, "stdout", "--psm", "6"], capture_output=True, text=True, timeout=120)
        texto = r.stdout or ""
    res = interpretar(texto)
    res["texto"] = texto
    res["leitor"] = "termux"
    return res


# ---------------- HTTP ----------------
class App(BaseHTTPRequestHandler):
    server_version = "RepApp/" + VERSAO
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        pass

    def _enviar(self, b, tipo, extra=None, code=200):
        self.send_response(code)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def _cors(self):
        origem = self.headers.get("Origin")
        if origem in ORIGENS:
            self.send_header("Access-Control-Allow-Origin", origem)
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        if not self._local():
            return self._json({"erro": "acesso negado"}, 403)
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj, code=200):
        self._enviar(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                     {"Cache-Control": "no-store"}, code)

    def _corpo(self, limite=25 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limite:
            raise ValueError("arquivo grande demais")
        return self.rfile.read(n) if n else b""

    def _local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _estatico(self, p):
        if p in ESTATICOS:
            rel = ESTATICOS[p]
        elif p.startswith(PASTAS_ESTATICAS):
            rel = unquote(p.lstrip("/"))
        else:
            return False
        caminho = os.path.realpath(os.path.join(BASE, rel))
        if not caminho.startswith(os.path.realpath(BASE) + os.sep) or not os.path.isfile(caminho):
            self.close_connection = True
            self._enviar(b"", "text/plain", {"Connection": "close", "Cache-Control": "no-store"}, 404)
            return True
        tipo = mimetypes.guess_type(caminho)[0] or "application/octet-stream"
        if caminho.endswith(".js"):
            tipo = "text/javascript; charset=utf-8"
        elif caminho.endswith(".json"):
            tipo = "application/manifest+json" if rel == "manifest.json" else "application/json"
        elif caminho.endswith(".html"):
            tipo = "text/html; charset=utf-8"
        elif caminho.endswith(".gz"):
            tipo = "application/gzip"
        extra = {"Cache-Control": "no-cache"}
        if rel == "sw.js":
            extra["Service-Worker-Allowed"] = "/"
        with open(caminho, "rb") as fh:
            self._enviar(fh.read(), tipo, extra)
        return True

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._local():
            return self._json({"erro": "acesso negado"}, 403)
        p = urlparse(self.path).path
        try:
            if self._estatico(p):
                return
            if p == "/api/status":
                return self._json(status())
            if p == "/api/onedrive-dados":
                return self._json(dados_onedrive())
            if p == "/api/recebidos":
                return self._json({"recebidos": listar_recebidos()})
            if p == "/api/antigo":
                return self._json({"itens": dados_antigos()})
            return self._json({"erro": "não encontrado"}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"erro": str(e)}, 500)

    def do_POST(self):
        if not self._local():
            return self._json({"erro": "acesso negado"}, 403)
        p = urlparse(self.path).path
        try:
            if p == "/api/backup":
                pacote = json.loads(self._corpo() or b"{}")
                if not isinstance(pacote, dict):
                    raise ValueError("backup inválido")
                receber_backup(pacote)
                return self._json(status())
            if p == "/api/sync":
                ok = enviar_onedrive()
                return self._json(dict(status(), ok=ok))
            if p == "/api/recebidos/ler":
                corpo = json.loads(self._corpo(4096) or b"{}")
                arq = caminho_recebido(corpo.get("nome"))
                with open(arq, "rb") as fh:
                    dados = fh.read()
                tipo = "image/png" if arq.lower().endswith(".png") else "image/webp" if arq.lower().endswith(".webp") else "image/jpeg"
                return self._json(ler_imagem(dados, tipo))
            if p == "/api/recebidos/apagar":
                corpo = json.loads(self._corpo(65536) or b"{}")
                n = 0
                for nome in corpo.get("nomes") or []:
                    try:
                        os.remove(caminho_recebido(nome))
                        n += 1
                    except (ValueError, OSError):
                        pass
                return self._json({"apagados": n})
            if p == "/api/ocr":
                dados = self._corpo()
                if not dados:
                    raise ValueError("imagem vazia")
                return self._json(ler_imagem(dados, (self.headers.get("Content-Type") or "").split(";")[0]))
            return self._json({"erro": "não encontrado"}, 404)
        except ValueError as e:
            return self._json({"erro": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"erro": str(e)}, 500)


def main():
    threading.Thread(target=laco_envio, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORTA), App)
    print("App do Representante v%s: http://127.0.0.1:%d" % (VERSAO, PORTA))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            enviar_onedrive()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
