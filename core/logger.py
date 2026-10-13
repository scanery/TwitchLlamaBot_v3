# -*- coding: utf-8 -*-
"""
Salva TUDO que aparece no terminal em logs/bot_AAAA-MM-DD_HH-MM-SS.log,
uma linha por vez, com data e hora no começo (estilo log de servidor).
A tela continua igual; o arquivo é uma cópia com carimbo de tempo.

No .env (opcionais):
  LOG_ENABLED=true            desliga com false
  LOG_DIR=logs                pasta dos logs
  LOG_RETENTION_DAYS=30       apaga logs mais antigos que isso (0 = nunca apaga)
"""
import atexit
import os
import re
import sys
import threading
import time
from datetime import datetime

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


class _Tee:
    def __init__(self, original, arquivo, trava):
        self._original = original
        self._arquivo = arquivo
        self._trava = trava
        self._restos = {}  # um buffer por thread: print() escreve texto e "\n" em chamadas separadas

    def write(self, texto):
        try:
            n = self._original.write(texto)
        except Exception:
            n = len(texto)
        try:
            self._gravar(texto)
        except Exception:
            pass  # log nunca pode derrubar o bot
        return n

    def _gravar(self, texto):
        texto = _ANSI.sub("", str(texto)).replace("\r\n", "\n").replace("\r", "\n")
        with self._trava:
            tid = threading.get_ident()
            resto = self._restos.get(tid, "") + texto
            if "\n" not in resto:
                self._restos[tid] = resto
                return
            *linhas, resto = resto.split("\n")
            if resto:
                self._restos[tid] = resto
            else:
                self._restos.pop(tid, None)
            carimbo = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for linha in linhas:
                if linha.strip():
                    self._arquivo.write(f"[{carimbo}] {linha.rstrip()}\n")
            self._arquivo.flush()

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass

    def __getattr__(self, nome):  # isatty, encoding, fileno... vêm do terminal real
        return getattr(self._original, nome)


def iniciar_log():
    if os.getenv("LOG_ENABLED", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return None

    pasta = os.getenv("LOG_DIR", "logs").strip() or "logs"
    os.makedirs(pasta, exist_ok=True)

    retencao = int(os.getenv("LOG_RETENTION_DAYS", "30") or 0)
    if retencao > 0:
        limite = time.time() - retencao * 86400
        for nome in os.listdir(pasta):
            caminho = os.path.join(pasta, nome)
            try:
                if nome.startswith("bot_") and nome.endswith(".log") and os.path.getmtime(caminho) < limite:
                    os.remove(caminho)
            except Exception:
                pass

    caminho = os.path.join(pasta, f"bot_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log")
    arquivo = open(caminho, "a", encoding="utf-8", errors="replace")
    trava = threading.Lock()

    sys.stdout = _Tee(sys.stdout, arquivo, trava)
    sys.stderr = _Tee(sys.stderr, arquivo, trava)

    def _fechar():
        for fluxo in (sys.stdout, sys.stderr):
            if isinstance(fluxo, _Tee):
                for tid in list(fluxo._restos):
                    if fluxo._restos[tid].strip():
                        fluxo._restos[tid] += "\n"
                        fluxo._gravar("")
        try:
            arquivo.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ===== FIM DO LOG =====\n")
            arquivo.close()
        except Exception:
            pass

    atexit.register(_fechar)
    print(f"===== INÍCIO DO LOG ===== arquivo: {caminho}")
    return caminho