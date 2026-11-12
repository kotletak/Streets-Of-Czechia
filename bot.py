import os
import re
import json
import asyncio
import logging
import aiohttp
import discord
from discord import app_commands
import gspread
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("AudioBot")

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ROBLOX_API_KEY = os.getenv("ROBLOX_API_KEY")
ROBLOX_GROUP_ID = os.getenv("ROBLOX_GROUP_ID")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")

raw_channel_id = os.getenv("UPLOAD_CHANNEL_ID", "").strip()
UPLOAD_CHANNEL_ID = int(raw_channel_id) if raw_channel_id.isdigit() else None

raw_admin_role_id = os.getenv("ADMIN_ROLE_ID", "").strip()
ADMIN_ROLE_ID = int(raw_admin_role_id) if raw_admin_role_id.isdigit() else None

raw_ultras_role_id = os.getenv("ULTRAS_LEADER_ROLE_ID", "").strip()
ULTRAS_LEADER_ROLE_ID = int(raw_ultras_role_id) if raw_ultras_role_id.isdigit() else None

MAX_FILE_SIZE_MB = float(os.getenv("MAX_FILE_SIZE_MB", "19.5"))

OPERATION_POLL_ATTEMPTS = int(os.getenv("OPERATION_POLL_ATTEMPTS", "20"))
OPERATION_POLL_INTERVAL = float(os.getenv("OPERATION_POLL_INTERVAL", "3"))

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


def log_to_sheets_sync(team_name: str, audio_name: str, asset_id: str, uploader: str) -> str:
    if GOOGLE_CREDENTIALS_JSON:
        gc = gspread.service_account_from_dict(json.loads(GOOGLE_CREDENTIALS_JSON))
    else:
        gc = gspread.service_account(filename=CREDENTIALS_FILE)
    sh = gc.open_by_key(SPREADSHEET_ID)

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
    logger.info("Zapsáno do Google Sheets: team=%s, audio=%s, asset_id=%s", safe_team_name, audio_name, asset_id)
    return safe_team_name


async def log_to_sheets(team_name: str, audio_name: str, asset_id: str, uploader: str) -> str:
    return await asyncio.to_thread(log_to_sheets_sync, team_name, audio_name, asset_id, uploader)


async def upload_mp3_to_roblox(
    file_bytes: bytes,
    filename: str,
    audio_name: str,
    team_name: str,
    uploader: str,
) -> dict:
    url = "https://apis.roblox.com/assets/v1/assets"
    headers = {"x-api-key": ROBLOX_API_KEY}

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
            if response.status not in (200, 202):
                raise Exception(f"HTTP {response.status}: {resp_text}")
            operation_data = json.loads(resp_text)

        operation_path = operation_data.get("path")
        if not operation_path or operation_data.get("done"):
            return operation_data

        op_url = f"https://apis.roblox.com/assets/v1/{operation_path}"
        logger.info("Polling operace: %s", op_url)

        for attempt in range(OPERATION_POLL_ATTEMPTS):
            await asyncio.sleep(OPERATION_POLL_INTERVAL)
            async with session.get(op_url, headers=headers) as op_resp:
                if op_resp.status == 200:
                    op_result = await op_resp.json()
                    if op_result.get("done"):
                        logger.info("Operace dokončena po %d pokusech.", attempt + 1)
                        return op_result
                else:
                    logger.warning("Polling vrátil HTTP %d (pokus %d)", op_resp.status, attempt + 1)

    logger.warning("Operace nebyla dokončena po %d pokusech, vracím poslední data.", OPERATION_POLL_ATTEMPTS)
    return operation_data


def extract_asset_id(result: dict) -> str | None:
    asset_id = result.get("response", {}).get("assetId")
    if asset_id:
        return str(asset_id)
    asset_id = result.get("assetId")
    if asset_id:
        return str(asset_id)
    asset_id = result.get("result", {}).get("assetId")
    if asset_id:
        return str(asset_id)
    return None


async def process_upload(
    file_bytes: bytes,
    filename: str,
    audio_name: str,
    team_name: str,
    user: discord.abc.User,
) -> discord.Embed:
    logger.info("Zahajuji nahrávání: audio=%s, team=%s, user=%s", audio_name, team_name, user)
    result = await upload_mp3_to_roblox(file_bytes, filename, audio_name, team_name, user.display_name)

    if "error" in result:
        raise Exception(f"Chyba z Robloxu: {json.dumps(result['error'])}")

    asset_id = extract_asset_id(result)
    asset_id_display = asset_id if asset_id else "Zpracovává se (zkontroluj Creator Dashboard)"

    sheet_tab = await log_to_sheets(team_name, audio_name, asset_id_display, f"{user.name} ({user.id})")

    embed = discord.Embed(title="✅ Audio nahráno a zapsáno do tabulky!", color=discord.Color.green())
    embed.add_field(name="Name", value=f"`{audio_name}`", inline=True)
    embed.add_field(name="Team (List v tabulce)", value=f"`{sheet_tab}`", inline=True)
    embed.add_field(name="Asset ID", value=f"`{asset_id_display}`", inline=False)
    if asset_id and asset_id.isdigit():
        embed.add_field(name="Pro Roblox Studio", value=f"`rbxassetid://{asset_id}`", inline=False)
    embed.set_footer(text=f"Nahrál: {user.display_name}", icon_url=user.display_avatar.url)

    return embed


def has_admin_role(member: discord.Member) -> bool:
    if not ADMIN_ROLE_ID:
        return True
    return any(r.id == ADMIN_ROLE_ID for r in getattr(member, "roles", []))


def has_ultras_role(member: discord.Member) -> bool:
    if not ULTRAS_LEADER_ROLE_ID:
        return True
    return any(r.id == ULTRAS_LEADER_ROLE_ID for r in getattr(member, "roles", []))


class ApprovalView(discord.ui.View):
    def __init__(self, file_bytes: bytes, filename: str, audio_name: str, team_name: str, requester: discord.Member):
        super().__init__(timeout=3600)
        self.file_bytes = file_bytes
        self.filename = filename
        self.audio_name = audio_name
        self.team_name = team_name
        self.requester = requester

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not has_admin_role(interaction.user):
            await interaction.response.send_message("❌ Pouze Admin může schvalovat requesty.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Přijmout", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()
        for child in self.children:
            child.disabled = True

        processing_embed = discord.Embed(
            title="⏳ Nahrávám na Roblox...",
            description=f"Request od **{self.requester.display_name}** byl přijat — nahrávám.",
            color=discord.Color.yellow(),
        )
        await interaction.message.edit(embed=processing_embed, view=self)

        try:
            embed = await process_upload(
                self.file_bytes, self.filename, self.audio_name, self.team_name, self.requester
            )
            embed.description = f"Schválil: {interaction.user.display_name}"
            await interaction.message.edit(embed=embed, view=self)
        except Exception as e:
            logger.exception("Chyba při nahrávání po schválení")
            err_embed = discord.Embed(title="❌ Chyba při nahrávání", description=str(e), color=discord.Color.red())
            await interaction.message.edit(embed=err_embed, view=self)

    @discord.ui.button(label="❌ Odmítnout", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()
        for child in self.children:
            child.disabled = True

        reject_embed = discord.Embed(
            title="❌ Request odmítnut",
            color=discord.Color.red(),
        )
        reject_embed.add_field(name="Name", value=f"`{self.audio_name}`", inline=True)
        reject_embed.add_field(name="Team", value=f"`{self.team_name}`", inline=True)
        reject_embed.set_footer(text=f"Odmítl: {interaction.user.display_name} | Žádal: {self.requester.display_name}")
        await interaction.message.edit(embed=reject_embed, view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class AudioBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("Slash příkazy byly synchronizovány.")


client = AudioBot()


def parse_template(content: str):
    name_match = re.search(r"^Name:\s*(.+)$", content, re.IGNORECASE | re.MULTILINE)
    team_match = re.search(r"^Team:\s*(.+)$", content, re.IGNORECASE | re.MULTILINE)
    if name_match and team_match:
        return name_match.group(1).strip(), team_match.group(1).strip()
    return None, None


@client.event
async def on_ready():
    logger.info("Bot je online jako %s (ID: %s)", client.user, client.user.id)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if not has_ultras_role(message.author):
        return

    if UPLOAD_CHANNEL_ID and message.channel.id != UPLOAD_CHANNEL_ID:
        return

    audio_name, team_name = parse_template(message.content)
    if not audio_name or not team_name:
        return

    mp3_attachment = next(
        (att for att in message.attachments if att.filename.lower().endswith(".mp3")),
        None,
    )
    if not mp3_attachment:
        await message.reply("❌ Vyplnil jsi šablonu `Name:` a `Team:`, ale zapomněl jsi přiložit `.mp3` soubor!")
        return

    size_mb = mp3_attachment.size / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        await message.reply(f"❌ Soubor je příliš velký ({size_mb:.2f} MB). Maximum je {MAX_FILE_SIZE_MB} MB.")
        return

    if not mp3_attachment.filename.lower().endswith(".mp3"):
        await message.reply("❌ Přiložený soubor musí mít příponu `.mp3`!")
        return

    file_bytes = await mp3_attachment.read()

    pending_embed = discord.Embed(
        title="⏳ Čeká na schválení",
        color=discord.Color.orange(),
    )
    pending_embed.add_field(name="Name", value=f"`{audio_name}`", inline=True)
    pending_embed.add_field(name="Team", value=f"`{team_name}`", inline=True)
    pending_embed.set_footer(text=f"Žádal: {message.author.display_name}", icon_url=message.author.display_avatar.url)

    view = ApprovalView(file_bytes, mp3_attachment.filename, audio_name, team_name, message.author)
    await message.reply(embed=pending_embed, view=view)


@client.tree.command(name="upload", description="Pošle request na nahrání MP3 na Roblox (vyžaduje schválení Adminem)")
@app_commands.describe(
    name="Name: Název audia",
    team="Team: Název týmu (vytvoří nebo najde záložku v Google Sheets)",
    mp3="Mp3: Přiložený .mp3 soubor"
)
async def upload_slash(interaction: discord.Interaction, name: str, team: str, mp3: discord.Attachment):
    if not has_ultras_role(interaction.user):
        await interaction.response.send_message("❌ Pouze Ultras Leader může posílat requesty.", ephemeral=True)
        return

    if not mp3.filename.lower().endswith(".mp3"):
        await interaction.response.send_message("❌ Přiložený soubor musí mít příponu `.mp3`!", ephemeral=True)
        return

    size_mb = mp3.size / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        await interaction.response.send_message(
            f"❌ Soubor je příliš velký ({size_mb:.2f} MB). Maximum je {MAX_FILE_SIZE_MB} MB.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)
    file_bytes = await mp3.read()

    pending_embed = discord.Embed(
        title="⏳ Čeká na schválení",
        color=discord.Color.orange(),
    )
    pending_embed.add_field(name="Name", value=f"`{name}`", inline=True)
    pending_embed.add_field(name="Team", value=f"`{team}`", inline=True)
    pending_embed.set_footer(
        text=f"Žádal: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url
    )

    view = ApprovalView(file_bytes, mp3.filename, name, team, interaction.user)
    await interaction.followup.send(embed=pending_embed, view=view)


if __name__ == "__main__":
    client.run(DISCORD_TOKEN, log_handler=None)
