# TwitchLlamaBot v3

## Como atualizar
1. Copie estes arquivos por cima da sua pasta (seus `.db` e `.env` ficam como estão).
2. `pip install -r requirements.txt` (novo: `pillow`)
3. `python atualizar_env.py` (acrescenta as variáveis novas ao seu `.env` sem mexer nas suas)
4. Pegue uma chave grátis em console.groq.com/keys e ponha em `GROQ_API_KEY`
5. `python soraka_que_nao_cura.py`

Sem `GROQ_API_KEY` o bot continua funcionando só com Ollama + Whisper local.

## Comandos no terminal (sem limpar a tela)
`/envio on|off` · `/status` · `/forcar` · `/limiar N|auto` · `/memoria` · `/lol` · `/ajuda`

## O que mudou
- Chat e áudio NÃO são mais descartados durante a reflexão.
- Cadeia de modelos com fallback automático (`LLM_CHAIN`).
- Ouvidos: Whisper large-v3-turbo na nuvem (fallback local). Sem hotwords de LoL.
- Olhos (opcional, `VISION_ENABLED=true`): 1 quadro da live a cada 2 min descrito por modelo de visão.
- O modelo propõe até 3 mensagens com nota; o código decide enviar (limiar que cai com o silêncio).
- Filtros anti-bot: cara de narrador, repetição, assunto já velho, comando (`/`, `!`).
- Memória que aprende: fatos validados contra o texto, episódios, estilo real do chat.
- Corrigido: perfil do streamer era o da conta do bot.

## League of Legends (Riot API, opcional)
O bot passa a saber, com dados oficiais, se o streamer está em partida (campeão, fila, tempo, times), o elo e as últimas partidas.

1. `python atualizar_env.py` (acrescenta as variáveis `RIOT_API_KEY`, `LOL_*` ao seu `.env`)
2. Pegue a chave grátis em developer.riotgames.com (Development API Key; expira em 24h, o bot relê o `.env` sozinho).
3. No `.env`: `LOL_RIOT_ID=Nome#TAG` (Riot ID do streamer) e `LOL_REGION=br1`.
4. Rode o bot e digite `/lol` para ver exatamente o que ele sabe.

Trocou `TWITCH_CHANNEL`? Troque `LOL_RIOT_ID` também. Sem `RIOT_API_KEY` ou `LOL_RIOT_ID` o bot funciona como antes.
