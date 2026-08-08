import os
import sqlite3
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TICKET_CATEGORY_ID = 1524417786602848518
DB_FILE = "queue.db"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("""
CREATE TABLE IF NOT EXISTS queue (
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS panels (
    guild_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL
)
""")
db.commit()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_queue(guild_id: int):
    return db.execute(
        "SELECT * FROM queue WHERE guild_id=? ORDER BY joined_at ASC",
        (str(guild_id),),
    ).fetchall()


def get_position(guild_id: int, user_id: int):
    rows = get_queue(guild_id)
    for i, row in enumerate(rows, 1):
        if str(row["user_id"]) == str(user_id):
            return i
    return None


def add_to_queue(guild_id: int, user_id: int, username: str):
    existing = db.execute(
        "SELECT 1 FROM queue WHERE guild_id=? AND user_id=?",
        (str(guild_id), str(user_id)),
    ).fetchone()
    if existing:
        return False
    db.execute(
        "INSERT INTO queue VALUES (?, ?, ?, ?)",
        (str(guild_id), str(user_id), username, now_iso()),
    )
    db.commit()
    return True


def remove_from_queue(guild_id: int, user_id: int):
    cur = db.execute(
        "DELETE FROM queue WHERE guild_id=? AND user_id=?",
        (str(guild_id), str(user_id)),
    )
    db.commit()
    return cur.rowcount > 0


def save_panel(guild_id, channel_id, message_id):
    db.execute(
        "INSERT OR REPLACE INTO panels VALUES (?, ?, ?)",
        (str(guild_id), str(channel_id), str(message_id)),
    )
    db.commit()


def get_panel(guild_id):
    return db.execute(
        "SELECT * FROM panels WHERE guild_id=?",
        (str(guild_id),),
    ).fetchone()


def format_joined(iso):
    dt = datetime.fromisoformat(iso)
    return dt.astimezone().strftime("%d/%m/%Y à %H:%M")


def is_admin(member: discord.Member):
    return member.guild_permissions.administrator or member.guild_permissions.manage_channels


def ticket_for_user(guild: discord.Guild, user_id: int):
    category = guild.get_channel(TICKET_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        return None
    marker = f"queue_ticket:{user_id}"
    for channel in category.text_channels:
        if channel.topic == marker:
            return channel
    return None


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Fermer le ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="francetier_close_ticket",
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return

        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Seuls les administrateurs/staff avec **Gérer les salons** peuvent fermer ce ticket.",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Salon invalide.", ephemeral=True)
            return

        user_id = None
        if channel.topic and channel.topic.startswith("queue_ticket:"):
            try:
                user_id = int(channel.topic.split(":", 1)[1])
            except ValueError:
                pass

        await interaction.response.send_message("🔒 Ticket fermé. Suppression dans 3 secondes.")
        if user_id:
            remove_from_queue(interaction.guild.id, user_id)

        await update_panel(interaction.guild)
        await ensure_first_ticket(interaction.guild)

        await asyncio.sleep(3)
        try:
            await channel.delete(reason=f"Ticket fermé par {interaction.user}")
        except discord.HTTPException:
            pass


class QueueView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Rejoindre la queue",
        style=discord.ButtonStyle.success,
        emoji="🎟️",
        custom_id="francetier_join_queue",
    )
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return

        added = add_to_queue(
            interaction.guild.id,
            interaction.user.id,
            str(interaction.user),
        )

        if not added:
            pos = get_position(interaction.guild.id, interaction.user.id)
            await interaction.response.send_message(
                f"⚠️ Tu es déjà dans la queue. Position : **#{pos}**.",
                ephemeral=True,
            )
            return

        pos = get_position(interaction.guild.id, interaction.user.id)
        await update_panel(interaction.guild)
        await ensure_first_ticket(interaction.guild)

        if pos == 1:
            msg = "🥇 Tu es **1er de la queue** ! Ton ticket va être créé automatiquement."
        else:
            msg = f"✅ Tu as rejoint la queue ! Tu es **#{pos}**."

        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(
        label="Quitter la queue",
        style=discord.ButtonStyle.secondary,
        emoji="🚪",
        custom_id="francetier_leave_queue",
    )
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return

        was_first = get_position(interaction.guild.id, interaction.user.id) == 1
        removed = remove_from_queue(interaction.guild.id, interaction.user.id)

        if not removed:
            await interaction.response.send_message(
                "❌ Tu n'es pas dans la queue.",
                ephemeral=True,
            )
            return

        ticket = ticket_for_user(interaction.guild, interaction.user.id)
        if ticket:
            try:
                await ticket.delete(reason="Joueur retiré de la queue")
            except discord.HTTPException:
                pass

        await update_panel(interaction.guild)
        if was_first:
            await ensure_first_ticket(interaction.guild)

        await interaction.response.send_message(
            "✅ Tu as quitté la queue.",
            ephemeral=True,
        )


async def create_ticket(guild: discord.Guild, user_id: int):
    row = db.execute(
        "SELECT * FROM queue WHERE guild_id=? AND user_id=?",
        (str(guild.id), str(user_id)),
    ).fetchone()
    if not row:
        return None

    # Anti-doublon : si le ticket existe déjà, on ne crée rien.
    existing = ticket_for_user(guild, user_id)
    if existing:
        return existing

    category = guild.get_channel(TICKET_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        print(f"[ERREUR] {TICKET_CATEGORY_ID} n'est pas une catégorie Discord.")
        return None

    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
    }

    # Les admins peuvent voir le ticket même sans rôle spécial.
    for role in guild.roles:
        if role.permissions.administrator or role.permissions.manage_channels:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
            )

    name = f"ticket-{member.name.lower().replace(' ', '-')[:70]}"
    channel = await guild.create_text_channel(
        name=name,
        category=category,
        overwrites=overwrites,
        topic=f"queue_ticket:{user_id}",
        reason="Joueur devenu premier dans la queue",
    )

    position = get_position(guild.id, user_id) or 1
    embed = discord.Embed(
        title="🎟️ Ticket FranceTier",
        description=(
            f"Bonjour {member.mention} !\n\n"
            "Tu es maintenant **1er dans la queue**.\n"
            "Un administrateur/testeur va s'occuper de toi ici.\n\n"
            f"👤 **Nom :** {member}\n"
            f"📅 **Rejoint le :** {format_joined(row['joined_at'])}\n"
            f"📍 **Position :** #{position}\n\n"
            "🔒 Un administrateur peut fermer ce ticket avec le bouton ci-dessous."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="FranceTier • Queue")
    await channel.send(content=member.mention, embed=embed, view=CloseTicketView())
    return channel


async def ensure_first_ticket(guild: discord.Guild):
    rows = get_queue(guild.id)
    if not rows:
        return

    first = rows[0]
    # Cette fonction est appelée après chaque changement et au démarrage.
    # Elle ne crée qu'un ticket si le premier n'en a pas déjà un.
    await create_ticket(guild, int(first["user_id"]))


async def update_panel(guild: discord.Guild):
    panel = get_panel(guild.id)
    if not panel:
        return

    try:
        channel = guild.get_channel(int(panel["channel_id"])) or await bot.fetch_channel(int(panel["channel_id"]))
        message = await channel.fetch_message(int(panel["message_id"]))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    rows = get_queue(guild.id)
    embed = discord.Embed(
        title="🎟️ FranceTier — Queue",
        description=(
            "Clique sur **Rejoindre la queue** pour attendre ton test.\n"
            "Quand tu arrives **1er**, un ticket privé est créé automatiquement."
        ),
        color=discord.Color.blurple(),
    )

    if not rows:
        embed.add_field(name="👥 Queue", value="*La queue est vide.*", inline=False)
    else:
        lines = []
        for i, row in enumerate(rows, 1):
            member = guild.get_member(int(row["user_id"]))
            mention = member.mention if member else f"<@{row['user_id']}>"
            lines.append(
                f"**#{i}** {mention} — `{row['username']}`\n"
                f"📅 Rejoint : {format_joined(row['joined_at'])}"
            )
        embed.add_field(name=f"👥 Joueurs ({len(rows)})", value="\n\n".join(lines)[:1024], inline=False)

    first = rows[0] if rows else None
    if first:
        embed.add_field(
            name="🥇 Actuellement premier",
            value=f"<@{first['user_id']}>",
            inline=False,
        )

    embed.set_footer(text="FranceTier • Queue automatique")
    await message.edit(embed=embed, view=QueueView())


queue_group = app_commands.Group(name="queue", description="Gestion de la queue FranceTier")


@queue_group.command(name="setup", description="Créer le panneau de queue")
@app_commands.default_permissions(administrator=True)
async def queue_setup(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Commande utilisable dans un salon texte.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🎟️ FranceTier — Queue",
        description=(
            "Bienvenue dans la queue de test !\n\n"
            "🟢 **Rejoindre** → entre dans la queue\n"
            "🚪 **Quitter** → sort de la queue\n"
            "🥇 Quand tu es **1er**, le bot crée automatiquement ton ticket.\n"
            "🔒 Le staff ferme le ticket quand le test est terminé."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="👥 Queue", value="*La queue est vide.*", inline=False)
    embed.set_footer(text="FranceTier • Queue")
    message = await interaction.channel.send(embed=embed, view=QueueView())
    save_panel(interaction.guild.id, interaction.channel.id, message.id)
    await interaction.response.send_message("✅ Panneau de queue créé.", ephemeral=True)


@queue_group.command(name="list", description="Afficher la queue")
async def queue_list(interaction: discord.Interaction):
    if not interaction.guild:
        return
    rows = get_queue(interaction.guild.id)
    if not rows:
        await interaction.response.send_message("📭 La queue est vide.", ephemeral=True)
        return

    text = []
    for i, row in enumerate(rows, 1):
        text.append(f"**#{i}** <@{row['user_id']}> — rejoint le {format_joined(row['joined_at'])}")
    await interaction.response.send_message("\n".join(text), ephemeral=True)


@queue_group.command(name="join", description="Rejoindre la queue")
async def queue_join(interaction: discord.Interaction):
    if not interaction.guild:
        return
    added = add_to_queue(interaction.guild.id, interaction.user.id, str(interaction.user))
    if not added:
        await interaction.response.send_message(
            f"⚠️ Tu es déjà dans la queue. Position : **#{get_position(interaction.guild.id, interaction.user.id)}**.",
            ephemeral=True,
        )
        return
    pos = get_position(interaction.guild.id, interaction.user.id)
    await update_panel(interaction.guild)
    await ensure_first_ticket(interaction.guild)
    await interaction.response.send_message(
        "🥇 Tu es 1er ! Ton ticket est créé." if pos == 1 else f"✅ Tu es #{pos} dans la queue.",
        ephemeral=True,
    )


@queue_group.command(name="leave", description="Quitter la queue")
async def queue_leave(interaction: discord.Interaction):
    if not interaction.guild:
        return
    was_first = get_position(interaction.guild.id, interaction.user.id) == 1
    removed = remove_from_queue(interaction.guild.id, interaction.user.id)
    if not removed:
        await interaction.response.send_message("❌ Tu n'es pas dans la queue.", ephemeral=True)
        return

    ticket = ticket_for_user(interaction.guild, interaction.user.id)
    if ticket:
        try:
            await ticket.delete(reason="Joueur a quitté la queue")
        except discord.HTTPException:
            pass

    await update_panel(interaction.guild)
    if was_first:
        await ensure_first_ticket(interaction.guild)
    await interaction.response.send_message("✅ Tu as quitté la queue.", ephemeral=True)


bot.tree.add_command(queue_group)


@bot.event
async def on_ready():
    print(f"Connecté en tant que {bot.user} ({bot.user.id})")
    for guild in bot.guilds:
        try:
            await bot.tree.sync(guild=guild)
            await ensure_first_ticket(guild)
            await update_panel(guild)
        except Exception as e:
            print(f"[ERREUR] Initialisation {guild.name}: {e}")
    print("Bot prêt.")


@bot.event
async def on_guild_join(guild: discord.Guild):
    await bot.tree.sync(guild=guild)


@tasks.loop(seconds=15)
async def queue_watchdog():
    for guild in bot.guilds:
        try:
            await ensure_first_ticket(guild)
            await update_panel(guild)
        except Exception as e:
            print(f"[WATCHDOG] {guild.name}: {e}")


@queue_watchdog.before_loop
async def before_watchdog():
    await bot.wait_until_ready()


async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN est manquant dans les variables Railway.")
    bot.add_view(QueueView())
    bot.add_view(CloseTicketView())
    queue_watchdog.start()
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
