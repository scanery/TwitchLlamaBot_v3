# TwitchLlamaBot v3

> **PT-BR abaixo · English version below**

Um bot experimental para Twitch que combina interação por chat, percepção de áudio da transmissão, modelos de linguagem e memória persistente para participar de lives com mais contexto e menos comportamento automatizado.

**Status:** projeto pessoal/experimental em desenvolvimento. Os resultados dependem dos modelos, dos provedores externos, da qualidade da transmissão e da configuração local. Não há garantia de disponibilidade contínua, precisão de transcrição ou autonomia conversacional.

---

## Português (Brasil)

## Visão geral

O **TwitchLlamaBot v3** é um experimento de agente conversacional para lives da Twitch. A proposta não é responder a cada mensagem nem narrar continuamente o que acontece. O bot coleta sinais do ambiente, mantém contexto, avalia possíveis intervenções e pode permanecer em silêncio quando não há motivo suficiente para falar.

A implementação reúne componentes independentes para chat, áudio, raciocínio, memória e integrações opcionais. O foco é explorar uma presença mais contextual no chat, com controle explícito sobre o envio de mensagens.

## Principais recursos

- **Chat da Twitch via IRC:** conecta-se com autenticação de usuário, acompanha mensagens e pode enviar respostas quando o envio está habilitado.
- **Percepção de áudio:** obtém o áudio público da live e transcreve trechos usando Faster-Whisper local ou um backend remoto compatível configurado no ambiente.
- **Raciocínio com LLMs:** aceita uma cadeia de modelos/provedores com fallback, incluindo Ollama local e provedores remotos suportados pelo código.
- **Memória persistente em SQLite:** armazena eventos, sessões, informações de espectadores e memórias consolidadas para apoiar interações futuras.
- **Persona configurável:** define características, interesses, estilo de conversa e regras de comportamento em `persona/profile.py`.
- **Integração opcional com a Riot API:** pode consultar informações públicas de League of Legends, como estado de partida, fila, campeão e dados recentes, conforme as permissões e os limites da API.
- **Visão opcional da transmissão:** captura quadros da própria live e solicita uma descrição a um modelo de visão compatível. Não captura a tela local do operador.
- **Pesquisa na web opcional:** pode buscar resultados textuais para contextualizar assuntos, dependendo da configuração e da disponibilidade do serviço.
- **Controles operacionais:** oferece comandos no terminal para consultar status e memória, ajustar o comportamento e controlar o envio de mensagens.

## Arquitetura

```text
Twitch Chat (IRC) ───────────────┐
                                 │
Áudio público da live → ASR ─────┤
                                 ├→ Eventos/contexto → AI Engine → avaliação de resposta
Quadros da live (opcional) ──────┤                           │
                                 │                           ├→ Memória SQLite
Riot API / pesquisa web ─────────┘                           │
                                                             └→ Twitch Chat
                                                                (se habilitado)
```

Os componentes principais ficam organizados assim:

| Caminho | Responsabilidade |
| --- | --- |
| `soraka_que_nao_cura.py` | Inicialização, coordenação dos componentes, ciclo de reflexão e comandos operacionais |
| `core/twitch_client.py` | Conexão e comunicação com o chat da Twitch |
| `core/audio_listener.py` | Captura do áudio da transmissão e transcrição |
| `core/ai_engine.py` | Contexto, prompts, avaliação e preparação de respostas |
| `core/llm_client.py` | Integração com modelos de linguagem e fallback entre backends |
| `core/vision.py` | Percepção visual opcional de quadros da transmissão |
| `core/lol_client.py` | Integração opcional com a Riot API |
| `core/web_search.py` | Pesquisa textual na web |
| `memory/sqlite_memory.py` | Persistência e recuperação de memória local |
| `persona/profile.py` | Definição da persona e das regras de comportamento |
| `atualizar_env.py` | Inclusão de novas variáveis de configuração sem sobrescrever as existentes |

## Requisitos

- Windows, Linux ou outro ambiente capaz de executar as dependências do projeto. O desenvolvimento principal foi feito em Windows.
- Python compatível com as versões exigidas pelas dependências instaladas. **Python 3.11 ou 3.12 é um ponto de partida razoável**, mas confirme a compatibilidade de `av`, `faster-whisper` e demais pacotes no seu sistema.
- Conexão com a internet para acessar a Twitch e, quando habilitados, provedores de IA, pesquisa web e Riot API.
- Uma conta da Twitch e um token OAuth válido para a conta que fará a conexão.
- Para inferência local, uma instalação funcional do [Ollama](https://ollama.com/) e um modelo compatível com a configuração.
- Para transcrição local, recursos de CPU/GPU e memória suficientes para o modelo escolhido. Modelos maiores podem exigir bastante memória e processamento.
- Uma chave de API da Riot somente se a integração com League of Legends for utilizada.
- Chaves de provedores externos somente para os backends remotos que você decidir habilitar.

## Instalação

Os comandos abaixo são executados a partir da pasta raiz do projeto.

### 1. Criar o ambiente Python

```powershell
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Se o PowerShell bloquear a ativação do ambiente, consulte a política de execução do seu Windows ou use o executável diretamente em `venv\Scripts\python.exe`. Não altere políticas de segurança do sistema sem entender o impacto.

### 2. Criar a configuração local

O repositório deve conter um arquivo `.env.example`, com nomes de variáveis e valores padrão seguros, mas sem credenciais reais. **O pacote analisado não inclui esse arquivo**, embora `atualizar_env.py` dependa dele. Antes de usar esse fluxo, crie e revise um `.env.example` sanitizado a partir das variáveis que o código realmente utiliza; não copie valores secretos do seu `.env`.

Quando o modelo de configuração estiver presente, crie sua configuração local:

```powershell
Copy-Item .env.example .env
```

Preencha as variáveis necessárias no `.env`. Nunca publique esse arquivo e nunca cole tokens reais em issues, logs públicos ou capturas de tela.

Se você já tem um `.env` de uma instalação anterior e quer acrescentar variáveis novas sem substituir as existentes, execute:

```powershell
python atualizar_env.py
```

O script cria um backup `.env.bak` e acrescenta as chaves que estiverem faltando. Revise o resultado antes de iniciar o bot.

### 3. Configurar a Twitch

Configure no mínimo as variáveis de conexão presentes no `.env.example`, incluindo:

- `TWITCH_CHANNEL`: canal que o bot acompanhará, sem `#`.
- `TWITCH_USERNAME`: nome de usuário da conta autenticada, conforme necessário para a configuração.
- `TWITCH_TOKEN`: token OAuth válido da conta que fará a conexão.
- `ENABLE_CHAT_RESPONSES`: mantenha `false` durante a configuração e os testes iniciais.

O modo anônimo não é utilizado. Gere e gerencie tokens apenas por meios confiáveis e conceda somente as permissões necessárias. Não compartilhe seu token: ele pode permitir ações em nome da conta.

### 4. Configurar o modelo de linguagem

O bot pode usar Ollama local e, conforme a configuração e as chaves disponíveis, provedores remotos. As variáveis relevantes incluem `LLM_CHAIN`, `OLLAMA_URL`, `OLLAMA_MODEL` e as chaves dos provedores habilitados.

Exemplo conceitual de cadeia:

```dotenv
LLM_CHAIN=ollama:qwen3:8b
OLLAMA_URL=http://localhost:11434/api/chat
```

A cadeia aceita pelo código deve ser consultada na configuração atual do projeto. Os nomes de modelos disponíveis variam por provedor e podem mudar com o tempo. Para usar um modelo local, instale-o no Ollama antes de iniciar o bot. Para usar provedores remotos, configure as respectivas chaves no `.env`.

### 5. Configurar áudio, visão e integrações opcionais

- **Áudio:** ajuste `WHISPER_BACKEND`, `WHISPER_MODEL`, `WHISPER_DEVICE` e as variáveis relacionadas no `.env.example`. O backend local exige baixar/carregar o modelo escolhido. O backend remoto exige a chave correspondente e está sujeito a limites, latência e mudanças do serviço.
- **Visão:** `VISION_ENABLED` controla o recurso; mantenha-o desativado se não tiver configurado um backend de visão compatível. O recurso acessa quadros da transmissão pública.
- **League of Legends:** habilite a integração somente se quiser usar a Riot API. Configure `RIOT_API_KEY`, `LOL_RIOT_ID` no formato `Nome#TAG` e `LOL_REGION` conforme a região da conta.
- **Pesquisa web:** configure `WEB_SEARCH_ENABLED` e demais variáveis relacionadas conforme sua necessidade.

A lista completa e os valores padrão devem ser conferidos em `.env.example`, que é a referência da configuração desta versão.

## Primeiro teste: não envie mensagens

1. Configure `ENABLE_CHAT_RESPONSES=false`.
2. Inicie o bot:

   ```powershell
   python soraka_que_nao_cura.py
   ```

3. Confira os logs de inicialização e valide a conexão, a transcrição, a seleção dos modelos e a criação da memória.
4. Use `/status` e `/ajuda` no terminal para consultar os controles disponíveis.
5. Só habilite respostas reais depois de revisar o comportamento em uma sessão controlada.

**Importante:** o controle de envio deve permanecer desativado enquanto você valida credenciais e comportamento. Confirme o estado exibido pelo próprio processo antes de permitir mensagens reais. O modo de teste não substitui a revisão dos logs.

## Comandos de terminal

Os comandos disponíveis na versão atual incluem:

| Comando | Finalidade |
| --- | --- |
| `/ajuda` | Exibe os comandos disponíveis |
| `/status` | Mostra o estado operacional |
| `/envio on` / `/envio off` | Habilita ou desabilita o envio de mensagens durante a execução |
| `/forcar` | Solicita uma reflexão fora do ciclo normal |
| `/limiar N` / `/limiar auto` | Ajusta o limiar de decisão, quando aplicável |
| `/memoria` | Consulta informações da memória |
| `/lol` | Consulta o estado da integração com League of Legends |

Os comandos podem evoluir entre versões. Use `/ajuda` no terminal para confirmar a lista efetiva. O envio também depende da configuração inicial e do estado do cliente Twitch.

## Memória e dados locais

A memória é armazenada em bancos SQLite locais com nomes no padrão `bot_memory_<canal>.db`. O código mantém registros de eventos, sessões, perfis de espectadores e memórias consolidadas.

Esses dados podem conter nomes de usuário, mensagens de chat, transcrições e informações inferidas a partir das interações. Trate os bancos como dados potencialmente pessoais:

- não os versione nem os envie para repositórios públicos;
- restrinja o acesso aos arquivos e mantenha backups protegidos;
- apague ou retenha os dados de acordo com sua finalidade e com as regras aplicáveis;
- informe-se sobre as políticas e expectativas de privacidade das pessoas que participam da transmissão.

A memória é um mecanismo de persistência de software, não uma garantia de lembrança correta. Conteúdo recuperado pode estar incompleto, desatualizado ou ter sido interpretado incorretamente pelo modelo.

## Privacidade, segurança e uso responsável

- **Credenciais:** mantenha tokens e chaves exclusivamente no `.env` local ou em um gerenciador de segredos. Nunca publique `.env`, backups, tokens, bancos SQLite ou logs de execução.
- **Dados de terceiros:** o chat e o áudio podem conter informações pessoais. Colete e retenha apenas o necessário e considere as expectativas do streamer e dos espectadores.
- **Serviços externos:** ao habilitar APIs de IA, transcrição, pesquisa ou visão, parte do conteúdo enviado ao serviço pode sair da máquina local. Leia as políticas do provedor antes de ativar essas integrações.
- **Autonomia:** respostas geradas por modelos podem ser incorretas, inadequadas ou fora de contexto. Mantenha limites de frequência e supervisão, especialmente antes de habilitar o envio real.
- **Regras da plataforma:** use o projeto em conformidade com os Termos de Serviço e as políticas da Twitch, com as condições dos provedores de IA e com os termos da Riot Games quando aplicável.
- **Chaves de desenvolvimento:** chaves de desenvolvimento de APIs podem expirar ou estar sujeitas a cotas. Não trate a disponibilidade de um serviço externo como garantida.

## Solução de problemas

- **Falha de autenticação Twitch:** confirme `TWITCH_TOKEN`, `TWITCH_USERNAME` e a validade do token. Gere um novo token por um fluxo confiável se necessário.
- **Ollama não responde:** verifique se o serviço está em execução, se `OLLAMA_URL` aponta para o endpoint esperado e se o modelo configurado está instalado.
- **Falhas ou lentidão na transcrição:** confira o backend, o modelo, a conectividade e os recursos de hardware. Modelos maiores melhoram potencialmente a qualidade, mas aumentam consumo e latência.
- **Limites ou indisponibilidade de APIs:** revise os logs, as cotas e as chaves do provedor. Se houver fallback configurado, confirme qual backend foi selecionado.
- **Integração LoL desativada:** confira `LOL_ENABLED`, `RIOT_API_KEY`, `LOL_RIOT_ID` no formato `Nome#TAG` e `LOL_REGION`.
- **Sem respostas no chat:** confirme o estado de envio, o cooldown, o limiar de decisão e se o cliente Twitch está conectado. O bot pode optar por permanecer em silêncio.

## Limitações conhecidas

Este projeto é experimental. A transcrição pode errar nomes, gírias, falas sobrepostas e termos de jogos; os modelos podem alucinar ou interpretar mal o contexto; integrações externas podem mudar; e o consumo de CPU, memória, rede e APIs varia de acordo com a configuração. O uso de memória persistente não elimina erros nem garante consistência entre sessões.

## Contribuições

Issues e pull requests são bem-vindos quando incluem contexto reproduzível, logs sanitizados e uma descrição objetiva do resultado esperado. Antes de publicar logs, remova tokens, IDs sensíveis, nomes privados e qualquer conteúdo de chat que não tenha autorização para compartilhar.

## Licença

Nenhuma licença de código aberto é declarada neste README. Até que uma licença seja adicionada ao repositório, não presuma que terceiros têm permissão para reutilizar, modificar ou redistribuir o código. Se a intenção for publicar o projeto como software livre, escolha uma licença e inclua o arquivo correspondente.

---

## English

## Overview

**TwitchLlamaBot v3** is an experimental conversational agent for Twitch livestreams. Rather than replying to every message or continuously narrating the stream, it gathers signals from its environment, maintains context, evaluates possible interventions, and can remain silent when there is no sufficient reason to speak.

The implementation combines separate components for chat, audio, reasoning, memory, and optional integrations. Its central goal is to explore more context-aware participation in live chat while keeping message sending under explicit control.

## Key features

- **Twitch chat over IRC:** authenticates as a user, reads chat messages, and can send replies when sending is enabled.
- **Audio perception:** accesses the public livestream audio and transcribes segments using local Faster-Whisper or a configured compatible remote backend.
- **LLM reasoning:** supports a configurable model/provider fallback chain, including local Ollama and remote providers supported by the code.
- **Persistent SQLite memory:** stores events, sessions, viewer information, and consolidated memories to support future interactions.
- **Configurable persona:** defines character traits, interests, conversational style, and behavioral rules in `persona/profile.py`.
- **Optional Riot API integration:** can retrieve public League of Legends information such as live-game status, queue, champion, and recent match data, subject to API permissions and limits.
- **Optional stream vision:** captures frames from the livestream itself and requests a description from a compatible vision model. It does not capture the operator's local screen.
- **Optional web search:** can retrieve text results to help contextualize topics, depending on configuration and service availability.
- **Operational controls:** provides terminal commands to inspect status and memory, adjust behavior, and control message sending.

## Architecture

```text
Twitch Chat (IRC) ───────────────┐
                                 │
Public stream audio → ASR ───────┤
                                 ├→ Events/context → AI Engine → response evaluation
Optional stream frames ──────────┤                         │
                                 │                         ├→ SQLite memory
Riot API / web search ───────────┘                         │
                                                           └→ Twitch Chat
                                                              (if enabled)
```

Main components:

| Path | Responsibility |
| --- | --- |
| `soraka_que_nao_cura.py` | Startup, component coordination, reflection cycle, and operational commands |
| `core/twitch_client.py` | Twitch chat connection and communication |
| `core/audio_listener.py` | Stream audio capture and transcription |
| `core/ai_engine.py` | Context, prompts, evaluation, and response preparation |
| `core/llm_client.py` | LLM provider integration and backend fallback |
| `core/vision.py` | Optional visual perception of stream frames |
| `core/lol_client.py` | Optional Riot API integration |
| `core/web_search.py` | Text-based web search |
| `memory/sqlite_memory.py` | Local memory persistence and retrieval |
| `persona/profile.py` | Persona definition and behavioral rules |
| `atualizar_env.py` | Adds missing configuration variables without overwriting existing values |

## Requirements

- Windows, Linux, or another environment capable of running the project's dependencies. Primary development has taken place on Windows.
- Python compatible with the installed dependency versions. **Python 3.11 or 3.12 is a reasonable starting point**, but verify compatibility for `av`, `faster-whisper`, and the other packages on your system.
- Internet access for Twitch and, when enabled, AI providers, web search, and the Riot API.
- A Twitch account and a valid OAuth token for the account that will connect.
- For local inference, a working [Ollama](https://ollama.com/) installation and a model compatible with your configuration.
- Enough CPU/GPU and memory resources for the selected local transcription model. Larger models can require substantial resources.
- A Riot API key only if the League of Legends integration is used.
- External provider API keys only for the remote backends you choose to enable.

## Installation

Run the following commands from the project root directory.

### 1. Create a Python environment

```powershell
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks environment activation, review your Windows execution policy or run the interpreter directly through `venv\Scripts\python.exe`. Do not change system security settings without understanding the implications.

### 2. Create the local configuration

The repository should include an `.env.example` file containing variable names and safe defaults, but no real credentials. **The reviewed archive does not include this file**, even though `atualizar_env.py` depends on it. Before using this workflow, create and review a sanitized `.env.example` based on the variables actually used by the code; do not copy secret values from your `.env`.

Once the template exists, create your local configuration:

```powershell
Copy-Item .env.example .env
```

Fill in the required variables. Never publish this file or paste real tokens into issues, public logs, or screenshots.

If you already have a `.env` from an earlier installation and want to add new variables without replacing existing values, run:

```powershell
python atualizar_env.py
```

The script creates a `.env.bak` backup and appends missing keys. Review the result before starting the bot.

### 3. Configure Twitch

Set at least the connection variables listed in `.env.example`, including:

- `TWITCH_CHANNEL`: the channel to monitor, without `#`.
- `TWITCH_USERNAME`: the authenticated account's username, as required by the configuration.
- `TWITCH_TOKEN`: a valid OAuth token for the connecting account.
- `ENABLE_CHAT_RESPONSES`: keep this set to `false` during setup and initial testing.

Anonymous login is not used. Obtain and manage tokens only through trusted methods and grant only the permissions required. Never share your token: it may allow actions on behalf of your account.

### 4. Configure the language model

The bot can use local Ollama and, depending on configuration and available credentials, remote providers. Relevant variables include `LLM_CHAIN`, `OLLAMA_URL`, `OLLAMA_MODEL`, and the keys for enabled providers.

Conceptual example:

```dotenv
LLM_CHAIN=ollama:qwen3:8b
OLLAMA_URL=http://localhost:11434/api/chat
```

Check the current project configuration for the exact supported chain syntax. Model names and availability vary by provider and can change over time. Install the selected model in Ollama before using it locally. For remote providers, configure their respective keys in `.env`.

### 5. Configure audio, vision, and optional integrations

- **Audio:** configure `WHISPER_BACKEND`, `WHISPER_MODEL`, `WHISPER_DEVICE`, and related variables from `.env.example`. A local backend must download/load the selected model. A remote backend requires the corresponding API key and is subject to latency, quotas, and service changes.
- **Vision:** `VISION_ENABLED` controls this feature. Keep it disabled unless a compatible vision backend is configured. It accesses frames from the public livestream.
- **League of Legends:** enable the integration only if you want to use the Riot API. Set `RIOT_API_KEY`, `LOL_RIOT_ID` in `Name#TAG` format, and `LOL_REGION` for the account's region.
- **Web search:** configure `WEB_SEARCH_ENABLED` and related variables as needed.

Use `.env.example` as the source of truth for the complete list of variables and their defaults in this version.

## First run: do not send messages

1. Set `ENABLE_CHAT_RESPONSES=false`.
2. Start the bot:

   ```powershell
   python soraka_que_nao_cura.py
   ```

3. Review startup logs and validate connectivity, transcription, model selection, and memory creation.
4. Use `/status` and `/ajuda` in the terminal to inspect the available controls.
5. Enable real replies only after reviewing the behavior in a controlled session.

**Important:** keep message sending disabled while validating credentials and behavior. Confirm the state reported by the running process before allowing real messages. A test mode does not replace log review.

## Terminal commands

Commands available in the current version include:

| Command | Purpose |
| --- | --- |
| `/ajuda` | Displays available commands |
| `/status` | Shows operational status |
| `/envio on` / `/envio off` | Enables or disables message sending during runtime |
| `/forcar` | Requests a reflection outside the normal cycle |
| `/limiar N` / `/limiar auto` | Adjusts the decision threshold, where applicable |
| `/memoria` | Displays memory information |
| `/lol` | Checks the League of Legends integration |

Commands may change between versions. Use `/ajuda` in the terminal to confirm the effective list. Sending also depends on the initial configuration and the Twitch client state.

## Memory and local data

Memory is stored in local SQLite databases named like `bot_memory_<channel>.db`. The code maintains event records, sessions, viewer profiles, and consolidated memories.

These files may contain usernames, chat messages, transcripts, and information inferred from interactions. Treat them as potentially personal data:

- do not commit them or upload them to public repositories;
- restrict file access and keep backups protected;
- delete or retain data according to its purpose and applicable rules;
- consider the privacy expectations of the streamer and viewers.

Persistent memory is a software mechanism, not a guarantee of accurate recall. Retrieved content may be incomplete, outdated, or incorrectly interpreted by a model.

## Privacy, security, and responsible use

- **Credentials:** keep tokens and keys in the local `.env` file or a secrets manager. Never publish `.env` files, backups, tokens, SQLite databases, or runtime logs.
- **Third-party data:** chat and audio may contain personal information. Collect and retain only what is necessary, and consider the expectations of the streamer and viewers.
- **External services:** when AI, transcription, search, or vision APIs are enabled, some submitted content may leave the local machine. Review provider policies before enabling these integrations.
- **Autonomy:** model-generated replies can be incorrect, inappropriate, or out of context. Keep rate limits and supervision in place, especially before enabling live sending.
- **Platform rules:** use the project in accordance with Twitch's Terms of Service and policies, the terms of the selected AI providers, and Riot Games' terms where applicable.
- **Development keys:** API development keys may expire or be subject to quotas. Do not assume external services will always be available.

## Troubleshooting

- **Twitch authentication fails:** verify `TWITCH_TOKEN`, `TWITCH_USERNAME`, and token validity. Generate a new token through a trusted flow if necessary.
- **Ollama does not respond:** ensure the service is running, `OLLAMA_URL` points to the expected endpoint, and the configured model is installed.
- **Transcription is inaccurate or slow:** check the backend, model, connectivity, and hardware resources. Larger models may improve quality but increase resource usage and latency.
- **API limits or outages:** review logs, quotas, and provider credentials. If fallback is configured, confirm which backend was selected.
- **LoL integration is disabled:** verify `LOL_ENABLED`, `RIOT_API_KEY`, `LOL_RIOT_ID` in `Name#TAG` format, and `LOL_REGION`.
- **No chat replies:** check the sending state, cooldown, decision threshold, and Twitch connection. The bot may intentionally choose silence.

## Known limitations

This is an experimental project. Transcription can misrecognize names, slang, overlapping speech, and gaming terminology; models can hallucinate or misread context; external integrations can change; and CPU, memory, network, and API usage vary by configuration. Persistent memory does not eliminate errors or guarantee consistency across sessions.

## Contributing

Issues and pull requests are welcome when they include reproducible context, sanitized logs, and a clear description of the expected result. Before publishing logs, remove tokens, sensitive IDs, private names, and chat content you are not authorized to share.

## License

This README does not declare an open-source license. Until a license is added to the repository, do not assume that others have permission to reuse, modify, or redistribute the code. If you intend to publish the project as open source, choose a license and include the corresponding file.
