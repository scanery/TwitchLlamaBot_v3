# -*- coding: utf-8 -*-
"""
Cliente de LLM com CADEIA DE FALLBACK.

LLM_CHAIN=groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b,ollama:qwen3:8b

O bot tenta o primeiro da lista. Se ele estourar o limite ou falhar, esse modelo
entra em "descanso" e o próximo assume. Cada modelo tem sua própria cota nas APIs
gratuitas, então a cadeia soma as cotas. O último costuma ser o Ollama local.
"""
import base64
import os
import re
import time

import requests

PROVEDORES = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
}

# Modelos que o Groq aposentou -> substituto recomendado por eles.
# (assim um .env antigo continua funcionando sem você editar nada)
SUBSTITUTOS_GROQ = {
    "llama-3.3-70b-versatile": "openai/gpt-oss-120b",
    "llama-3.1-8b-instant": "openai/gpt-oss-20b",
    "qwen/qwen3-32b": "openai/gpt-oss-120b",
    "meta-llama/llama-4-scout-17b-16e-instruct": "qwen/qwen3.6-27b",
    "meta-llama/llama-4-maverick-17b-128e-instruct": "qwen/qwen3.6-27b",
    "moonshotai/kimi-k2-instruct": "openai/gpt-oss-120b",
    "moonshotai/kimi-k2-instruct-0905": "openai/gpt-oss-120b",
}

ESPERA_CURTA = 20.0  # 429 com espera até isso: espera e tenta o MESMO modelo de novo


class _LimiteExcedido(Exception):
    def __init__(self, espera, texto=""):
        super().__init__(texto)
        self.espera = espera


class _Backend:
    def __init__(self, provedor, modelo):
        self.provedor = provedor
        self.modelo = modelo
        self.descanso_ate = 0.0
        self.params_removidos = set()
        self.chamadas = 0
        self.falhas = 0
        self.tokens = 0
        if provedor == "ollama":
            url = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
            self.base = url.replace("/api/generate", "/api/chat")
            self.chave = ""
        else:
            self.base, env_chave = PROVEDORES[provedor]
            self.chave = os.getenv(env_chave, "").strip()

    @property
    def rotulo(self):
        return f"{self.provedor}:{self.modelo}"


def _bool(nome, padrao="false"):
    return os.getenv(nome, padrao).strip().lower() in {"1", "true", "yes", "on"}


class LLMClient:
    def __init__(self, env_cadeia="LLM_CHAIN", padrao=None, rotulo="IA", fallback_env=None):
        self.rotulo = rotulo
        self.verbose = _bool("AI_VERBOSE", "true")
        self.timeout_nuvem = float(os.getenv("LLM_CLOUD_TIMEOUT", "60"))
        self.timeout_local = float(os.getenv("OLLAMA_TIMEOUT", "600"))
        self.ollama_ctx = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
        self.ollama_think = _bool("OLLAMA_THINK", "false")
        self.esforco = os.getenv("LLM_REASONING_EFFORT", "low").strip().lower()
        self.ultimo = None
        self.backends = []

        spec = os.getenv(env_cadeia, "").strip()
        if not spec and fallback_env:
            spec = os.getenv(fallback_env, "").strip()
        spec = spec or (padrao or "")

        vistos = set()
        for item in [s.strip() for s in spec.split(",") if s.strip()]:
            provedor, _, modelo = item.partition(":")
            provedor = provedor.lower()
            if provedor != "ollama" and provedor not in PROVEDORES:
                print(f"[{rotulo}] Provedor desconhecido ignorado: {item}")
                continue
            if not modelo:
                continue
            if provedor == "groq" and modelo in SUBSTITUTOS_GROQ:
                novo = SUBSTITUTOS_GROQ[modelo]
                print(f"[{rotulo}] groq:{modelo} foi aposentado pelo Groq. Usando {novo} no lugar.")
                modelo = novo
            if (provedor, modelo) in vistos:
                continue
            vistos.add((provedor, modelo))
            b = _Backend(provedor, modelo)
            if provedor != "ollama" and not b.chave:
                print(f"[{rotulo}] {b.rotulo} ignorado: falta {PROVEDORES[provedor][1]} no .env")
                continue
            self.backends.append(b)

    @property
    def ativo(self):
        return bool(self.backends)

    def resumo(self):
        return " -> ".join(b.rotulo for b in self.backends) or "(nenhum)"

    def uso(self):
        """Quanto cada modelo já consumiu nesta execução."""
        partes = [
            f"{b.rotulo}: {b.chamadas} chamadas, {b.tokens / 1000:.1f}K tokens"
            for b in self.backends if b.chamadas
        ]
        return " | ".join(partes) or "nenhuma chamada ainda"

    # ------------------------------------------------------------
    def chat(self, mensagens, temperature=0.6, max_tokens=600, json_mode=True, think=None):
        """Devolve o texto da resposta ou '' se todos os backends falharem."""
        for b in self.backends:
            if b.descanso_ate > time.time():
                continue
            for tentativa in (1, 2):
                inicio = time.perf_counter()
                b.chamadas += 1
                try:
                    if b.provedor == "ollama":
                        texto, uso = self._ollama(b, mensagens, temperature, max_tokens, json_mode, think)
                    else:
                        texto, uso = self._openai(b, mensagens, temperature, max_tokens, json_mode)
                except _LimiteExcedido as exc:
                    b.falhas += 1
                    if exc.espera <= ESPERA_CURTA and tentativa == 1:
                        print(f"[{self.rotulo}] {b.rotulo} pediu {exc.espera:.0f}s de pausa. Esperando e tentando de novo.")
                        time.sleep(exc.espera + 0.5)
                        continue
                    b.descanso_ate = time.time() + exc.espera
                    print(f"[{self.rotulo}] {b.rotulo} indisponível por {exc.espera:.0f}s ({str(exc)[:90]}). Próximo da cadeia.")
                    break
                except Exception as exc:
                    b.falhas += 1
                    b.descanso_ate = time.time() + 20
                    print(f"[{self.rotulo} ERRO] {b.rotulo}: {type(exc).__name__}: {str(exc)[:160]}")
                    break

                if not texto.strip():
                    b.falhas += 1
                    print(f"[{self.rotulo}] {b.rotulo} devolveu resposta vazia. Tentando o próximo.")
                    break

                self.ultimo = b.rotulo
                b.tokens += int(uso.get("total", 0))
                if self.verbose:
                    print(
                        f"[{self.rotulo}] {b.rotulo} | {time.perf_counter() - inicio:.1f}s | "
                        f"entrada={uso.get('prompt', 0)} saída={uso.get('saida', 0)}"
                    )
                return texto
        return ""

    # ------------------------------------------------------------
    @staticmethod
    def _limpar(texto):
        texto = re.sub(r"<think>.*?</think>", "", texto or "", flags=re.DOTALL | re.IGNORECASE)
        return texto.strip()

    def _openai(self, b, mensagens, temperature, max_tokens, json_mode):
        corpo = {
            "model": b.modelo,
            "messages": mensagens,
            "temperature": temperature,
            "max_tokens": int(max_tokens),
        }
        if json_mode and "response_format" not in b.params_removidos:
            corpo["response_format"] = {"type": "json_object"}
        # gpt-oss raciocina antes de responder e isso gasta tokens: "low" economiza muito
        if b.modelo.startswith("openai/gpt-oss") and self.esforco in {"low", "medium", "high"} \
                and "reasoning_effort" not in b.params_removidos:
            corpo["reasoning_effort"] = self.esforco

        for _ in range(3):
            r = requests.post(
                f"{b.base}/chat/completions",
                headers={"Authorization": f"Bearer {b.chave}"},
                json=corpo,
                timeout=self.timeout_nuvem,
            )
            if r.status_code == 429:
                espera = 60.0
                try:
                    espera = float(r.headers.get("retry-after", espera))
                except Exception:
                    pass
                corpo_erro = r.text.lower()
                if "per day" in corpo_erro or "tpd" in corpo_erro or "daily" in corpo_erro:
                    espera = max(espera, 1800.0)
                raise _LimiteExcedido(max(espera, 5.0), r.text[:200])
            if r.status_code == 404:
                raise _LimiteExcedido(3600.0, "modelo não existe mais nesse provedor")
            if r.status_code == 400:
                # parâmetro opcional que o modelo não aceita: remove e tenta de novo
                removido = False
                for param in ("reasoning_effort", "response_format"):
                    if param in corpo and param in r.text.lower():
                        corpo.pop(param)
                        b.params_removidos.add(param)
                        removido = True
                        break
                if not removido and "response_format" in corpo:
                    corpo.pop("response_format")
                    b.params_removidos.add("response_format")
                    removido = True
                if removido:
                    continue
            r.raise_for_status()
            dados = r.json()
            msg = (dados.get("choices") or [{}])[0].get("message") or {}
            u = dados.get("usage") or {}
            uso = {
                "prompt": u.get("prompt_tokens", 0),
                "saida": u.get("completion_tokens", 0),
                "total": u.get("total_tokens", 0),
            }
            return self._limpar(msg.get("content")), uso
        return "", {}

    def _ollama(self, b, mensagens, temperature, max_tokens, json_mode, think):
        corpo = {
            "model": b.modelo,
            "messages": mensagens,
            "stream": False,
            "keep_alive": "10m",
            "options": {
                "temperature": temperature,
                "num_predict": int(max_tokens),
                "num_ctx": self.ollama_ctx,
                "top_k": int(os.getenv("OLLAMA_TOP_K", "20")),
                "top_p": float(os.getenv("OLLAMA_TOP_P", "0.95")),
                "min_p": float(os.getenv("OLLAMA_MIN_P", "0.0")),
                "presence_penalty": float(os.getenv("OLLAMA_PRESENCE_PENALTY", "1.0")),
            },
        }
        if json_mode:
            corpo["format"] = "json"
        corpo["think"] = self.ollama_think if think is None else bool(think)

        r = requests.post(b.base, json=corpo, timeout=self.timeout_local)
        if r.status_code == 404:
            raise RuntimeError(f"modelo não encontrado no Ollama. Rode: ollama pull {b.modelo}")
        r.raise_for_status()
        dados = r.json()
        uso = {
            "prompt": dados.get("prompt_eval_count", 0),
            "saida": dados.get("eval_count", 0),
        }
        uso["total"] = uso["prompt"] + uso["saida"]
        return self._limpar((dados.get("message") or {}).get("content")), uso

    # ------------------------------------------------------------
    def descrever_imagem(self, instrucao, jpeg_bytes, max_tokens=300):
        """Visão: manda uma imagem JPEG para o primeiro backend disponível."""
        b64 = base64.b64encode(jpeg_bytes).decode("ascii")
        for b in self.backends:
            if b.descanso_ate > time.time():
                continue
            b.chamadas += 1
            try:
                if b.provedor == "ollama":
                    msg = [{"role": "user", "content": instrucao, "images": [b64]}]
                    texto, uso = self._ollama(b, msg, 0.2, max_tokens, False, False)
                else:
                    msg = [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": instrucao},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        ],
                    }]
                    texto, uso = self._openai(b, msg, 0.2, max_tokens, False)
            except _LimiteExcedido as exc:
                b.descanso_ate = time.time() + exc.espera
                print(f"[VISÃO] {b.rotulo} indisponível por {exc.espera:.0f}s.")
                continue
            except Exception as exc:
                b.descanso_ate = time.time() + 30
                print(f"[VISÃO ERRO] {b.rotulo}: {type(exc).__name__}: {str(exc)[:160]}")
                continue
            b.tokens += int(uso.get("total", 0))
            if texto.strip():
                self.ultimo = b.rotulo
                return texto.strip()
        return ""

    # ------------------------------------------------------------
    def verificar(self):
        """Confere no start se os modelos existem e desliga os que sumiram."""
        for b in self.backends:
            if b.provedor == "ollama":
                try:
                    url = b.base.replace("/api/chat", "/api/tags")
                    nomes = {m.get("name") for m in requests.get(url, timeout=5).json().get("models", [])}
                    ok = b.modelo in nomes or f"{b.modelo}:latest" in nomes
                    print(f"[{self.rotulo}] {b.rotulo}: {'OK' if ok else 'NÃO BAIXADO -> ollama pull ' + b.modelo}")
                except Exception:
                    print(f"[{self.rotulo}] {b.rotulo}: Ollama não respondeu em localhost.")
                continue
            try:
                r = requests.get(f"{b.base}/models", headers={"Authorization": f"Bearer {b.chave}"}, timeout=10)
                if r.status_code in (401, 403):
                    print(f"[{self.rotulo}] {b.rotulo}: CHAVE INVÁLIDA ({r.status_code}). Desativado.")
                    b.descanso_ate = time.time() + 10 ** 9
                    continue
                ids = {str(m.get("id", "")).replace("models/", "") for m in r.json().get("data", [])}
                if ids and b.modelo not in ids:
                    print(f"[{self.rotulo}] {b.rotulo}: NÃO existe na lista do provedor. Desativado nesta execução.")
                    b.descanso_ate = time.time() + 10 ** 9
                else:
                    print(f"[{self.rotulo}] {b.rotulo}: OK")
            except Exception as exc:
                print(f"[{self.rotulo}] {b.rotulo}: não consegui verificar ({type(exc).__name__}).")
