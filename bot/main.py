"""
The CAS bot is defined here. Utility methods for verification, fetching details, roles, nicknames
etc. are defined, as well the bot commands and a main routine.

This module defines the following functions.

- `get_users_from_discordid()`: Find users from DB given user ID
- `is_verified()`: If a user is present in DB or not
- `check_bot_admin()`: Check if a user is a bot admin or not
- `get_realname_from_discordid()`: Get a user's real name from their Discord ID.
- `create_roles_if_missing()`: Adds missing roles to a server.
- `assign_role()`: Adds specified roles to the given user post-verification.
- `delete_role()`: Removes specified roles from the given user post-verification.
- `set_nickname()`: Sets nickname of the given user to real name if server specifies.
- `post_verification()`: Handle role add/delete and nickname set post-verification of given user.
- `verify_user()`: Implements `/verify`.
- `backend_info()`: Logs server details for debug purposes
- `backend_info_error()`: If the author of the message is not a bot admin then reply accordingly.
- `check_is_academic_mod()`: Checks if server is for academic use.
- `query()`: Returns user details, uses Discord ID to find in DB.
- `roll()`: Returns user details, uses roll number to find in DB.
- `roll_or_query_error()`: Replies eror message if server is not academic or author is not a bot admin.
- `on_ready()`: Executes startup actions.

"""

import os
import sys
import asyncio
import platform
import secrets
import time
from typing import TypedDict
from aiohttp import web

import discord
from discord.ext import commands

from motor.motor_asyncio import AsyncIOMotorClient

from config_verification import ConfigEntry, server_configs

if not server_configs:
    sys.exit(1)

TOKEN = os.environ["DISCORD_TOKEN"]
MONGO_DATABASE = os.environ["MONGO_DATABASE"]
MONGO_URI = os.getenv("MONGO_URI")

PROTOCOL = os.getenv("PROTOCOL")
HOST = os.getenv("HOST")

PORT = os.getenv("PORT") if os.getenv("PORT") else "80"
_PORT_AS_SUFFIX = f":{PORT}" if os.getenv("PORT") else ""

SUBPATH = os.getenv("SUBPATH")
BASE_URL = f"{PROTOCOL}://{HOST}{_PORT_AS_SUFFIX}{SUBPATH}"

BOT_ADMINS = {int(id) for id in os.getenv("BOT_ADMINS", "").split(",") if id}

BOT_PRIVATE_IP = os.getenv("BOT_PRIVATE_IP")

VERIFY_TIMEOUT_SECONDS = 300

intent = discord.Intents.default()
intent.message_content = True
bot = commands.Bot(command_prefix=".", intents=intent)
# to get message privelege

mongo_client = AsyncIOMotorClient(
    f"{MONGO_URI}/{MONGO_DATABASE}?retryWrites=true&w=majority"
)
users = mongo_client[MONGO_DATABASE]["users"]

# Yes, global variable. Not the most ideal thing but is efficient
token_to_id: dict[str, tuple[int, float]] = {}


class DBEntry(TypedDict):
    discordId: str
    name: str
    email: str
    rollno: str


async def webserver():
    """
    Launch an aiohttp web server.
    This is an internal server used for the JS code in /portal to communicate
    with the code here. This server MUST NOT be exposed to the public.
    """

    async def authenticate(request: web.Request):
        token = request.match_info["token"]
        try:
            discord_id = str(token_to_id.pop(token)[0])
        except KeyError:
            # the token has already expired
            return web.Response(status=404)

        data = await request.post()
        search = {"discordId": discord_id}
        try:
            updated = {
                "discordId": discord_id,
                "name": data["name"],
                "email": data["email"],
                "rollno": data["rollno"],
            }
        except KeyError:
            # client sent bad request
            return web.Response(status=400)

        await users.update_one(search, {"$set": updated}, upsert=True)
        return web.Response()

    app = web.Application()
    app.add_routes([web.post("/{token}", authenticate)])

    # run app async
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, BOT_PRIVATE_IP, 80).start()


async def get_users_from_discordid(user_id: int):
    """
    Finds users from the database, given their ID and returns
    a list containing those users.
    """
    ret: list[DBEntry] = []
    async for document in users.find({"discordId": str(user_id)}):
        ret.append(document)
    return ret


async def is_verified(user_id: int):
    """Checks if any user with the given ID exists in the DB or not."""
    return bool(await get_users_from_discordid(user_id))


@commands.check
def check_bot_admin(ctx: commands.Context):
    """Checks if the user with the given discord ID is a bot admin or not."""
    return ctx.author.id in BOT_ADMINS


async def get_realname_from_discordid(user_id: int):
    """Returns the real name of the first user who matches the given ID."""
    users = await get_users_from_discordid(user_id)
    assert users
    return users[0]["name"]


def get_first_channel_with_permission(guild: discord.Guild):
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).send_messages:
            return channel

    return None


async def create_roles_if_missing(guild: discord.Guild, req_guild_roles: set[str]):
    """Creates roles for the given guild if they are missing."""
    role_names = [role.name for role in guild.roles]
    for role in req_guild_roles:
        if role not in role_names:
            await guild.create_role(name=role)


async def assign_role(member: discord.Member, server_config: ConfigEntry):
    """For a given guild, user and server confg object, creates roles in the guild if missing,
    and then assigns the guild's roles to the user post-verification.
    """
    req_roles = server_config["grantroles"]

    await create_roles_if_missing(member.guild, req_roles)

    assign_roles = [role for role in member.guild.roles if role.name in req_roles]

    await member.add_roles(*assign_roles)


async def delete_role(member: discord.Member, server_config: ConfigEntry):
    """
    For a given guild, user and server config object, remove roles that the server specified
    to be deleted post-verification.
    """
    config_remove_roles = server_config["deleteroles"]
    to_remove_roles = [
        role for role in member.guild.roles if role.name in config_remove_roles
    ]

    # if the user does not have that role, this does not crash
    await member.remove_roles(*to_remove_roles)


async def set_nickname(member: discord.Member, server_config: ConfigEntry):
    """If the server wants users to have their actual names as server nicknames,
    set the given user's nickname to their name fetched from the database.
    """
    if server_config["setrealname"]:
        realname = await get_realname_from_discordid(member.id)
        await member.edit(nick=realname)


async def leave_guild(guild: discord.Guild, channel: discord.abc.Messageable | None):
    """
    Send a message to the given channel and leave the given guild.
    """
    if channel:
        await channel.send(
            "This server is not authorized to work with CAS-bot. "
            "Read the instructions to invite the bot in the project README",
        )
    await guild.leave()


async def post_verification(
    ctx: commands.Context, member: discord.Member | discord.User
):
    """
    For the Discord context and the given member, assign roles to be added post-verification
    and remove roles to be deleted post verification as specified in the serve's config object.
    """
    if ctx.guild is None or isinstance(member, discord.User):
        # we are in a non-server context (like a DM channel)
        # do not try to do server verification
        await ctx.reply(
            f"{member.mention} has been CAS-verified! Now, run the same command on the "
            "discord servers where you want to be verified!"
        )
        return

    server_config = server_configs.get(ctx.guild.id)
    if server_config is None:
        await leave_guild(ctx.guild, ctx.channel)
        return

    try:
        await assign_role(member, server_config)
        await delete_role(member, server_config)
    except discord.DiscordException:
        await ctx.reply(
            "Failed to assign or delete roles. "
            "The server admin(s) should set up the right permissions for the bot"
        )

    try:
        await set_nickname(member, server_config)
    except discord.DiscordException:
        await ctx.reply(
            "Failed to set nickname. "
            "The server admin(s) should set up the right permissions for the bot."
        )

    await ctx.reply(f"{member.mention} has been CAS-verified on this server!")


@bot.hybrid_command(name="verify")
async def verify_user(ctx: commands.Context):
    """
    Verify yourself with a CAS login.

    Runs when the user does `/verify` in the server. First tries to find the user.
    If present, performs post-verification actions. If not verified, creates and sends
    a unique verification link. Handles the link timeout and also performs post-verify
    actions if the user could verify within the timeout.
    """
    if not ctx.interaction:
        await ctx.reply("This command has been removed, please use /verify instead.")
        return

    author = ctx.message.author
    if await is_verified(author.id):
        # user has already previously verified
        await post_verification(ctx, author)
        return

    old_link = True
    for loop_token, (loop_discord_id, loop_expire_time) in token_to_id.items():
        if loop_discord_id == author.id:
            # user already has an active link
            token = loop_token
            expire_time = loop_expire_time
            break
    else:
        old_link = False
        token = secrets.token_urlsafe()
        expire_time = time.time() + VERIFY_TIMEOUT_SECONDS
        token_to_id[token] = (author.id, expire_time)

    # it is important that is is ephemeral. It has a secret link that only the
    # invoker must see.
    await ctx.send(
        f"[This](<{BASE_URL}/cas?token={token}>) is your verification link, click "
        "it to login and verify yourself.\n"
        "IMPORTANT NOTE: Above link is secret, do not share with anyone! "
        f"This link expires <t:{int(expire_time)}:R>.",
        ephemeral=True,
    )

    if old_link:
        return

    while time.time() < expire_time and token in token_to_id:
        await asyncio.sleep(1)

    if token not in token_to_id and await is_verified(author.id):
        await post_verification(ctx, author)
        return

    # time-out has happened, and no success. pop token to expire link
    token_to_id.pop(token, None)
    await ctx.reply(
        f"{author.mention}, you haven't been CAS-verified, you may retry to /verify"
    )


@bot.hybrid_command(name="backend_info")
@check_bot_admin
async def backend_info(ctx: commands.Context):
    """For debugging server info; sends details of the server."""
    uname = platform.uname()
    await ctx.reply(
        f"Here are the server details:\n"
        f"system: {uname.system}\n"
        f"node: {uname.node}\n"
        f"release: {uname.release}\n"
        f"version: {uname.version}\n"
        f"machine: {uname.machine}",
        ephemeral=True,
    )


@backend_info.error
async def backend_info_error(ctx: commands.Context, error: Exception):
    """If the author of the message is not a bot admin then reply accordingly."""
    if isinstance(error, commands.CheckFailure):
        await ctx.reply(f"{ctx.author.mention} is not a bot admin.", ephemeral=True)


@commands.check
async def check_is_academic_mod(ctx: commands.Context):
    """
    Checks if the server is an academic server, and that the invoker has moderation
    permissions
    """
    if ctx.guild is None:
        return False

    try:
        if server_configs[ctx.guild.id]["is_academic"]:
            return await commands.has_permissions(moderate_members=True).predicate(ctx)

    except KeyError:
        pass

    return False


@bot.hybrid_command(name="query")
@commands.check_any(check_is_academic_mod, check_bot_admin)
async def query(
    ctx: commands.Context,
    identifier: discord.User,
):
    """
    First checks if the server is an academic one or if the author is a bot admin.
    If so, finds the user mentioned (by Discord ID) in the command in the DB.
    If present, replies with their name, email and roll number. Otherwise
    replies telling the author that the mentioned user is not registed with CAS.
    """
    user = await users.find_one({"discordId": str(identifier.id)})
    if user:
        await ctx.reply(
            f"Name: {user['name']}\nEmail: {user['email']}\nRoll Number: {user['rollno']}",
            ephemeral=True,
        )
    else:
        await ctx.reply(
            f"{identifier} is not registered with IIIT-CAS.", ephemeral=True
        )


@bot.hybrid_command(name="roll")
@commands.check_any(check_is_academic_mod, check_bot_admin)
async def roll(
    ctx: commands.Context,
    identifier: int,
):
    """
    First checks if the server is an academic one or if the author is a bot admin.
    If so, finds the user mentioned in the command in the DB. If present, replies
    with their name, email and roll number. Otherwise replies telling the author
    that the mentioned user is not registed with CAS.

    Same as the `query` command, except the user is mentioned by roll number
    instead of Discord ID.
    """
    user = await users.find_one({"rollno": str(identifier)})
    if user:
        await ctx.reply(
            f"Name: {user['name']}\nEmail: {user['email']}\nRoll Number: {user['rollno']}",
            ephemeral=True,
        )
    else:
        await ctx.reply(
            f"{identifier} is not registered with IIIT-CAS.", ephemeral=True
        )


@query.error
@roll.error
async def roll_or_query_error(ctx: commands.Context, error: Exception):
    """
    For the `roll` and `query` commands, if the server is not academic and if
    the author is not a bot admin, replies with an error message.
    """
    if isinstance(error, commands.CheckFailure):
        await ctx.reply(
            "This server is not for academic purposes "
            f"and {ctx.author.mention} is not a bot admin.",
            ephemeral=True,
        )


@bot.event
async def on_guild_join(guild: discord.Guild):
    server_config = server_configs.get(guild.id)
    first_channel = get_first_channel_with_permission(guild)
    if not server_config:
        await leave_guild(guild, first_channel)
        return

    if first_channel:
        await first_channel.send("CAS-bot has joined this server")


@bot.event
async def on_ready():
    """This is executed when the bot connects to a server."""
    if not bot.user:
        print("Failed to connect to discord! bot.user not defined")
        return

    print(f"{bot.user.name} has connected to Discord!")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands.")
    except Exception as e:
        print(e)

    users_count = await users.count_documents({})
    print(f"Connected to database! The collection `users` has {users_count} documents.")

    bot.loop.create_task(webserver())

    print(f"The bot is a member in {len(bot.guilds)} guild(s).")
    for guild in bot.guilds:
        server_config = server_configs.get(guild.id)
        if not server_config:
            print(f"  - LEAVING: {guild.id} [{guild.name}]")
            await leave_guild(guild, get_first_channel_with_permission(guild))
            continue

        print(f"  - {guild.id}: {guild.name} [{server_config['configname']}]")


if __name__ == "__main__":
    bot.run(TOKEN)
