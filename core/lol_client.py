# -*- coding: utf-8 -*-
"""
LoL (opcional): dados OFICIAIS da Riot API sobre a conta do streamer, para o bot
saber o que está acontecendo no jogo: se está em partida agora (campeão, fila,
tempo, times), o elo ranqueado e as últimas partidas.

Liga sozinho quando existem RIOT_API_KEY e LOL_RIOT_ID (Nome#TAG) no .env.
Tudo é cacheado: são poucas chamadas por hora, bem abaixo do limite da chave.
Nunca levanta exceção para o resto do bot: se algo falhar, devolve texto vazio.
"""
import os
import threading
import time
from urllib.parse import quote

import requests


_TIMEOUT = 8

# plataforma (onde a conta joga) -> região de roteamento (Account-V1 / Match-V5)
_ROTEAMENTO = {
    "br1": "americas", "na1": "americas", "la1": "americas", "la2": "americas",
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe", "me1": "europe",
    "kr": "asia", "jp1": "asia",
    "oc1": "sea", "ph2": "sea", "sg2": "sea", "th2": "sea", "tw2": "sea", "vn2": "sea",
}

_ELO = {
    "IRON": "Ferro", "BRONZE": "Bronze", "SILVER": "Prata", "GOLD": "Ouro", "PLATINUM": "Platina",
    "EMERALD": "Esmeralda", "DIAMOND": "Diamante", "MASTER": "Mestre",
    "GRANDMASTER": "Grão-Mestre", "CHALLENGER": "Desafiante",
}
_ELO_SEM_DIVISAO = {"MASTER", "GRANDMASTER", "CHALLENGER"}

_FILAS = {
    0: "Personalizada", 400: "Normal Draft", 430: "Normal Blind", 420: "Ranqueada Solo/Duo",
    440: "Ranqueada Flex", 450: "ARAM", 480: "Swiftplay", 490: "Normal Quickplay", 700: "Clash",
    830: "Co-op vs IA", 840: "Co-op vs IA", 850: "Co-op vs IA", 900: "ARURF", 1700: "Arena",
    1900: "URF",
}
_POSICOES = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Suporte"}


def _bool(nome, padrao="false"):
    return os.getenv(nome, padrao).strip().lower() in {"1", "true", "yes", "on"}


def _num(nome, padrao, tipo=float):
    try:
        return tipo(os.getenv(nome, str(padrao)).strip())
    except ValueError:
        return tipo(padrao)


def _ha(segundos):
    segundos = max(0, int(segundos))
    if segundos < 3600:
        return f"há {max(1, segundos // 60)} min"
    if segundos < 86400:
        return f"há {segundos // 3600} h"
    return f"há {segundos // 86400} d"


class LoLClient:
    def __init__(self):
        self.key = os.getenv("RIOT_API_KEY", "").strip().strip("\"'")
        self.platform = os.getenv("LOL_REGION", "br1").strip().lower()
        self.regiao = _ROTEAMENTO.get(self.platform, "americas")
        # Account-V1 só existe em americas/asia/europe
        self.regiao_conta = "asia" if self.regiao == "sea" else self.regiao
        self.n_partidas = max(0, min(10, _num("LOL_MATCHES", 5, int)))
        self.ttl_ao_vivo = _num("LOL_LIVE_REFRESH_SECONDS", 60)
        self.ttl_perfil = _num("LOL_PROFILE_REFRESH_SECONDS", 600)
        self.atraso_spoiler = _num("LOL_SPOILER_DELAY_SECONDS", 120)
        self.so_se_lol = _bool("LOL_ONLY_WHEN_PLAYING_LOL", "true")
        self.lang = os.getenv("LOL_LANG", "pt_BR").strip() or "pt_BR"

        bruto = os.getenv("LOL_RIOT_ID", "").strip().strip("\"'")
        self.nome, self.tag = "", ""
        if "#" in bruto:
            nome, tag = bruto.rsplit("#", 1)
            self.nome, self.tag = nome.strip(), tag.strip()
        self.nome_exibicao = f"{self.nome}#{self.tag}" if self.nome else ""

        self.motivo_off = ""
        if not _bool("LOL_ENABLED", "true"):
            self.motivo_off = "LOL_ENABLED=false"
        elif not bruto:
            self.motivo_off = "LOL_RIOT_ID está vazio"
        elif not (self.nome and self.tag):
            self.motivo_off = (
                "LOL_RIOT_ID precisa ser Nome#TAG (só o nickname não basta). "
                'Se o nome tiver espaço, use aspas no .env: LOL_RIOT_ID="Nome Aqui#BR1"'
            )
        elif not self.key:
            self.motivo_off = "falta RIOT_API_KEY"
        elif self.platform not in _ROTEAMENTO:
            self.motivo_off = f"LOL_REGION desconhecida: {self.platform} (ex.: br1, na1, euw1, kr)"
        self.ativo = not self.motivo_off

        self.puuid = ""
        self._conta_prox = 0.0
        self._perfil_prox = 0.0
        self._vivo_prox = 0.0
        self._bloq_ate = 0.0
        self._chave_ruim = False
        self._elos = None
        self._ids_partidas = []
        self._partidas = {}
        self._vivo = None
        self._vivo_ts = 0.0
        self._campeoes = {}
        self._camp_prox = 0.0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _recarregar_chave(self):
        """A chave de desenvolvimento expira a cada 24h. Se você colar uma nova no
        .env, o bot pega sozinho, sem precisar reiniciar."""
        try:
            from dotenv import dotenv_values, find_dotenv

            nova = (dotenv_values(find_dotenv(usecwd=True)).get("RIOT_API_KEY") or "").strip().strip("\"'")
        except Exception:
            return False
        if nova and nova != self.key:
            self.key = nova
            self._chave_ruim = False
            self._conta_prox = self._perfil_prox = self._vivo_prox = 0.0  # tenta tudo de novo já
            print("[LOL] Chave nova detectada no .env. Tentando de novo.")
            return True
        return False

    def _get(self, host, caminho, params=None):
        """-> (status, json|None). status None = falha de rede/timeout."""
        if self._chave_ruim and not self._recarregar_chave():
            return 401, None
        if time.time() < self._bloq_ate:
            return 429, None
        try:
            r = requests.get(
                f"https://{host}.api.riotgames.com{caminho}",
                headers={"X-Riot-Token": self.key},
                params=params,
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            print(f"[LOL] Falha de rede: {type(exc).__name__}")
            return None, None

        if r.status_code == 200:
            try:
                return 200, r.json()
            except ValueError:
                return None, None
        if r.status_code in (401, 403):
            if not self._chave_ruim:
                self._chave_ruim = True
                print(
                    "[LOL] A Riot recusou a chave (401/403). Chaves de desenvolvimento expiram em 24h: "
                    "gere outra em developer.riotgames.com e cole em RIOT_API_KEY no .env "
                    "(o bot lê sozinho). Enquanto isso o bot segue sem dados de LoL."
                )
            return r.status_code, None
        if r.status_code == 429:
            try:
                espera = float(r.headers.get("Retry-After", 10))
            except ValueError:
                espera = 10.0
            espera = max(2.0, min(espera, 120.0))
            self._bloq_ate = time.time() + espera
            print(f"[LOL] Limite de requisições da Riot. Pausando {espera:.0f}s.")
            return 429, None
        return r.status_code, None

    # ------------------------------------------------------------------
    # DADOS
    # ------------------------------------------------------------------
    def _garantir_conta(self):
        if self.puuid:
            return True
        if time.time() < self._conta_prox:
            return False
        status, d = self._get(
            self.regiao_conta,
            f"/riot/account/v1/accounts/by-riot-id/{quote(self.nome, safe='')}/{quote(self.tag, safe='')}",
        )
        if status == 200 and isinstance(d, dict) and d.get("puuid"):
            self.puuid = d["puuid"]
            self.nome_exibicao = f"{d.get('gameName') or self.nome}#{d.get('tagLine') or self.tag}"
            print(f"[LOL] Conta encontrada: {self.nome_exibicao} ({self.platform.upper()}).")
            return True
        if status == 404:
            print(f"[LOL] Riot ID não encontrado: {self.nome}#{self.tag}. Confira o Nome#TAG no .env.")
        self._conta_prox = time.time() + 300
        return False

    def _carregar_campeoes(self):
        """id numérico -> nome do campeão (Data Dragon, público, sem chave)."""
        if self._campeoes or time.time() < self._camp_prox:
            return
        self._camp_prox = time.time() + 600
        try:
            r = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=_TIMEOUT)
            r.raise_for_status()
            versao = r.json()[0]
            r = requests.get(
                f"https://ddragon.leagueoflegends.com/cdn/{versao}/data/{self.lang}/champion.json",
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            self._campeoes = {int(c["key"]): c["name"] for c in r.json()["data"].values()}
        except Exception as exc:
            print(f"[LOL] Não consegui a lista de campeões (Data Dragon): {type(exc).__name__}")

    def _nome_campeao(self, cid, alternativo=""):
        try:
            nome = self._campeoes.get(int(cid))
        except (TypeError, ValueError):
            nome = None
        return nome or alternativo or f"campeão #{cid}"

    @staticmethod
    def _nome_fila(fila, modo=""):
        if fila in _FILAS:
            return _FILAS[fila]
        return f"modo {modo}" if modo else f"fila {fila}"

    def _atualizar_perfil(self):
        if time.time() < self._perfil_prox:
            return
        self._perfil_prox = time.time() + self.ttl_perfil
        status, d = self._get(self.platform, f"/lol/league/v4/entries/by-puuid/{self.puuid}")
        if status == 200 and isinstance(d, list):
            self._elos = d
        if self.n_partidas > 0:
            self._atualizar_partidas()

    def _atualizar_partidas(self):
        status, ids = self._get(
            self.regiao, f"/lol/match/v5/matches/by-puuid/{self.puuid}/ids",
            {"start": 0, "count": self.n_partidas},
        )
        if status != 200 or not isinstance(ids, list):
            return
        self._ids_partidas = ids
        for mid in ids:
            if mid in self._partidas:
                continue  # só busca partida nova
            status, m = self._get(self.regiao, f"/lol/match/v5/matches/{mid}")
            if status == 200 and isinstance(m, dict):
                resumo = self._resumir_partida(m)
                if resumo:
                    self._partidas[mid] = resumo
            elif status in (401, 403, 429):
                break
        for velho in set(self._partidas) - set(ids):
            del self._partidas[velho]

    def _resumir_partida(self, m):
        info = m.get("info") or {}
        eu = next((p for p in info.get("participants", []) if p.get("puuid") == self.puuid), None)
        if not eu:
            return None
        dur = info.get("gameDuration") or 0
        if dur > 100000:  # partidas muito antigas vinham em milissegundos
            dur //= 1000
        fim_ms = info.get("gameEndTimestamp") or ((info.get("gameStartTimestamp") or 0) + dur * 1000)
        return {
            "fim": fim_ms / 1000.0 if fim_ms else 0.0,
            "venceu": bool(eu.get("win")),
            "remake": bool(eu.get("gameEndedInEarlySurrender")) or dur < 300,
            "cid": eu.get("championId"),
            "cnome": eu.get("championName", ""),
            "pos": _POSICOES.get(eu.get("teamPosition"), ""),
            "k": eu.get("kills", 0), "d": eu.get("deaths", 0), "a": eu.get("assists", 0),
            "min": round(dur / 60),
            "fila": self._nome_fila(info.get("queueId"), info.get("gameMode", "")),
        }

    def _atualizar_ao_vivo(self):
        if time.time() < self._vivo_prox:
            return
        self._vivo_prox = time.time() + self.ttl_ao_vivo
        status, d = self._get(self.platform, f"/lol/spectator/v5/active-games/by-summoner/{self.puuid}")
        if status == 200 and isinstance(d, dict):
            self._vivo = self._resumir_vivo(d)
            self._vivo_ts = time.time()
        elif status == 404:  # 404 = não está em partida
            if self._vivo is not None:
                # acabou de terminar: busca a partida nova depois do atraso anti-spoiler
                self._perfil_prox = min(self._perfil_prox, time.time() + self.atraso_spoiler + 30)
            self._vivo = None
            self._vivo_ts = time.time()

    def _resumir_vivo(self, d):
        gente = d.get("participants") or []
        eu = next((p for p in gente if p.get("puuid") == self.puuid), None)
        meu_time = eu.get("teamId") if eu else None
        return {
            "fila": self._nome_fila(d.get("gameQueueConfigId"), d.get("gameMode", "")),
            "inicio": (d.get("gameStartTime") or 0) / 1000.0,
            "duracao": d.get("gameLength") or 0,
            "cid": eu.get("championId") if eu else None,
            "time": meu_time,
            "aliados": [p.get("championId") for p in gente if eu and p.get("teamId") == meu_time and p is not eu],
            "inimigos": [p.get("championId") for p in gente if eu and p.get("teamId") != meu_time],
            "todos": [] if eu else [p.get("championId") for p in gente],
        }

    # ------------------------------------------------------------------
    # TEXTO PARA O PROMPT
    # ------------------------------------------------------------------
    def _linha_elo(self):
        if self._elos is None:
            return ""
        partes = []
        for fila, rotulo in (("RANKED_SOLO_5x5", "Solo/Duo"), ("RANKED_FLEX_SR", "Flex")):
            e = next((x for x in self._elos if x.get("queueType") == fila), None)
            if not e:
                if fila == "RANKED_SOLO_5x5":
                    partes.append("Solo/Duo: sem ranque")
                continue
            tier = str(e.get("tier") or "")
            elo = _ELO.get(tier, tier.title())
            if tier not in _ELO_SEM_DIVISAO and e.get("rank"):
                elo += f" {e['rank']}"
            v, d = e.get("wins", 0), e.get("losses", 0)
            wr = round(100 * v / (v + d)) if (v + d) else 0
            txt = f"{rotulo}: {elo} {e.get('leaguePoints', 0)} PDL ({v}V {d}D, {wr}%)"
            if e.get("hotStreak"):
                txt += " [sequência de vitórias]"
            partes.append(txt)
        return "Elo: " + " | ".join(partes) if partes else ""

    def _linha_ao_vivo(self, agora):
        if not self._vivo_ts or agora - self._vivo_ts > 600:
            return ""  # dado velho demais: melhor não afirmar nada
        v = self._vivo
        if v is None:
            return "Agora: sem partida em andamento (pode estar em lobby, fila ou seleção de campeões)."
        if v["inicio"] > 0:
            seg = max(0, agora - v["inicio"])
        else:
            seg = v["duracao"]
        tempo = f"~{round(seg / 60)} min de jogo" if seg >= 60 else "começando agora"
        txt = f"Agora: EM PARTIDA ({v['fila']}), {tempo}."
        if v["cid"] is not None:
            lado = {100: "azul", 200: "vermelho"}.get(v["time"], "")
            txt += f" Ele joga de {self._nome_campeao(v['cid'])}" + (f" (lado {lado})." if lado else ".")
            if v["aliados"]:
                txt += " Aliados: " + ", ".join(self._nome_campeao(c) for c in v["aliados"]) + "."
            if v["inimigos"]:
                txt += " Inimigos: " + ", ".join(self._nome_campeao(c) for c in v["inimigos"]) + "."
        elif v["todos"]:
            txt += " Campeões na partida: " + ", ".join(self._nome_campeao(c) for c in v["todos"]) + "."
        return txt + " (o tempo é aproximado)"

    def _linhas_partidas(self, agora):
        todas = [self._partidas[i] for i in self._ids_partidas if i in self._partidas]
        # anti-spoiler: a live tem atraso, então esconde partida que acabou de terminar
        visiveis = [p for p in todas if not (p["fim"] and agora - p["fim"] < self.atraso_spoiler)]
        if not visiveis:
            return []
        linhas = ["Últimas partidas (mais recente primeiro):"]
        for p in visiveis:
            res = "REMAKE" if p["remake"] else ("VITÓRIA" if p["venceu"] else "DERROTA")
            pos = f" ({p['pos']})" if p["pos"] else ""
            quando = f" | {_ha(agora - p['fim'])}" if p["fim"] else ""
            linhas.append(
                f"- {res} | {self._nome_campeao(p['cid'], p['cnome'])}{pos} "
                f"{p['k']}/{p['d']}/{p['a']} | {p['fila']}, {p['min']} min{quando}"
            )
        validas = [p for p in visiveis if not p["remake"]]
        if validas:
            seq = 1
            while seq < len(validas) and validas[seq]["venceu"] == validas[0]["venceu"]:
                seq += 1
            v = sum(1 for p in validas if p["venceu"])
            resumo = f"Saldo: {v}V {len(validas) - v}D."
            if seq >= 2:
                resumo += f" Sequência atual: {seq} {'vitórias' if validas[0]['venceu'] else 'derrotas'} seguidas."
            linhas.append(resumo)
        return linhas

    def _montar(self):
        agora = time.time()
        linhas = [
            "LOL DO STREAMER (dados oficiais da Riot, confiáveis. Use só se encaixar no que está "
            "rolando agora; nunca recite números nem liste partidas):",
            f"Conta: {self.nome_exibicao} ({self.platform.upper()})",
        ]
        for linha in (self._linha_ao_vivo(agora), self._linha_elo()):
            if linha:
                linhas.append(linha)
        linhas.extend(self._linhas_partidas(agora))
        return "\n".join(linhas)[:1800] if len(linhas) > 2 else ""

    # ------------------------------------------------------------------
    # API PÚBLICA
    # ------------------------------------------------------------------
    def contexto(self, canal=None, forcar=False):
        """Bloco de texto para o prompt. '' se desligado, sem dados, ou se a live não é de LoL."""
        if not self.ativo:
            return ""
        if (
            self.so_se_lol and not forcar and canal and canal.get("live")
            and canal.get("game_name") and "league of legends" not in canal["game_name"].lower()
        ):
            return ""
        try:
            with self._lock:
                if self._chave_ruim:
                    self._recarregar_chave()  # se achar chave nova, zera os temporizadores
                self._carregar_campeoes()
                if not self._garantir_conta():
                    return ""
                if forcar:
                    self._perfil_prox = self._vivo_prox = 0.0
                self._atualizar_perfil()
                self._atualizar_ao_vivo()
                return self._montar()
        except Exception as exc:
            print(f"[LOL ERRO] {type(exc).__name__}: {exc}")
            return ""

    def verificar(self):
        """Chamado na partida do bot: confirma chave e Riot ID e avisa se algo estiver errado."""
        if not self.ativo:
            if self.key or os.getenv("LOL_RIOT_ID", "").strip():
                print(f"[LOL] Desligado: {self.motivo_off}.")
            return
        with self._lock:
            self._garantir_conta()

    def diagnostico(self):
        """Para o comando /lol: força atualização e mostra exatamente o que o bot sabe."""
        if not self.ativo:
            return f"[LOL] Desligado: {self.motivo_off}."
        texto = self.contexto(forcar=True)
        return texto or "[LOL] Sem dados. Veja as mensagens [LOL] acima (chave, Riot ID ou região)."

    def resumo(self):
        if not self.ativo:
            return f"desligado ({self.motivo_off.split('.')[0]})"
        if self._chave_ruim:
            estado = "chave recusada"
        else:
            estado = "conta ok" if self.puuid else "conta ainda não resolvida"
        return f"{self.nome_exibicao}@{self.platform} | {estado}"
