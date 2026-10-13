# -*- coding: utf-8 -*-
import io
import os
import threading
import time
import re
import wave
from types import SimpleNamespace

import requests

import av
import numpy as np
import streamlink
from faster_whisper import WhisperModel


class AudioListener:
    """Captura única de áudio da Twitch e entrega transcrições via callback."""

    def __init__(self, channel, model_size=None, language="pt", chunk_seconds=None, on_transcription=None):
        self.channel = channel.lower()
        self.language = language
        self.model_size = model_size or os.getenv("WHISPER_MODEL", "medium")
        self.backend = os.getenv("WHISPER_BACKEND", "local").strip().lower()
        padrao_chunk = "15" if self.backend == "groq" else "8"
        self.chunk_seconds = float(chunk_seconds or os.getenv("WHISPER_CHUNK_SECONDS", padrao_chunk))
        self._groq_key = os.getenv("GROQ_API_KEY", "").strip()
        self._groq_model = os.getenv("WHISPER_GROQ_MODEL", "whisper-large-v3-turbo")
        self._groq_descanso_ate = 0.0
        self._groq_segundos = 0.0
        self._groq_falhas_seguidas = 0
        self._fallback_local = os.getenv("WHISPER_FALLBACK_LOCAL", "true").lower() in {"1", "true", "yes", "on"}
        if self.backend == "groq" and not self._groq_key:
            print("[AUDIO] WHISPER_BACKEND=groq mas falta GROQ_API_KEY. Usando Whisper local.")
            self.backend = "local"
        self.device = os.getenv("WHISPER_DEVICE", "cpu").lower()
        self.compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
        self.on_transcription = on_transcription
        self.sample_rate = 16000
        self._rodando = False
        self._thread = None
        self._container = None
        self._container_lock = threading.Lock()
        self._ultima_transcricao = ""
        self._debug = os.getenv("WHISPER_DEBUG", "false").lower() in {"1", "true", "yes", "on"}
        self._mostrar_status = os.getenv("WHISPER_STATUS", "true").lower() in {"1", "true", "yes", "on"}
        self._chunks_processados = 0
        self._pausado = threading.Event()
        self.model = None
        # Hotwords fixas de LoL faziam o Whisper "ouvir" Kha'Zix em live IRL.
        # Agora só entram se você definir WHISPER_HOTWORDS no .env.
        self._hotwords = os.getenv("WHISPER_HOTWORDS", "").strip() or None
        self._mostrar_configuracao()
        if self.backend == "local":
            self._carregar_modelo()

    def _mostrar_configuracao(self):
        print("[AUDIO] listener único")
        if self.backend == "groq":
            print(f"[AUDIO] backend=GROQ ({self._groq_model}) | chunk={self.chunk_seconds:g}s | fallback local={'sim' if self._fallback_local else 'não'}")
        else:
            print(f"[AUDIO] backend=LOCAL | modelo={self.model_size} | device={self.device} | compute={self.compute_type} | chunk={self.chunk_seconds:g}s")
        print(f"[AUDIO] hotwords={'(nenhuma)' if not self._hotwords else self._hotwords}")
        print("[AUDIO] fonte=Twitch audio_only | saída=16 kHz mono")

    def _carregar_modelo(self):
        print("[AUDIO] Carregando Faster-Whisper...")
        inicio = time.time()
        self.model = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)
        print(f"[AUDIO] Whisper carregado em {time.time() - inicio:.2f}s")

    def _obter_stream_audio(self):
        url = f"https://www.twitch.tv/{self.channel}"
        session = streamlink.Streamlink()
        session.set_option("http-headers", "User-Agent=Mozilla/5.0")
        streams = session.streams(url)
        if not streams or "audio_only" not in streams:
            raise RuntimeError("audio_only não encontrado na Twitch")
        return streams["audio_only"]

    def _capturar_stream(self):
        stream = self._obter_stream_audio()
        stream_url = stream.to_url()
        container = av.open(stream_url, mode="r")
        with self._container_lock:
            self._container = container

        audio_stream = next((candidate for candidate in container.streams if candidate.type == "audio"), None)
        if audio_stream is None:
            raise RuntimeError("Nenhum stream de áudio encontrado")

        print(
            f"[AUDIO] stream index={audio_stream.index} codec={audio_stream.codec_context.name or '?'} "
            f"rate={audio_stream.codec_context.sample_rate or 0} channels={audio_stream.codec_context.channels or 0}"
        )
        print("[AUDIO] Captura HLS iniciada.")

        resampler = av.audio.resampler.AudioResampler(format="flt", layout="mono", rate=self.sample_rate)
        buffer = np.empty(0, dtype=np.float32)
        chunk_samples = int(self.sample_rate * self.chunk_seconds)

        try:
            for frame in container.decode(audio=audio_stream.index):
                if not self._rodando:
                    break
                if self._pausado.is_set():
                    continue
                converted = resampler.resample(frame)
                if not isinstance(converted, list):
                    converted = [converted]
                for item in converted:
                    if item is None:
                        continue
                    array = np.asarray(item.to_ndarray(), dtype=np.float32).reshape(-1)
                    if array.size == 0:
                        continue
                    buffer = np.concatenate((buffer, array))
                    while len(buffer) >= chunk_samples:
                        chunk = buffer[:chunk_samples].copy()
                        buffer = buffer[chunk_samples:]
                        self._chunks_processados += 1
                        self._processar_chunk(chunk)
        finally:
            with self._container_lock:
                self._container = None
            try:
                container.close()
            except Exception:
                pass

    def _processar_chunk(self, audio):
        if self._pausado.is_set():
            return
        if audio is None or len(audio) == 0:
            return
        rms = float(np.sqrt(np.mean(np.square(audio))))
        peak = float(np.max(np.abs(audio)))
        if peak < 0.003 or rms < 0.0008:
            return

        inicio = time.time()
        segmentos = self._transcrever(audio)
        if segmentos is None:
            return

        # A pausa pode ter sido ativada enquanto o Whisper
        # ainda estava processando o chunk. Nesse caso,
        # este resultado pertence ao período congelado e
        # NÃO deve ser exibido nem entregue ao cérebro.
        if self._pausado.is_set():
            return

        if not segmentos:
            return

        texto = " ".join((segment.text or "").strip() for segment in segmentos).strip()
        texto = self._normalizar_texto(texto)
        if not texto or not self._transcricao_aceitavel(texto, segmentos):
            return
        if texto.lower() == self._ultima_transcricao.lower():
            return
        self._ultima_transcricao = texto

        # Segunda trava antes da saída/callback.
        # Evita uma corrida em que a reflexão começa depois
        # da transcrição mas antes da impressão/entrega.
        if self._pausado.is_set():
            return

        tempo = time.time() - inicio
        if self._mostrar_status:
            print(f"[OUVIDOS] {texto}")
            if self._debug:
                print(f"[AUDIO DEBUG] chunk={self._chunks_processados} tempo={tempo:.2f}s rms={rms:.4f} peak={peak:.4f}")

        if self.on_transcription and not self._pausado.is_set():
            try:
                self.on_transcription(texto)
            except Exception as exc:
                print(f"[AUDIO CALLBACK ERRO] {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # BACKENDS DE TRANSCRIÇÃO
    # ------------------------------------------------------------------
    def _transcrever(self, audio):
        """Retorna lista de segmentos (com .text/.avg_logprob/...) ou None."""
        if self.backend == "groq" and time.time() >= self._groq_descanso_ate:
            segmentos = self._transcrever_groq(audio)
            if segmentos is not None:
                return segmentos
            if not self._fallback_local:
                return None
        elif self.backend == "groq" and not self._fallback_local:
            return None
        return self._transcrever_local(audio)

    def _transcrever_local(self, audio):
        if self.model is None:
            print("[AUDIO] Carregando Whisper local (fallback)...")
            self._carregar_modelo()
        try:
            segments, _info = self.model.transcribe(
                audio,
                language=self.language,
                beam_size=1,
                temperature=0.0,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 300},
                condition_on_previous_text=False,
                no_speech_threshold=0.55,
                log_prob_threshold=-1.0,
                compression_ratio_threshold=2.4,
                hotwords=self._hotwords,
            )
            return list(segments)
        except Exception as exc:
            print(f"[ERRO WHISPER] {type(exc).__name__}: {exc}")
            return None

    @staticmethod
    def _para_wav(audio):
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        return buf.getvalue()

    def _transcrever_groq(self, audio):
        """Whisper large-v3-turbo na nuvem. None = falhou (usa fallback)."""
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {self._groq_key}"},
                files={"file": ("chunk.wav", self._para_wav(audio), "audio/wav")},
                data={
                    "model": self._groq_model,
                    "language": self.language,
                    "response_format": "verbose_json",
                    "temperature": "0",
                },
                timeout=30,
            )
            if resp.status_code == 429:
                espera = 60.0
                try:
                    espera = float(resp.headers.get("retry-after", espera))
                except Exception:
                    pass
                self._groq_descanso_ate = time.time() + max(espera, 5.0)
                print(f"[AUDIO] Groq no limite. {'Usando Whisper local' if self._fallback_local else 'Ouvidos em pausa'} por {espera:.0f}s.")
                return None
            if resp.status_code in (401, 403):
                print("[AUDIO] GROQ_API_KEY inválida. Voltando para o Whisper local.")
                self.backend = "local"
                return None
            resp.raise_for_status()
            dados = resp.json()
            self._groq_falhas_seguidas = 0
            self._groq_segundos += len(audio) / 16000.0
            return [
                SimpleNamespace(
                    text=str(seg.get("text") or ""),
                    avg_logprob=seg.get("avg_logprob"),
                    no_speech_prob=seg.get("no_speech_prob"),
                    compression_ratio=seg.get("compression_ratio"),
                )
                for seg in (dados.get("segments") or [])
            ]
        except Exception as exc:
            self._groq_falhas_seguidas += 1
            print(f"[ERRO GROQ WHISPER] {type(exc).__name__}: {str(exc)[:140]}")
            if self._groq_falhas_seguidas >= 3:
                self._groq_descanso_ate = time.time() + 120
            return None

    @staticmethod
    def _normalizar_texto(texto):
        return re.sub(r"\s+", " ", str(texto or "").replace("\n", " ")).strip()

    @staticmethod
    def _parece_repeticao(texto):
        palavras = re.findall(r"\b[\wÀ-ÿ']+\b", texto.lower())
        if len(palavras) < 5:
            return False
        contagem = {}
        for palavra in palavras:
            contagem[palavra] = contagem.get(palavra, 0) + 1
        if len(palavras) >= 8 and max(contagem.values()) / len(palavras) >= 0.55:
            return True
        consecutivas = 1
        for i in range(1, len(palavras)):
            if palavras[i] == palavras[i - 1]:
                consecutivas += 1
                if consecutivas >= 6:
                    return True
            else:
                consecutivas = 1
        return False

    _ALUCINACOES = (
        "amara.org", "legendas pela comunidade", "obrigado por assistir", "inscreva-se",
        "se inscreva", "deixe seu like", "tchau, tchau", "música", "[música]", "(música)",
        "transcrição e legendas", "legendado por",
    )

    def _transcricao_aceitavel(self, texto, segmentos):
        if len(texto) < 2 or len(texto) > 350 or self._parece_repeticao(texto):
            return False
        baixo = texto.lower().strip(" .!?[]()")
        # Frases típicas que o Whisper inventa em silêncio/música. Só descarta se
        # o trecho for curto, para não perder fala real do streamer.
        if len(baixo) < 45 and any(a in baixo for a in self._ALUCINACOES if len(a) > 8):
            return False
        if baixo in {"música", "musica", "tchau", "obrigado", "obrigada"}:
            return False
        log_probs = [float(getattr(s, "avg_logprob")) for s in segmentos if getattr(s, "avg_logprob", None) is not None]
        no_speech = [float(getattr(s, "no_speech_prob")) for s in segmentos if getattr(s, "no_speech_prob", None) is not None]
        compression = [float(getattr(s, "compression_ratio")) for s in segmentos if getattr(s, "compression_ratio", None) is not None]
        if log_probs and sum(log_probs) / len(log_probs) < -1.20:
            return False
        if no_speech and max(no_speech) > 0.82:
            return False
        if compression and max(compression) > 2.6:
            return False
        return True

    def pausar(self):
        if not self._pausado.is_set():
            self._pausado.set()
            print("[AUDIO] Ouvidos pausados durante a reflexão.")

    def retomar(self):
        if self._pausado.is_set():
            self._pausado.clear()
            print("[AUDIO] Ouvidos retomados.")

    def iniciar_escuta(self):
        if self._rodando:
            return
        self._rodando = True
        self._thread = threading.Thread(target=self._loop, name="TwitchAudioListener", daemon=True)
        self._thread.start()
        print("[AUDIO] Ouvidos da Twitch ativados.")

    def _loop(self):
        while self._rodando:
            try:
                self._capturar_stream()
            except Exception as exc:
                if self._rodando:
                    print(f"[ERRO AUDIO LOOP] {type(exc).__name__}: {exc}")
            if not self._rodando:
                break
            print("[AUDIO] Captura encerrada. Reconectando em 3s...")
            for _ in range(30):
                if not self._rodando:
                    break
                time.sleep(0.1)

    def parar(self):
        if not self._rodando:
            return
        print("[AUDIO] Encerrando ouvidos...")
        self._rodando = False
        with self._container_lock:
            container = self._container
            self._container = None
        if container is not None:
            try:
                container.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            try:
                self._thread.join(timeout=0.15)
            except KeyboardInterrupt:
                pass
        if self._thread and self._thread.is_alive():
            print("[AUDIO] Worker HLS encerrando em segundo plano.")
        else:
            print("[AUDIO] Ouvidos encerrados.")
