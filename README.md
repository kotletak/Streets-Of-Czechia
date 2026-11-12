# Streets of Czechia – Discord Bot

Discord bot pro nahrávání MP3 souborů na Roblox přes Open Cloud API a automatické logování do Google Sheets.

## Funkce

- Přijímá zprávy se šablonou `Name:` / `Team:` a přiloženým `.mp3` souborem
- Podporuje slash příkaz `/upload`
- Nahraje audio na Roblox Group přes Open Cloud Assets API
- Zapíše záznam do Google Sheets (automaticky vytvoří záložku pro každý tým)

## Instalace

```bash
pip install discord.py aiohttp gspread python-dotenv
```

## Konfigurace

Vytvoř soubor `.env` v kořenové složce projektu:

```
DISCORD_TOKEN=tvůj_discord_bot_token
ROBLOX_API_KEY=tvůj_roblox_open_cloud_api_key
ROBLOX_GROUP_ID=id_tvé_roblox_skupiny
SPREADSHEET_ID=id_google_tabulky
CREDENTIALS_FILE=credentials.json       # cesta ke Google service account JSON
UPLOAD_CHANNEL_ID=                      # (volitelné) ID Discord kanálu; prázdné = všechny kanály
MAX_FILE_SIZE_MB=19.5                   # (volitelné) maximální velikost MP3 v MB
OPERATION_POLL_ATTEMPTS=20             # (volitelné) počet pokusů při čekání na Roblox operaci
OPERATION_POLL_INTERVAL=3              # (volitelné) prodleva v sekundách mezi pokusy
```

> **⚠️ DŮLEŽITÉ – BEZPEČNOST:**  
> Soubor `credentials.json` obsahuje privátní klíče ke Google service accountu.  
> **NIKDY** tento soubor nenahrávej na GitHub ani jiný veřejný repozitář!  
> Přidej `credentials.json` a `.env` do `.gitignore`.

## .gitignore

```
.env
credentials.json
*.log
__pycache__/
```

## Spuštění

```bash
python bot.py
```

## Šablona zprávy

```
Name: Název audia
Team: Název týmu
[přiložený .mp3 soubor]
```

## Slash příkaz

```
/upload name:<název> team:<tým> mp3:<soubor>
```
