# -*- coding: utf-8 -*-
import os
import re
import socket
import time

import requests


_RE_PRIVMSG = re.compile(
    r"^(?:@(?P<tags>\S+) )?:(?P<nick>[^!\s]+)![^\s]+ PRIVMSG #(?P<canal>\S+) :(?P<msg>.*)$"
)


class TwitchClient:
    SERVER = "irc.chat.twitch.tv"
    PORT = 6667

    def __init__(self, channel=None, token=None, client_id=None):
        self.channel = (channel or os.getenv("TWITCH_CHANNEL", "")).lower().strip().lstrip("#")
        if not self.channel:
            raise ValueError("TWITCH_CHANNEL não definido no .env")

        self.token = (token or os.getenv("TWITCH_TOKEN", "")).strip()
        if self.token.lower().startswith("oauth:"):
            self.token = self.token[6:]
        self.client_id = (client_id or os.getenv("TWITCH_CLIENT_ID", "")).strip()
        self.nick = os.getenv("TWITCH_USERNAME", "").strip().lower()
        self.sock = None
        self._buffer = ""
        self._rodando = False
        self.can_send = os.getenv("ENABLE_CHAT_RESPONSES", "false").lower() in {"1", "true", "yes", "on"}
        self.diagnostics = os.getenv("TWITCH_DIAGNOSTICS", "true").lower() in {"1", "true", "yes", "on"}
        self.last_server_rx = time.time()
        self.last_chat_rx = 0.0
        self.chat_messages = 0
        self.server_messages = 0

    def _validate_token(self):
        if not self.token:
            raise RuntimeError("TWITCH_TOKEN não definido. O modo anônimo foi removido.")
        headers = {"Authorization": f"OAuth {self.token}"}
        response = requests.get("https://id.twitch.tv/oauth2/validate", headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        login = str(data.get("login") or "").strip().lower()
        if login and not self.nick:
            self.nick = login
        if not self.nick:
            raise RuntimeError("Não foi possível descobrir o usuário Twitch pelo token.")
        return data

    def _abrir_socket(self):
        self._fechar_socket()
        self._buffer = ""
        self._validate_token()

        s = socket.socket()
        s.settimeout(0.35)
        s.connect((self.SERVER, self.PORT))
        s.send(f"PASS oauth:{self.token}\r\n".encode("utf-8"))
        s.send(f"NICK {self.nick}\r\n".encode("utf-8"))
        s.send(b"CAP REQ :twitch.tv/membership twitch.tv/tags twitch.tv/commands\r\n")
        s.send(f"JOIN #{self.channel}\r\n".encode("utf-8"))
        self.sock = s
        print(f"[TWITCH] Socket IRC conectado. JOIN #{self.channel} enviado.")

    def _fechar_socket(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def start(self):
        self._rodando = True
        self._abrir_socket()
        modo = "ENVIO ATIVO" if self.can_send else "SOMENTE LEITURA"
        print(f"[TWITCH] Autenticado como {self.nick} | {modo}")
        print(f"[TWITCH] Lendo #{self.channel}")

    def stop(self):
        self._rodando = False
        self._fechar_socket()

    conectar = start
    fechar = stop

    def _reconectar(self):
        if not self._rodando:
            return
        print("[TWITCH] Conexão caiu. Reconectando...")
        for espera in (2, 5, 10, 20, 30):
            if not self._rodando:
                return
            try:
                self._abrir_socket()
                print(f"[TWITCH] Reconectado como {self.nick}.")
                return
            except Exception as exc:
                print(f"[TWITCH] Falha ao reconectar: {exc}. Tentando em {espera}s")
                time.sleep(espera)

    def receber_linhas(self):
        if not self.sock:
            self._reconectar()
            return []

        try:
            dados = self.sock.recv(8192)
        except socket.timeout:
            return []
        except OSError:
            self._reconectar()
            return []

        if not dados:
            self._reconectar()
            return []

        self.last_server_rx = time.time()
        self.server_messages += 1
        self._buffer += dados.decode("utf-8", errors="ignore")
        partes = self._buffer.split("\r\n")
        self._buffer = partes.pop()
        return [linha for linha in partes if linha]

    def receber_dados(self):
        return "\n".join(self.receber_linhas())

    def log_status_line(self, linha):
        if not self.diagnostics or not linha:
            return
        if " 001 " in linha:
            print(f"[TWITCH] IRC autenticado. Nick aceito como {self.nick}.")
        elif " 366 " in linha:
            print(f"[TWITCH] JOIN confirmado em #{self.channel}.")
        elif " NOTICE " in linha or " GLOBALUSERSTATE " in linha:
            if "Login authentication failed" in linha:
                print("[TWITCH ERRO] Autenticação rejeitada pela Twitch.")
            elif "Login unsuccessful" in linha:
                print(f"[TWITCH NOTICE] {linha.split(" :", 1)[-1]}")
        elif " ERROR " in linha or linha.startswith("ERROR"):
            print(f"[TWITCH ERRO IRC] {linha}")
        elif " JOIN #" in linha and self.nick + "!" in linha:
            print(f"[TWITCH] Usuário {self.nick} entrou em #{self.channel}.")

    def note_chat_message(self):
        self.last_chat_rx = time.time()
        self.chat_messages += 1

    def status_summary(self):
        agora = time.time()
        chat_age = "nunca" if not self.last_chat_rx else f"{agora - self.last_chat_rx:.0f}s"
        rx_age = f"{agora - self.last_server_rx:.0f}s"
        return f"IRC=conectado | servidor_ultimo_rx={rx_age} | chat_ultimo_rx={chat_age} | mensagens_chat={self.chat_messages}"

    @staticmethod
    def parse_privmsg(linha):
        match = _RE_PRIVMSG.match(linha)
        if not match:
            return None

        usuario = match.group("nick")
        tags = match.group("tags") or ""
        for part in tags.split(";"):
            if part.startswith("display-name=") and len(part) > 13:
                usuario = part[13:]
                break

        mensagem = match.group("msg")
        if mensagem.startswith("\x01ACTION ") and mensagem.endswith("\x01"):
            mensagem = mensagem[8:-1]
        return usuario, mensagem

    def responder_pong(self, linha=""):
        if not self.sock:
            return
        try:
            self.sock.send(b"PONG :tmi.twitch.tv\r\n")
        except Exception:
            pass

    responder_ping = responder_pong

    def enviar_mensagem(self, texto):
        texto = str(texto or "").strip()
        if not texto:
            return False
        if not self.can_send:
            print(f"[CHAT OFF] {texto}")
            return False
        if not self.sock:
            return False
        if texto[0] in "/.\\!":
            print(f"[TWITCH] Mensagem bloqueada (começa com '{texto[0]}', pode virar comando): {texto}")
            return False
        if time.time() - getattr(self, "_ultimo_envio", 0) < 3:
            print("[TWITCH] Mensagem bloqueada (envio muito rápido).")
            return False

        try:
            self._ultimo_envio = time.time()
            safe_text = texto.replace("\r", " ").replace("\n", " ")[:450]
            self.sock.send(f"PRIVMSG #{self.channel} :{safe_text}\r\n".encode("utf-8"))
            print(f"[CHAT ENVIADO] {safe_text}")
            return True
        except Exception as exc:
            print(f"[TWITCH ERRO ENVIO] {type(exc).__name__}: {exc}")
            return False

    def obter_info_stream(self):
        """Dados da live em andamento (jogo, título, espectadores). {} se offline."""
        if not self.token or not self.client_id:
            return {}
        try:
            r = requests.get(
                "https://api.twitch.tv/helix/streams",
                headers={"Authorization": f"Bearer {self.token}", "Client-Id": self.client_id},
                params={"user_login": self.channel},
                timeout=10,
            )
            r.raise_for_status()
            dados = r.json().get("data", [])
            if not dados:
                return {"live": False}
            d = dados[0]
            return {
                "live": True,
                "game_name": d.get("game_name"),
                "title": d.get("title"),
                "viewer_count": d.get("viewer_count"),
                "started_at": d.get("started_at"),
                "language": d.get("language"),
            }
        except Exception as exc:
            print(f"[TWITCH LIVE] Falha: {type(exc).__name__}: {exc}")
            return {}

    def definir_envio(self, ativo):
        self.can_send = bool(ativo)
        print(f"[TWITCH] Envio de mensagens {'LIGADO' if self.can_send else 'DESLIGADO'}.")

    def obter_perfil_canal(self):
        if not self.token or not self.client_id or not self.channel:
            return {}

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Client-Id": self.client_id,
        }
        try:
            user_response = requests.get(
                "https://api.twitch.tv/helix/users",
                headers=headers,
                params={"login": self.channel},
                timeout=10,
            )
            user_response.raise_for_status()
            user_data = user_response.json().get("data", [])
            if not user_data:
                return {}

            user = user_data[0]
            channel_response = requests.get(
                "https://api.twitch.tv/helix/channels",
                headers=headers,
                params={"broadcaster_id": user["id"]},
                timeout=10,
            )
            channel_response.raise_for_status()
            channel_data = channel_response.json().get("data", [])
            channel = channel_data[0] if channel_data else {}

            return {
                "user_id": user.get("id"),
                "login": user.get("login"),
                "display_name": user.get("display_name"),
                "description": user.get("description"),
                "created_at": user.get("created_at"),
                "profile_image_url": user.get("profile_image_url"),
                "broadcaster_type": user.get("broadcaster_type"),
                "game_name": channel.get("game_name"),
                "title": channel.get("title"),
                "language": channel.get("language"),
            }
        except Exception as exc:
            print(f"[TWITCH PERFIL] Falha: {type(exc).__name__}: {exc}")
            return {}
