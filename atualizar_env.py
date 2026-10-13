# -*- coding: utf-8 -*-
"""Acrescenta ao seu .env as variáveis novas do .env.example.
NUNCA altera valores que você já tem (token, canal etc.). Faz backup em .env.bak."""
import shutil
from pathlib import Path


def ler(caminho):
    pares = {}
    for linha in Path(caminho).read_text(encoding="utf-8-sig").splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#") and "=" in linha:
            k, v = linha.split("=", 1)
            pares[k.strip()] = v.strip()
    return pares


atual = Path(".env")
if not atual.exists():
    shutil.copy(".env.example", ".env")
    print("Criei .env a partir do .env.example. Preencha TWITCH_* e GROQ_API_KEY.")
    raise SystemExit

shutil.copy(".env", ".env.bak")
tenho = ler(".env")
exemplo = ler(".env.example")

novas = [f"{k}={v}" for k, v in exemplo.items() if k not in tenho]
if novas:
    with open(".env", "a", encoding="utf-8") as f:
        f.write("\n\n# ---- adicionadas pela atualização ----\n" + "\n".join(novas) + "\n")
print(f"{len(novas)} variável(is) nova(s) adicionada(s). Backup em .env.bak.")
for k in novas:
    print("  +", k.split("=")[0])

print("\nValores antigos que você pode querer ajustar (não mexi):")
for k in ("BOT_COOLDOWN_SECONDS", "REFLECTION_INTERVAL_SECONDS", "OLLAMA_PRESENCE_PENALTY"):
    if k in tenho and tenho[k] != exemplo.get(k):
        print(f"  {k}: você tem {tenho[k]} | sugerido {exemplo[k]}")
if not tenho.get("GROQ_API_KEY", exemplo.get("GROQ_API_KEY")):
    print("\nFalta preencher GROQ_API_KEY (console.groq.com/keys).")
