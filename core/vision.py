# -*- coding: utf-8 -*-
"""
OLHOS do bot: de tempos em tempos pega UM quadro do vídeo da live (stream
de baixa qualidade, quase sem custo de CPU) e pede a um modelo de visão
na nuvem uma descrição curta. A descrição vira um evento "visao" na memória.

Não captura a sua tela: usa o mesmo HLS público da Twitch que o áudio usa.
"""
import io
import os
import re
import threading
import time

import av
import streamlink

try:
    from PIL import Image
except Exception:  # Pillow é opcional; sem ele a visão fica desligada
    Image = None


_PROMPT = (
    "Esta é uma imagem da live da Twitch do canal {canal}. {contexto}"
    "Descreva em português, em no máximo 3 frases curtas, o que aparece: "
    "se é jogo (qual, se der para ver) ou câmera/IRL (onde, quem, o que faz), "
    "se há vídeo de outra plataforma sendo reproduzido, e qualquer texto legível na tela. "
    "Não adivinhe nomes de pessoas. Se não der para entender, diga isso."
)


def _tokens(t):
    return {x for x in re.findall(r"\w{4,}", (t or "").lower())}


class StreamVision:
    def __init__(self, channel, llm, on_description, info_canal=None):
        self.channel = channel.lower()
        self.llm = llm
        self.on_description = on_description
        self.info_canal = info_canal  # função -> dict com title/game_name
        self.intervalo = float(os.getenv("VISION_INTERVAL_SECONDS", "120"))
        self.qualidade = os.getenv("VISION_QUALITY", "360p")
        self._rodando = False
        self._thread = None
        self._ultima = ""
        self.ativo = (
            os.getenv("VISION_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
            and Image is not None
            and llm is not None
            and llm.ativo
        )

    def start(self):
        if not self.ativo or self._rodando:
            if os.getenv("VISION_ENABLED", "false").lower() in {"1", "true", "yes", "on"} and not self.ativo:
                print("[VISÃO] Desligada: falta Pillow (pip install pillow) ou VISION_CHAIN no .env.")
            return
        self._rodando = True
        self._thread = threading.Thread(target=self._loop, name="Vision", daemon=True)
        self._thread.start()
        print(f"[VISÃO] Olhos ativados: 1 quadro a cada {self.intervalo:.0f}s ({self.llm.resumo()}).")

    def stop(self):
        self._rodando = False

    # ------------------------------------------------------------------
    def capturar_jpeg(self):
        session = streamlink.Streamlink()
        session.set_option("http-headers", "User-Agent=Mozilla/5.0")
        streams = session.streams(f"https://www.twitch.tv/{self.channel}")
        if not streams:
            raise RuntimeError("live offline ou indisponível")
        escolha = None
        for nome in (self.qualidade, "480p", "360p", "480p30", "360p30", "worst"):
            if nome in streams:
                escolha = streams[nome]
                break
        if escolha is None:
            raise RuntimeError("nenhuma qualidade de vídeo disponível")

        container = av.open(escolha.to_url(), mode="r", timeout=15)
        try:
            video = next(s for s in container.streams if s.type == "video")
            for i, frame in enumerate(container.decode(video)):
                img = frame.to_image()
                # pula quadros pretos (transição/carregando)
                if sum(img.resize((8, 8)).convert("L").getdata()) / 64 < 8 and i < 30:
                    continue
                img.thumbnail((768, 768))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "JPEG", quality=70)
                return buf.getvalue()
        finally:
            container.close()
        raise RuntimeError("não consegui decodificar um quadro")

    def _loop(self):
        falhas = 0
        while self._rodando:
            try:
                info = (self.info_canal() if self.info_canal else {}) or {}
                contexto = ""
                if info.get("title") or info.get("game_name"):
                    contexto = f"Título da live: {info.get('title') or '?'}. Categoria: {info.get('game_name') or '?'}. "
                jpeg = self.capturar_jpeg()
                texto = self.llm.descrever_imagem(
                    _PROMPT.format(canal=self.channel, contexto=contexto), jpeg
                )
                falhas = 0
                if texto:
                    a, b = _tokens(texto), _tokens(self._ultima)
                    parecida = bool(a and b and len(a & b) / len(a | b) >= 0.75)
                    if not parecida:
                        self._ultima = texto
                        print(f"[VISÃO] {texto}")
                        self.on_description(texto)
            except Exception as exc:
                falhas += 1
                print(f"[VISÃO ERRO] {type(exc).__name__}: {str(exc)[:140]}")

            espera = self.intervalo * (1 + min(falhas, 4))
            fim = time.time() + espera
            while self._rodando and time.time() < fim:
                time.sleep(0.5)
