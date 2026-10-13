# -*- coding: utf-8 -*-
import os
import re
import threading
import time
import uuid

from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from core.ai_engine import AIEngine
from core.audio_listener import AudioListener
from core.lol_client import LoLClient
from core.twitch_client import TwitchClient
from core.vision import StreamVision
from memory.sqlite_memory import SQLiteMemory
from persona.profile import PERSONA_CONFIG

load_dotenv()

from core.logger import iniciar_log  # noqa: E402
iniciar_log()

console = Console(force_terminal=False, color_system=None)

def _bool(nome, padrao="false"):
    return os.getenv(nome, padrao).strip().lower() in {"1", "true", "yes", "on"}

CANAL = os.getenv("TWITCH_CHANNEL", "soraakando").lower().strip()
REFLEXAO_INTERVALO = float(os.getenv("REFLECTION_INTERVAL_SECONDS", "120"))
REFLEXAO_MAX_EVENTOS = int(os.getenv("REFLECTION_MAX_EVENTS", "200"))
REFLEXAO_MIN_EVENTOS = int(os.getenv("REFLECTION_MIN_EVENTS", "3"))
STATUS_INTERVALO = float(os.getenv("STATUS_INTERVAL_SECONDS", "30"))
COOLDOWN_SEGUNDOS = float(os.getenv("BOT_COOLDOWN_SECONDS", "240"))

LIMIAR_BASE = float(os.getenv("SPEAK_MIN_SCORE", "6"))
LIMIAR_PISO = float(os.getenv("SPEAK_MIN_FLOOR", "4"))
URGE_MINUTOS = float(os.getenv("SPEAK_URGE_MINUTES", "4"))
FRESCOR_SEGUNDOS = float(os.getenv("FRESHNESS_SECONDS", "150"))

PAUSAR_AUDIO_NA_REFLEXAO = _bool("WHISPER_PAUSE_DURING_REFLECTION", "false")
CONSOLIDAR = _bool("MEMORY_CONSOLIDATE", "true")
CONSOLIDAR_A_CADA = max(1, int(os.getenv("MEMORY_CONSOLIDATE_EVERY", "3")))

IGNORADOS = {
    "nightbot", "streamelements", "moobot", "wizebot", "fossabot", "twishgamebot", "streamlabs",
    *{u.strip().lower() for u in os.getenv("IGNORED_USERS", "").split(",") if u.strip()},
}

ai = AIEngine()
lol = LoLClient()
memory = SQLiteMemory(channel=CANAL)
twitch = TwitchClient(channel=CANAL)
session_id = uuid.uuid4().hex
memory.start_session(session_id)

audio = None
vision = None
stop_event = threading.Event()
forcar_reflexao = threading.Event()
reflection_lock = threading.Lock()
reflection_running = False
last_reflection = time.time()
last_fala = time.time()
last_status_print = time.time()
limiar_manual = None
reflexoes_desde_consolidacao = 0
ultima_consolidacao_ts = time.time() - 600
mencao_pendente = None
mencao_lock = threading.Lock()

_canal_info = {}
_canal_info_ts = 0.0
_canal_info_lock = threading.Lock()

def info_canal(max_idade=180):
    global _canal_info, _canal_info_ts
    with _canal_info_lock:
        if time.time() - _canal_info_ts > max_idade:
            dados = twitch.obter_info_stream()
            if dados:
                _canal_info = dados
            _canal_info_ts = time.time()
        return dict(_canal_info)

def ignorar_mensagem(usuario, mensagem):
    usuario = str(usuario or "").lower().strip()
    texto = str(mensagem or "").strip()
    if usuario in IGNORADOS:
        return True
    if len(texto) < 2 or len(texto) > 400:
        return True
    return texto.startswith(("!", "$"))

def registrar_evento(tipo, texto, username=None):
    return memory.add_event(tipo, texto, username=username, session_id=session_id)

def on_audio(texto):
    if not stop_event.is_set():
        console.print(f"[bold red][ÁUDIO (OUVIDOS)]:[/bold red] {texto}")
        registrar_evento("audio", texto)

def on_visao(texto):
    if not stop_event.is_set():
        # StreamVision já imprime a descrição uma vez; aqui só persistimos o evento.
        registrar_evento("visao", texto)

def _termos_mencao():
    termos = {str(PERSONA_CONFIG.get("apelido", "")).lower(), str(PERSONA_CONFIG.get("nome", "")).lower()}
    if twitch.nick:
        termos.add(twitch.nick.lower())
    return {t for t in termos if len(t) >= 4}

def detectar_mencao(usuario, texto):
    baixo = texto.lower()
    return any(re.search(rf"(?<!\w)@?{re.escape(t)}(?!\w)", baixo) for t in _termos_mencao())

def limiar_atual(mencionado=False):
    if limiar_manual is not None:
        return limiar_manual
    calado_min = (time.time() - last_fala) / 60.0
    desconto = int(calado_min // URGE_MINUTOS) if URGE_MINUTOS > 0 else 0
    limiar = max(LIMIAR_PISO, LIMIAR_BASE - desconto)
    if mencionado:
        limiar = min(limiar, 4)
    return limiar

def mostrar_reflexao(resultado, eventos, tempo, cand, notes, limiar):
    # Telemetria rica e detalhada para você ver o pensamento e o humor
    humor = resultado.get('humor', 'neutro').upper()
    entendi = resultado.get('entendi') or '(nada)'
    duvida = resultado.get('duvida') or '-'
    
    linhas = [
        f"[bold magenta]Humor Atual:[/bold magenta] {humor}",
        f"[dim]Eventos analisados na janela: {len(eventos)} | Tempo de processamento: {tempo:.1f}s[/dim]",
        f"[bold]O que a IA entendeu:[/bold] {entendi}",
        f"[bold]Dúvida:[/bold] {duvida}",
        ""
    ]
    for c in resultado.get("candidatos", []):
        linhas.append(f"  • [CANDIDATO [{c['tipo']}]] nat={c['naturalidade']} rel={c['relevancia']} (score {c.get('score', '-')}): {c['texto']}")
    for n in notes:
        linhas.append(f"  [nota]: {n}")
    linhas.append(f"[bold yellow]Limiar necessário para falar:[/bold yellow] {limiar:.0f}")
    
    console.print(Panel("\n".join(linhas), title="🧠 PAINEL COGNITIVO (REFLEXÃO)", border_style="magenta"))

def decidir_e_falar(cand, limiar, mencao):
    global last_fala
    if not cand:
        return

    if cand["score"] < limiar:
        console.print(f"[dim][DECISÃO] Silêncio: melhor candidato pontuou {cand['score']} < limiar {limiar:.0f}: \"{cand['texto']}\"[/dim]")
        return

    if FRESCOR_SEGUNDOS > 0 and not mencao:
        recentes = memory.events_since(time.time() - FRESCOR_SEGUNDOS)
        if not ai.candidato_ainda_vale(cand, recentes):
            console.print(f"[dim][DECISÃO] Silêncio: o assunto já passou: \"{cand['texto']}\"[/dim]")
            return

    espera = COOLDOWN_SEGUNDOS / (3 if mencao else 1)
    agora = time.time()
    if agora - last_fala < espera:
        console.print(f"[dim][COOLDOWN] {espera - (agora - last_fala):.0f}s restantes. Frase segurada: \"{cand['texto']}\"[/dim]")
        return

    titulo = "🤖 MENSAGEM ENVIADA AO CHAT" if twitch.can_send else "[ENVIO DESLIGADO] MENSAGEM QUE SERIA ENVIADA"
    console.print(Panel(f"[bold green]{cand['texto']}[/bold green]\n\n[dim](score: {cand['score']} | gancho: {cand['gancho'] or '-'})[/dim]", title=titulo, border_style="green"))

    if twitch.can_send:
        if twitch.enviar_mensagem(cand["texto"]):
            last_fala = agora
            memory.add_bot_message(cand["texto"], session_id)
    else:
        last_fala = agora
        memory.add_bot_message(cand["texto"], session_id)

def consolidar_se_chegou_a_hora(cutoff):
    global reflexoes_desde_consolidacao, ultima_consolidacao_ts
    if not CONSOLIDAR:
        return
    reflexoes_desde_consolidacao += 1
    if reflexoes_desde_consolidacao < CONSOLIDAR_A_CADA:
        return

    try:
        eventos = memory.events_since(ultima_consolidacao_ts)[-200:]
        mem = ai.consolidar(eventos)
    except Exception as exc:
        console.print(f"[red][MEMÓRIA] Consolidação falhou: {exc}[/red]")
        return

    for pessoa in mem["pessoas"]:
        memory.update_person(pessoa["usuario"], {"fatos": pessoa["fatos"], "interesses": pessoa["interesses"]})
    for m in mem["memorias"]:
        memory.add_memory(m["tipo"], m["sujeito"], m["conteudo"], m["importancia"], m["confianca"])
    
    console.print(f"[bold blue][MEMÓRIA CONSOLIDADA][/bold blue] Aprendidos {len(mem['pessoas'])} perfis e {len(mem['memorias'])} fatos.")
    reflexoes_desde_consolidacao = 0
    ultima_consolidacao_ts = cutoff

def processar_reflexao():
    global reflection_running, last_reflection, last_fala, mencao_pendente

    with reflection_lock:
        if reflection_running:
            return
        reflection_running = True

    with mencao_lock:
        mencao = mencao_pendente
        mencao_pendente = None

    if audio is not None and PAUSAR_AUDIO_NA_REFLEXAO:
        audio.pausar()

    try:
        cutoff = time.time()
        eventos = memory.get_unprocessed_events(limit=REFLEXAO_MAX_EVENTOS, before_ts=cutoff)
        eventos = [e for e in eventos if e["event_type"] in {"chat", "audio", "visao"}]

        if len(eventos) < REFLEXAO_MIN_EVENTOS and not mencao:
            last_reflection = time.time() - REFLEXAO_INTERVALO + 20
            return

        console.print(f"[magenta][REFLEXÃO][/magenta] Processando {len(eventos)} eventos acumulados...")
        inicio = time.perf_counter()

        contexto = memory.build_context(eventos)
        estilo = memory.recent_chat_texts(limit=14, exclude=IGNORADOS | {twitch.nick})
        emotes = memory.emote_candidates()
        recentes_bot = memory.recent_bot_messages(6)
        canal = info_canal()

        args = dict(
            canal=canal, estilo=estilo, emotes=emotes, recentes_bot=recentes_bot, mencao=mencao,
            lol=lol.contexto(canal),
        )
        resultado = ai.refletir(eventos, contexto, **args)

        if len(resultado.get("entendi", "")) >= 30:
            memory.add_memory("episodio", "streamer", resultado["entendi"], 0.4, 0.7)

        memory.mark_processed([e["id"] for e in eventos])

        cand, notas = ai.escolher_candidato(
            resultado["candidatos"], recentes_bot, estilo, eventos_recentes=eventos
        )
        limiar = limiar_atual(bool(mencao))
        tempo = time.perf_counter() - inicio
        
        mostrar_reflexao(resultado, eventos, tempo, cand, notas, limiar)
        decidir_e_falar(cand, limiar, mencao)
        consolidar_se_chegou_a_hora(cutoff)

    except Exception as exc:
        console.print(f"[red][REFLEXÃO ERRO] {exc}[/red]")
    finally:
        last_reflection = time.time()
        with reflection_lock:
            reflection_running = False
        if audio is not None and PAUSAR_AUDIO_NA_REFLEXAO:
            audio.retomar()

def scheduler_reflexao():
    print(f"[COGNIÇÃO] Scheduler ativo. Reflexão a cada {REFLEXAO_INTERVALO:.0f}s.")
    while not stop_event.is_set():
        pronto = forcar_reflexao.is_set() or (time.time() - last_reflection >= REFLEXAO_INTERVALO)
        if pronto:
            with reflection_lock:
                ocupado = reflection_running
            if not ocupado:
                forcar_reflexao.clear()
                threading.Thread(target=processar_reflexao, name="Reflection", daemon=True).start()
        stop_event.wait(1.0)

def comandos():
    global limiar_manual
    AJUDA = (
        "/envio on|off - Liga/desliga envio real no chat\n"
        "/status - Mostra estado atual\n"
        "/forcar - Força uma reflexão imediata\n"
        "/limiar N|auto - Altera limiar de fala\n"
        "/ajuda - Mostra esta lista"
    )
    sessao = PromptSession()
    while not stop_event.is_set():
        try:
            # patch_stdout redesenha "bot> " mesmo quando outras threads
            # imprimem logs; as mensagens continuam visíveis normalmente.
            with patch_stdout():
                linha = sessao.prompt("bot> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not linha.startswith("/"):
            continue
        partes = linha.lower().split()
        cmd, arg = partes[0], (partes[1] if len(partes) > 1 else "")

        if cmd == "/envio":
            if arg in {"on", "off"}:
                twitch.definir_envio(arg == "on")
            else:
                console.print(f"[CMD] Envio está {'LIGADO' if twitch.can_send else 'DESLIGADO'}.")
        elif cmd == "/status":
            st = memory.stats()
            console.print(f"[CMD] envio={'ON' if twitch.can_send else 'OFF'} | limiar={limiar_atual():.0f} | calado há {(time.time() - last_fala)/60:.1f}min | memória={st}")
        elif cmd == "/forcar":
            forcar_reflexao.set()
            console.print("[CMD] Reflexão forçada agendada.")
        elif cmd == "/memoria":
            st = memory.stats()
            console.print(
                f"[bold cyan][MEMÓRIA][/bold cyan] eventos={st['eventos']} | pendentes={st['pendentes']} | "
                f"pessoas={st['pessoas']} | fatos={st['memorias']} | episódios={st['episodios']}"
            )
            fatos = memory.top_memories(8)
            if fatos:
                for fato in fatos:
                    console.print(f"  • [{fato['memory_type']}] {fato['subject']}: {fato['content']}")
            else:
                console.print("[MEMÓRIA] Ainda não há fatos consolidados.")
        elif cmd == "/lol":
            console.print(lol.diagnostico())
        elif cmd == "/limiar":
            if arg == "auto":
                limiar_manual = None
                console.print("[CMD] Limiar automático.")
            else:
                try:
                    limiar_manual = float(arg)
                    console.print(f"[CMD] Limiar fixado em {limiar_manual:g}.")
                except ValueError:
                    console.print("[CMD] Use /limiar 6 ou /limiar auto.")
        else:
            console.print(AJUDA)

def atualizar_perfil_streamer():
    perfil = twitch.obter_perfil_canal()
    if perfil:
        memory.set_streamer_profile(perfil)
        print(f"[TWITCH] Perfil atualizado: {perfil.get('display_name')} | Jogo: {perfil.get('game_name')}")

def main():
    global audio, vision, last_status_print, mencao_pendente

    console.print(Panel("TwitchLlamaBot v3 - Rodando com Telemetria Completa", title="Soraka Que Não Cura", border_style="cyan"))
    console.print("[dim]Comandos locais: /ajuda | /status | /memoria | /lol | /forcar | /envio on|off | /limiar N|auto[/dim]")
    
    if not ai.llm.ativo:
        console.print("[red]Nenhum modelo configurado no .env.[/red]")
        return
    ai.llm.verificar()
    lol.verificar()

    twitch.start()
    atualizar_perfil_streamer()

    audio = AudioListener(CANAL, on_transcription=on_audio)
    audio.iniciar_escuta()

    vision = StreamVision(CANAL, LLMClientVisao(), on_visao, info_canal=info_canal)
    vision.start()
    if not vision.ativo:
        console.print("[yellow][VISÃO] Inativa: confira VISION_ENABLED, Pillow e VISION_CHAIN no .env.[/yellow]")

    threading.Thread(target=scheduler_reflexao, name="Scheduler", daemon=True).start()
    threading.Thread(target=comandos, name="Comandos", daemon=True).start()

    print("[BOT] Operacional. Monitorando chat, áudio e tela em tempo real.")

    try:
        while not stop_event.is_set():
            for linha in twitch.receber_linhas():
                twitch.log_status_line(linha)
                if linha.startswith("PING"):
                    twitch.responder_pong(linha)
                    continue

                parsed = twitch.parse_privmsg(linha)
                if not parsed:
                    continue

                usuario, mensagem = parsed
                if ignorar_mensagem(usuario, mensagem):
                    continue
                if twitch.nick and usuario.lower() == twitch.nick.lower():
                    continue

                twitch.note_chat_message()
                console.print(f"[bold blue][CHAT (OLHOS)][/bold blue] [yellow]{usuario}[/yellow]: {mensagem}")
                registrar_evento("chat", mensagem, username=usuario)

                if detectar_mencao(usuario, mensagem):
                    with mencao_lock:
                        mencao_pendente = {"usuario": usuario, "texto": mensagem}
                    forcar_reflexao.set()
                    console.print(f"[yellow][MENÇÃO] {usuario} chamou o bot. Reflexão antecipada acionada![/yellow]")

            agora = time.time()
            if agora - last_status_print >= STATUS_INTERVALO:
                if not reflection_running:
                    prox = max(0.0, REFLEXAO_INTERVALO - (agora - last_reflection))
                    print(f"[STATUS] Próxima reflexão em {prox:.0f}s | Limiar atual: {limiar_atual():.0f}")
                last_status_print = agora

            time.sleep(0.03)

    except KeyboardInterrupt:
        print("\n[MAIN] Encerrando com segurança...")
    finally:
        stop_event.set()
        for nome, fn in (("visão", lambda: vision and vision.stop()),
                         ("áudio", lambda: audio and audio.parar()),
                         ("Twitch", twitch.stop)):
            try:
                fn()
            except Exception:
                pass
        try:
            memory.end_session(session_id)
        except Exception:
            pass
        print("[BOT] Encerrado.")

def LLMClientVisao():
    from core.llm_client import LLMClient
    return LLMClient("VISION_CHAIN", padrao="", rotulo="VISÃO")

if __name__ == "__main__":
    main()