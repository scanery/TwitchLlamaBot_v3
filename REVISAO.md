# Relatório de revisão — TwitchLlamaBot v3

Data: 13/10/2026

## Correções implementadas

1. **Áudio duplicado no shell** — `AudioListener` imprimia `[OUVIDOS] ...` e, logo depois, o callback imprimia a mesma transcrição como `[ÁUDIO (OUVIDOS)] ...`. A saída de transcrição agora é centralizada no callback. A telemetria de áudio permanece quando `WHISPER_DEBUG=true`.
2. **Comandos ausentes** — o loop aceitava comandos, mas só implementava `/envio`, `/status`, `/forcar` e `/limiar`. `/memoria` e `/lol` agora são implementados, e `/ajuda` lista todos.
3. **Prompt de shell** — `input()` puro era vulnerável à mistura visual com logs de threads. O projeto agora usa `prompt_toolkit` e `patch_stdout`, mantendo os logs visíveis e redesenhando `bot> ` após novas saídas.
4. **Eco do que foi falado** — o filtro anterior só comparava uma sugestão com mensagens anteriores do próprio bot. Agora também compara o candidato com eventos recentes de chat e áudio e rejeita ecos quase literais.
5. **Pesquisa web não executada** — o prompt podia retornar `pesquisar=true`, mas o fluxo nunca chamava `WebSearch.search()`. Agora executa a consulta, mostra `[WEB]`, injeta os resultados em uma segunda reflexão e impede uma recursão infinita. O módulo também informa no arranque se a pesquisa está ativa ou se falta `ddgs`.
6. **Olhos / visão** — o código já iniciava a visão, mas o log fornecido mostra que `qwen/qwen3.6-27b` estava indisponível. O provedor descontinuou esse ID em 14/09/2026 e recomenda `qwen/qwen3.8-27b`; o cliente agora migra automaticamente o ID antigo para o novo e `.env.example` usa o novo. A inicialização também avisa quando a visão está inativa.
7. **Higiene do pacote** — removidos do ZIP de entrega: `venv/`, `.git/`, `.env`, `.env.bak1`, bancos SQLite, WAL/SHM, logs e bytecode. Foram mantidos os fontes e adicionados `.env.example` e testes de regressão.

## Achados adicionais

- O `.env` original contém credenciais para Twitch/Groq/Riot. Não foi copiado para o pacote corrigido.
- O arquivo `riot.txt` não era referenciado pelo código-fonte incluído e foi removido da distribuição.
- O ZIP original carregava um ambiente virtual e o histórico Git junto do código; isso infla muito o arquivo e pode vazar dados antigos.
- A busca web depende de `ddgs` e de conectividade externa. Se não aparecer `[WEB] Pesquisando: ...`, o modelo não pediu pesquisa naquela reflexão; se aparecer `[WEB ERRO]`, o log agora deixa a falha explícita.
- A visão não pode ser validada contra uma live real durante uma análise estática; confirme no arranque a linha `[VISÃO] Olhos ativados ...` e, depois, `[VISÃO] ...` ou `[VISÃO ERRO] ...`.

## Validação feita

- Todos os arquivos Python compilam sem erro de sintaxe.
- Teste local do filtro confirmou que uma sugestão que ecoa áudio/chat é rejeitada e uma sugestão distinta é mantida.
- Não foram feitas chamadas reais à Twitch, Riot, Groq ou mecanismo de pesquisa; por isso a entrega não afirma ter validado credenciais, cotas, conectividade ou o comportamento em uma live real.

## Passos de atualização

1. Faça backup dos bancos `bot_memory_*.db` e do `.env` atual.
2. Extraia o ZIP corrigido sobre uma cópia do projeto (não apague os bancos locais).
3. Execute `pip install -r requirements.txt` no ambiente virtual.
4. Compare o `.env` com `.env.example`; altere `VISION_CHAIN` para `groq:qwen/qwen3.8-27b`.
5. Inicie e teste `/ajuda`, `/memoria`, `/lol` e `/forcar`.
6. Para validar pesquisa, espere um tópico desconhecido/importante e procure `[WEB] Pesquisando:`; para validar olhos, procure a descrição `[VISÃO]` depois do intervalo configurado.

**Segurança:** como o arquivo original continha credenciais e `.git`, se o ZIP foi enviado ou armazenado fora de um local privado, revogue/regenere o token OAuth da Twitch e as chaves Groq/Riot.
