import os
import re
import json
import asyncio
import aiohttp
import discord
from discord import app_commands
import gspread
from dotenv import load_dotenv

# ==========================================
# NAČTENÍ .ENV KONFIGURACE
# ==========================================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ROBLOX_API_KEY = os.getenv("ROBLOX_API_KEY")
ROBLOX_GROUP_ID = os.getenv("ROBLOX_GROUP_ID")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json")

# Volitelné ID kanálu (převede se na číslo, pokud je vyplněné)
raw_channel_id = os.getenv("UPLOAD_CHANNEL_ID", "").strip()
UPLOAD_CHANNEL_ID = int(raw_channel_id) if raw_channel_id.isdigit() else None

MAX_FILE_SIZE_MB = float(os.getenv("MAX_FILE_SIZE_MB", "19.5"))

# Kontrola povinných proměnných při startu
missing_vars = [
    var_name for var_name, val in [
        ("DISCORD_TOKEN", DISCORD_TOKEN),
        ("ROBLOX_API_KEY", ROBLOX_API_KEY),
        ("ROBLOX_GROUP_ID", ROBLOX_GROUP_ID),
        ("SPREADSHEET_ID", SPREADSHEET_ID),
    ] if not val
]

if missing_vars:
    raise RuntimeError(f"❌ V souboru .env chybí tyto povinné proměnné: {', '.join(missing_vars)}")


# ==========================================
# GOOGLE SHEETS FUNKCE
# ==========================================
def log_to_sheets_sync(team_name: str, audio_name: str, asset_id: str, uploader: str):
    """Synchronní funkce pro gspread - najde nebo vytvoří list týmu a zapíše záznam."""
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open_by_key(SPREADSHEET_ID)

    # Očištění názvu týmu pro Google Sheets (nesmí obsahovat * ? : / \ [ ])
    safe_team_name = re.sub(r'[\*\?\:\/\\\[\]]', '', team_name).strip()[:99]
    if not safe_team_name:
        safe_team_name = "Nezarazeno"

    try:
        worksheet = sh.worksheet(safe_team_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=safe_team_name, rows=1000, cols=5)
        worksheet.append_row(["Name", "Asset ID", "Roblox Format", "Nahrál (Discord)"])

    worksheet.append_row([
        audio_name,
        str(asset_id),
        f"rbxassetid://{asset_id}",
        uploader
    ])
    return safe_team_name


async def log_to_sheets(team_name: str, audio_name: str, asset_id: str, uploader: str) -> str:
    """Spustí zápis do Google Sheets v samostatném vlákně, aby neblokoval Discord bota."""
    return await asyncio.to_thread(log_to_sheets_sync, team_name, audio_name, asset_id, uploader)


# ==========================================
# ROBLOX OPEN CLOUD UPLOAD FUNKCE
# ==========================================
async def upload_mp3_to_roblox(file_bytes: bytes, filename: str, audio_name: str, team_name: str, uploader: str) -> dict:
    """Nahraje MP3 na Roblox Group přes Open Cloud Assets API v1 a počká na Asset ID."""
    url = "https://apis.roblox.com/assets/v1/assets"
    headers = {
        "x-api-key": ROBLOX_API_KEY
    }

    display_name = f"[{team_name}] {audio_name}"[:50]
    description = f"Name: {audio_name}\nTeam: {team_name}\nNahrál: {uploader}"[:1000]

    request_payload = {
        "assetType": "Audio",
        "displayName": display_name,
        "description": description,
        "creationContext": {
            "creator": {
                "groupId": str(ROBLOX_GROUP_ID)
            }
        }
    }

    form = aiohttp.FormData()
    form.add_field("request", json.dumps(request_payload), content_type="application/json")
    form.add_field("fileContent", file_bytes, filename=filename, content_type="audio/mpeg")

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=form) as response:
            resp_text = await response.text()
            if response.status != 200:
                raise Exception(f"HTTP {response.status}: {resp_text}")
            operation_data = json.loads(resp_text)

        operation_path = operation_data.get("path")
        if not operation_path or operation_data.get("done"):
            return operation_data

        op_url = f"https://apis.roblox.com/assets/v1/{operation_path}"
        for _ in range(15):
            await asyncio.sleep(2)
            async with session.get(op_url, headers=headers) as op_resp:
                if op_resp.status == 200:
                    op_result = await op_resp.json()
                    if op_result.get("done"):
                        return op_result

        return operation_data


# ==========================================
# DISCORD BOT A ZPRACOVÁNÍ ŠABLONY
# ==========================================
class AudioBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("Slash příkazy byly synchronizovány.")


client = AudioBot()


def parse_template(content: str):
    """Přečte zprávu a vytáhne hodnoty za 'Name:' a 'Team:'."""
    name_match = re.search(r"^Name:\s*(.+)$", content, re.IGNORECASE | re.MULTILINE)
    team_match = re.search(r"^Team:\s*(.+)$", content, re.IGNORECASE | re.MULTILINE)

    if name_match and team_match:
        return name_match.group(1).strip(), team_match.group(1).strip()
    return None, None


async def process_upload(attachment: discord.Attachment, audio_name: str, team_name: str, user: discord.abc.User):
    """Společná logika pro kontrolu souboru, nahrání na Roblox a zápis do tabulky."""
    if not attachment.filename.lower().endswith(".mp3"):
        raise ValueError("Přiložený soubor musí mít příponu `.mp3`!")

    size_mb = attachment.size / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise ValueError(f"Soubor je příliš velký ({size_mb:.2f} MB). Maximum je {MAX_FILE_SIZE_MB} MB.")

    file_bytes = await attachment.read()
    result = await upload_mp3_to_roblox(file_bytes, attachment.filename, audio_name, team_name, user.display_name)

    if "error" in result:
        raise Exception(f"Chyba z Robloxu: {json.dumps(result['error'])}")

    asset_id = result.get("response", {}).get("assetId")
    if not asset_id:
        asset_id = "Zpracovává se (zkontroluj Creator Dashboard)"

    sheet_tab = await log_to_sheets(team_name, audio_name, asset_id, f"{user.name} ({user.id})")

    embed = discord.Embed(title="✅ Audio nahráno a zapsáno do tabulky!", color=discord.Color.green())
    embed.add_field(name="Name", value=f"`{audio_name}`", inline=True)
    embed.add_field(name="Team (List v tabulce)", value=f"`{sheet_tab}`", inline=True)
    embed.add_field(name="Asset ID", value=f"`{asset_id}`", inline=False)
    if str(asset_id).isdigit():
        embed.add_field(name="Pro Roblox Studio", value=f"`rbxassetid://{asset_id}`", inline=False)
    embed.set_footer(text=f"Nahrál: {user.display_name}", icon_url=user.display_avatar.url)

    return embed


@client.event
async def on_ready():
    print(f"Bot je online jako {client.user} (ID: {client.user.id})")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if UPLOAD_CHANNEL_ID and message.channel.id != UPLOAD_CHANNEL_ID:
        return

    audio_name, team_name = parse_template(message.content)
    if not audio_name or not team_name:
        return

    mp3_attachment = next((att for att in message.attachments if att.filename.lower().endswith(".mp3")), None)
    if not mp3_attachment:
        await message.reply("❌ Vyplnil jsi šablonu `Name:` a `Team:`, ale zapomněl jsi přiložit `.mp3` soubor!")
        return

    status_msg = await message.reply(f"⏳ Nahrávám `{audio_name}` pro tým **{team_name}** na Roblox a zapisuji do tabulky...")

    try:
        embed = await process_upload(mp3_attachment, audio_name, team_name, message.author)
        await status_msg.edit(content=None, embed=embed)
    except Exception as e:
        await status_msg.edit(content=f"❌ **Chyba:** {str(e)}")


@client.tree.command(name="upload", description="Nahraje MP3 na Roblox a zapíše Name a ID do tabulky týmu")
@app_commands.describe(
    name="Name: Název audia",
    team="Team: Název týmu (vytvoří nebo najde záložku v Google Sheets)",
    mp3="Mp3: Přiložený .mp3 soubor"
)
async def upload_slash(interaction: discord.Interaction, name: str, team: str, mp3: discord.Attachment):
    await interaction.response.defer(thinking=True)
    try:
        embed = await process_upload(mp3, name, team, interaction.user)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ **Chyba:** {str(e)}")


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
