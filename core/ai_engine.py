# -*- coding: utf-8 -*-
import json
import os
import re
import time
import unicodedata

from core.llm_client import LLMClient
from core.web_search import WebSearch
from persona.profile import PERSONA_CONFIG


_STOP = set(
    "a o e é de da do das dos em um uma que pra para com não nao mais mas por se eu ele ela "
    "tá ta vai foi são ser ter tem isso esse essa aqui lá já muito bem só né então como "
    "quando onde porque pq tudo você voce vc meu minha seu sua cara mano tipo".split()
)

# Jeitos de escrever que denunciam um bot / narrador.
_PADROES_ROBOTICOS = [
    r"^(o|a) streamer\b",
    r"^parece que\b",
    r"^interessante\b",
    r"^(olá|ola|oi pessoal|boa noite pessoal|boa tarde pessoal)\b",
    r"\bcomo (uma )?(ia|inteligência artificial|assistente|bot)\b",
    r"\bsou (uma )?(ia|bot)\b",
    r"\b(modelo de linguagem|inteligência artificial)\b",
    r"https?://",
    r"^(acho que|eu acho que) (o|a) (streamer|chat)\b",
]


def _norm(texto):
    t = unicodedata.normalize("NFKD", str(texto or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9@_ ]+", " ", t)


def _tokens(texto, minimo=4):
    return {t for t in _norm(texto).split() if len(t) >= minimo and t not in _STOP}


def _clamp_nota(valor):
    try:
        return max(0, min(10, int(round(float(valor)))))
    except Exception:
        return 0


class AIEngine:
    def __init__(self):
        self.llm = LLMClient("LLM_CHAIN", padrao=f"ollama:{os.getenv('OLLAMA_MODEL', 'qwen3:8b')}")
        if os.getenv("LLM_MEMORY_CHAIN", "").strip():
            # outra cadeia = outra cota diária nas APIs gratuitas
            self.llm_mem = LLMClient("LLM_MEMORY_CHAIN", rotulo="MEM")
        else:
            self.llm_mem = self.llm
        self.web = WebSearch()
        self.verbose = os.getenv("AI_VERBOSE", "true").lower() in {"1", "true", "yes", "on"}
        self.max_tokens = int(os.getenv("REFLECT_MAX_TOKENS", os.getenv("OLLAMA_NUM_PREDICT", "1200")))
        self.max_linhas = int(os.getenv("REFLECT_MAX_LINES", "120"))
        self.humor = "neutro"

    # ------------------------------------------------------------------
    # PROMPTS
    # ------------------------------------------------------------------
    def _perfil_texto(self):
        p = PERSONA_CONFIG
        partes = [
            f"{p.get('idade', '')} anos",
            p.get("localizacao", ""),
            p.get("trabalho", ""),
            p.get("familia", ""),
        ]
        gosto = (
            f"Joga LoL ({p.get('estilo_lol', '')}) e gosta de "
            f"{', '.join(p.get('campeoes_favoritos', []))}. "
            "ATENÇÃO: o canal assistido pode NÃO ser de LoL. Só fale de LoL se a live realmente estiver nisso."
        )
        return " ".join(x for x in partes if x) + " " + gosto

    def _system(self):
        nome = PERSONA_CONFIG.get("apelido") or PERSONA_CONFIG.get("nome") or "Sorakinho"
        return f"""
Você é {nome}, um frequentador comum do chat de uma live da Twitch. Você NÃO é narrador, NÃO é assistente e NUNCA revela ser uma IA ou bot.

SOBRE VOCÊ (pano de fundo, nunca recite): {self._perfil_texto()}

COMO VOCÊ ESCREVE NO CHAT:
- Uma ideia só, de 3 a 12 palavras. Normalmente minúsculo e sem ponto final, igual ao chat real.
- Reaja ao que foi dito: piada, opinião, pergunta curta ao streamer, resposta a alguém do chat.
- Nunca descreva a cena. Nada de "parece que", "interessante", "o streamer está...", elogio genérico ou emoji unicode.
- Gíria só se combinar com o tom do chat. No máximo 1 emote, e só se ele aparecer nos exemplos do chat.
- Não repita suas mensagens recentes nem o que o streamer acabou de falar.
- Não invente fatos, nomes ou jogo. Use só o que está na janela, na tela, na memória, nos dados de LoL ou na pesquisa.

SOBRE O ÁUDIO: é transcrição automática. Tem erros, pode misturar o streamer com outras pessoas em call, vídeos que ele reage e música. Se não fizer sentido, não force interpretação: registre a dúvida.
""".strip()

    def _linhas_janela(self, eventos):
        """Linhas da janela, compactas: junta falas seguidas do mesmo usuário
        e reduz 'AAAAAAAA' para 'AAA'. Menos tokens = menos cota gasta."""
        agora = time.time()
        linhas = []
        ultimo = None  # [usuario, indice da linha, quantas já juntei]
        for e in eventos[-self.max_linhas * 2:]:
            tipo = e.get("event_type") or e.get("tipo")
            texto = (e.get("text") or e.get("texto") or "").strip()
            if not texto:
                continue
            texto = re.sub(r"(.)\1{5,}", r"\1\1\1", texto)[:220]
            marca = f"-{max(0, int(agora - float(e.get('ts') or agora)))}s"
            if tipo == "chat":
                usuario = e.get("username") or "?"
                if ultimo and ultimo[0] == usuario and ultimo[2] < 4:
                    linhas[ultimo[1]] += f" | {texto}"
                    ultimo[2] += 1
                    continue
                linhas.append(f"[{marca}] CHAT {usuario}: {texto}")
                ultimo = [usuario, len(linhas) - 1, 1]
            elif tipo == "visao":
                linhas.append(f"[{marca}] TELA: {texto}")
                ultimo = None
            elif tipo == "audio":
                linhas.append(f"[{marca}] ÁUDIO: {texto}")
                ultimo = None
        return linhas[-self.max_linhas:]

    def refletir(
        self,
        eventos,
        contexto_memoria,
        canal=None,
        estilo=None,
        emotes=None,
        recentes_bot=None,
        mencao=None,
        pesquisa=None,
        lol=None,
    ):
        vazio = {
            "entendi": "",
            "duvida": "",
            "humor": self.humor,
            "candidatos": [],
            "pesquisar": False,
            "consulta": "",
            "observacao": "",
        }
        linhas = self._linhas_janela(eventos)
        if not linhas:
            vazio["observacao"] = "Sem eventos."
            return vazio

        bloco_canal = "Sem informação do canal."
        if canal:
            bloco_canal = (
                f"Título: {canal.get('title') or '?'} | Jogo/categoria: {canal.get('game_name') or '?'} | "
                f"Espectadores: {canal.get('viewer_count', '?')}"
            )

        bloco_estilo = "\n".join(f"- {t}" for t in (estilo or [])) or "(sem exemplos ainda)"
        bloco_emotes = ", ".join(emotes or []) or "(nenhum identificado)"
        bloco_bot = "\n".join(f"- {t}" for t in (recentes_bot or [])) or "(você ainda não falou)"

        bloco_mencao = ""
        if mencao:
            bloco_mencao = (
                f"\nMENÇÃO DIRETA: {mencao.get('usuario')} falou com você: \"{mencao.get('texto')}\". "
                "Pelo menos um candidato deve responder a isso.\n"
            )

        bloco_pesquisa = ""
        if pesquisa:
            bloco_pesquisa = (
                "\nPESQUISA NA INTERNET (use só para completar o contexto, nunca cite site):\n"
                f"{self.web.format_results(pesquisa)}\n"
            )

        bloco_lol = f"\n{lol}\n" if lol else ""

        prompt = f"""
Você acompanhou esta janela da live. Entenda o que está acontecendo e proponha até 3 mensagens que um frequentador de verdade poderia mandar agora. O sistema decide se envia, então SEMPRE proponha candidatos quando houver algum gancho real, e seja crítico nas notas.

CANAL AGORA: {bloco_canal}
{bloco_lol}
MEMÓRIA:
{contexto_memoria}

COMO O CHAT ESCREVE (exemplos reais, imite tom e tamanho):
{bloco_estilo}
EMOTES COMUNS DO CANAL: {bloco_emotes}

SUAS MENSAGENS RECENTES (não repita):
{bloco_bot}
{bloco_mencao}{bloco_pesquisa}
JANELA (tempo é "quanto tempo atrás"):
{chr(10).join(linhas)}

Se aparecer termo, gíria, pessoa, jogo, produto ou notícia que você não conhece e que importa para entender, use pesquisar=true com uma consulta curta (até 8 palavras).

Notas de 0 a 10: naturalidade = um humano do chat realmente mandaria isso assim?; relevancia = encaixa no que está acontecendo agora?

Responda SOMENTE JSON:
{{
  "entendi": "até 300 caracteres: o que está acontecendo (assunto, clima, quem parece falar)",
  "duvida": "até 160 caracteres: o que não deu para entender, ou vazio",
  "humor": "neutro|animado|curioso|entediado|irritado|rindo",
  "candidatos": [
    {{"texto": "mensagem", "tipo": "reacao|pergunta|resposta|piada", "gancho": "trecho curto da janela que motivou", "naturalidade": 0, "relevancia": 0}}
  ],
  "pesquisar": false,
  "consulta": ""
}}
""".strip()

        bruto = self.llm.chat(
            [
                {"role": "system", "content": self._system()},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=self.max_tokens,
            json_mode=True,
        )
        dados = self._extrair_json(bruto)
        if not isinstance(dados, dict):
            vazio["observacao"] = "Modelo não retornou JSON válido." if bruto else "Nenhum modelo respondeu."
            return vazio

        humor = str(dados.get("humor") or self.humor).strip().lower()[:20]
        self.humor = humor or self.humor

        resultado = {
            "entendi": str(dados.get("entendi") or "").strip()[:300],
            "duvida": str(dados.get("duvida") or "").strip()[:160],
            "humor": self.humor,
            "candidatos": self._normalizar_candidatos(dados.get("candidatos")),
            "pesquisar": bool(dados.get("pesquisar")) and bool(str(dados.get("consulta") or "").strip()),
            "consulta": str(dados.get("consulta") or "").strip()[:120],
        }
        resultado["observacao"] = resultado["entendi"]

        # A pesquisa era apenas sugerida pelo prompt, mas nunca executada.
        # Quando o modelo pede uma consulta, buscamos e fazemos uma segunda
        # reflexão enriquecida com fontes. O parâmetro pesquisa impede loops.
        if resultado["pesquisar"] and not pesquisa:
            consulta = resultado["consulta"]
            print(f"[WEB] Pesquisando: {consulta}")
            resultados_web = self.web.search(consulta)
            if resultados_web:
                print(f"[WEB] {len(resultados_web)} resultado(s) encontrados:")
                for indice, item in enumerate(resultados_web, 1):
                    print(f"[WEB] {indice}. {item['title']} — {item['url']}")
                return self.refletir(
                    eventos, contexto_memoria, canal=canal, estilo=estilo,
                    emotes=emotes, recentes_bot=recentes_bot, mencao=mencao,
                    pesquisa=resultados_web, lol=lol,
                )
            print("[WEB] Nenhum resultado retornado; seguindo sem pesquisa.")
            resultado["observacao"] = (resultado["observacao"] + " | Pesquisa sem resultados").strip(" |")
        return resultado

    # ------------------------------------------------------------------
    # CANDIDATOS / DECISÃO / HUMANIZAÇÃO
    # ------------------------------------------------------------------
    @staticmethod
    def _normalizar_candidatos(brutos):
        saida = []
        if not isinstance(brutos, list):
            return saida
        for c in brutos[:3]:
            if not isinstance(c, dict):
                continue
            texto = str(c.get("texto") or "").strip()
            if not texto:
                continue
            saida.append({
                "texto": texto[:200],
                "tipo": str(c.get("tipo") or "reacao")[:12],
                "gancho": str(c.get("gancho") or "")[:140],
                "naturalidade": _clamp_nota(c.get("naturalidade")),
                "relevancia": _clamp_nota(c.get("relevancia")),
            })
        return saida

    @staticmethod
    def _repetido(texto, recentes_bot):
        a = _tokens(texto, 3)
        if not a:
            return False
        for r in recentes_bot or []:
            b = _tokens(r, 3)
            if b and len(a & b) / len(a | b) >= 0.5:
                return True
        return False

    def humanizar(self, texto, amostra_chat=None, max_palavras=14):
        t = re.sub(r"\s+", " ", str(texto or "")).strip().strip("\"“”'`")
        t = re.sub(r"https?://\S+", "", t).strip()
        t = re.sub(r"[.\s]+$", "", t)
        palavras = t.split()[:max_palavras]
        t = " ".join(palavras)
        if not t:
            return ""

        # Se o chat quase todo escreve em minúsculo, faz igual.
        amostra = [m for m in (amostra_chat or []) if m and m[0].isalpha()]
        if len(amostra) >= 5:
            minusc = sum(1 for m in amostra if m[0].islower())
            if minusc / len(amostra) >= 0.6 and not t[:2].isupper():
                t = t[0].lower() + t[1:]
        return t

    def escolher_candidato(self, candidatos, recentes_bot=None, amostra_chat=None, eventos_recentes=None):
        """Escolhe o melhor candidato aceitável e evita eco literal do chat/áudio."""
        notas = []
        melhor = None
        fontes = []
        for evento in eventos_recentes or []:
            texto_fonte = str(evento.get("text") or evento.get("texto") or "").strip()
            if texto_fonte and evento.get("event_type", evento.get("tipo")) in {"chat", "audio"}:
                fontes.append(texto_fonte)
        for c in candidatos or []:
            texto = self.humanizar(c["texto"], amostra_chat)
            if len(texto.split()) < 2 and not re.search(r"[A-Z]{3,}|\d", texto):
                notas.append(f"descartado (curto demais): {texto!r}")
                continue
            if any(re.search(p, texto.lower()) for p in _PADROES_ROBOTICOS):
                notas.append(f"descartado (cara de bot/narrador): {texto!r}")
                continue
            if self._repetido(texto, recentes_bot):
                notas.append(f"descartado (repete algo recente do bot): {texto!r}")
                continue
            normal = lambda value: " ".join(_norm(value).split())
            candidato_norm = normal(texto)
            tokens_candidato = set(candidato_norm.split())
            eco = False
            if len(tokens_candidato) >= 5:
                for fonte in fontes:
                    fonte_norm = normal(fonte)
                    tokens_fonte = set(fonte_norm.split())
                    if candidato_norm and (candidato_norm in fonte_norm or
                        (len(tokens_fonte) >= 5 and
                         len(tokens_candidato & tokens_fonte) / max(1, len(tokens_candidato | tokens_fonte)) >= 0.82)):
                        eco = True
                        break
            if eco:
                notas.append(f"descartado (eco literal de chat/áudio): {texto!r}")
                continue
            item = dict(c)
            item["texto"] = texto
            item["score"] = min(c["naturalidade"], c["relevancia"])
            if melhor is None or (item["score"], c["naturalidade"] + c["relevancia"]) > (
                melhor["score"], melhor["naturalidade"] + melhor["relevancia"]
            ):
                melhor = item
        return melhor, notas

    @staticmethod
    def candidato_ainda_vale(candidato, eventos_recentes):
        """Evita resposta velha: o gancho precisa ainda estar vivo na conversa."""
        base = _tokens(f"{candidato.get('gancho', '')} {candidato.get('texto', '')}", 4)
        if not base:
            return True
        recente = set()
        for e in eventos_recentes:
            recente |= _tokens(e.get("text") or "", 4)
        return bool(base & recente)

    # ------------------------------------------------------------------
    # CONSOLIDAÇÃO DE MEMÓRIA (chamada separada, sem "thinking")
    # ------------------------------------------------------------------
    def consolidar(self, eventos):
        linhas = self._linhas_janela(eventos)
        if len(linhas) < 3:
            return {"pessoas": [], "memorias": []}

        usuarios = {
            str(e.get("username")).lower()
            for e in eventos
            if (e.get("event_type") == "chat") and e.get("username")
        }
        texto_eventos = " ".join((e.get("text") or "") for e in eventos)
        tokens_eventos = _tokens(texto_eventos, 4)

        prompt = f"""
Extraia só o que vale a pena lembrar depois sobre o streamer e as pessoas do chat, a partir desta janela de uma live.

JANELA:
{chr(10).join(linhas)}

REGRAS:
- Só registre o que foi dito de forma explícita. Não deduza, não invente.
- Fato sobre um espectador: só se ele mesmo disse sobre si, ou o streamer disse sobre ele. Use o nome de usuário exato.
- O áudio tem erros de transcrição: ignore trechos sem sentido e letras de música.
- Ignore papo vazio e risadas. Se não houver nada útil, devolva listas vazias.
- Máximo 4 fatos por pessoa e 6 memórias no total.

Responda SOMENTE JSON:
{{
  "pessoas": [{{"usuario": "nick", "fatos": ["fato curto"], "interesses": ["interesse"]}}],
  "memorias": [{{"tipo": "fato|preferencia|piada|assunto|evento", "sujeito": "streamer|canal|nick", "conteudo": "frase curta", "importancia": 0.5, "confianca": 0.7}}]
}}
""".strip()

        bruto = self.llm_mem.chat(
            [
                {"role": "system", "content": "Você organiza a memória de longo prazo de um frequentador de live. Seja literal e conservador."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=700,
            json_mode=True,
            think=False,
        )
        dados = self._extrair_json(bruto)
        if not isinstance(dados, dict):
            return {"pessoas": [], "memorias": []}

        def ancorado(texto):
            t = _tokens(texto, 4)
            return (not t) or bool(t & tokens_eventos)

        pessoas = []
        for p in (dados.get("pessoas") or [])[:8]:
            if not isinstance(p, dict):
                continue
            nick = str(p.get("usuario") or "").strip().lower().lstrip("@")
            if nick not in usuarios:
                continue
            fatos = [
                str(f).strip()[:160] for f in (p.get("fatos") or [])
                if 6 <= len(str(f).strip()) and ancorado(str(f))
            ][:4]
            interesses = [str(i).strip()[:60] for i in (p.get("interesses") or []) if str(i).strip()][:4]
            if fatos or interesses:
                pessoas.append({"usuario": nick, "fatos": fatos, "interesses": interesses})

        tipos = {"fato", "preferencia", "piada", "assunto", "evento"}
        memorias = []
        for m in (dados.get("memorias") or [])[:10]:
            if not isinstance(m, dict):
                continue
            conteudo = str(m.get("conteudo") or "").strip()[:200]
            sujeito = str(m.get("sujeito") or "streamer").strip().lower().lstrip("@")
            try:
                conf = float(m.get("confianca", 0.6))
                imp = float(m.get("importancia", 0.5))
            except Exception:
                continue
            if len(conteudo) < 8 or conf < 0.5 or not ancorado(conteudo):
                continue
            if sujeito not in usuarios and sujeito not in {"streamer", "canal"}:
                sujeito = "streamer"
            tipo = str(m.get("tipo") or "fato").strip().lower()
            memorias.append({
                "tipo": tipo if tipo in tipos else "fato",
                "sujeito": sujeito,
                "conteudo": conteudo,
                "importancia": max(0.0, min(1.0, imp)),
                "confianca": max(0.0, min(1.0, conf)),
            })
            if len(memorias) >= 6:
                break

        return {"pessoas": pessoas, "memorias": memorias}

    # ------------------------------------------------------------------
    @staticmethod
    def _extrair_json(texto):
        if not texto:
            return None
        texto = re.sub(r"```json\s*|```", "", texto, flags=re.IGNORECASE).strip()
        try:
            return json.loads(texto)
        except Exception:
            pass
        ini, fim = texto.find("{"), texto.rfind("}")
        if ini == -1 or fim == -1:
            return None
        try:
            return json.loads(texto[ini:fim + 1])
        except Exception:
            return None
