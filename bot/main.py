"""
The CAS bot is defined here. Utility methods for verification, fetching details, roles, nicknames
etc. are defined, as well the bot commands and a main routine.

This module defines the following functions.

- `get_users_from_discordid()`: Find users from DB given user ID
- `is_verified()`: If a user is present in DB or not
- `get_realname_from_discordid()`: Get a user's real name from their Discord ID.
- `send_link()`: Send link for reattempting authentication.
- `create_roles_if_missing()`: Adds missing roles to a server.
- `assign_role()`: Adds specified roles to the given user post-verification.
- `delete_role()`: Removes specified roles from the given user post-verification.
- `set_nickname()`: Sets nickname of the given user to real name if server specifies.
- `post_verification()`: Handle role add/delete and nickname set post-verification of given user.
- `verify_user()`: Implements `.verify`.
- `backend_info()`: Logs server details for debug purposes
- `is_academic()`: Checks if server is for academic use.
- `query()`: Returns user details, uses Discord ID to find in DB.
- `query_error()`: Replies eror message if server is not academic.
- `roll()`: Returns user details, uses roll number to find in DB.
- `roll_error()`: Replies eror message if server is not academic.
- `on_ready()`: Logs a message when the bot joins a server.
- `main()`: Reads server config, loads DB and starts bot.

"""

import os
import sys
import asyncio
import platform
from typing import TypedDict
from dotenv import load_dotenv

import discord
from discord.ext import commands

from pymongo import MongoClient, database

from config_verification import ConfigEntry, server_configs

if not server_configs:
    sys.exit(1)

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_DATABASE = os.getenv("MONGO_DATABASE")
MONGO_URI = os.getenv("MONGO_URI")

PROTOCOL = os.getenv("PROTOCOL")
HOST = os.getenv("HOST")

PORT = os.getenv("PORT") if os.getenv("PORT") else "80"
_PORT_AS_SUFFIX = f":{PORT}" if os.getenv("PORT") else ""

SUBPATH = os.getenv("SUBPATH")
BASE_URL = f"{PROTOCOL}://{HOST}{_PORT_AS_SUFFIX}{SUBPATH}"

BOT_ADMINS = [int(id) for id in os.getenv("BOT_ADMINS").split(",") if id != ""]

intent = discord.Intents.default()
intent.message_content = True
bot = commands.Bot(command_prefix=".", intents=intent)
# to get message privelege

db: database.Database | None = None  # assigned in main function


class DBEntry(TypedDict):
    discordId: str
    name: str
    email: str
    rollno: str
    view: bool


def get_users_from_discordid(user_id: int):
    """
    Finds users from the database, given their ID and returns
    a list containing those users.
    """
    users: list[DBEntry] = []
    if db is not None:
        users = list(db.users.find({"discordId": str(user_id)}))
    return users


def is_verified(user_id: int):
    """Checks if any user with the given ID exists in the DB or not."""
    return True if get_users_from_discordid(user_id) else False


def is_bot_admin(user_id: int):
    """Checks if the user with the given discord ID is a bot admin or not."""
    return user_id in BOT_ADMINS


def is_author_bot_admin(ctx: commands.Context):
    """Wrapper check function for `is_bot_admin`; checks if the user who invoked the command
    is a bot admin or not."""
    userID = ctx.message.author.id
    return is_bot_admin(userID)


def get_realname_from_discordid(user_id: int):
    """Returns the real name of the first user who matches the given ID."""
    users = get_users_from_discordid(user_id)
    assert users
    return users[0]["name"]


async def send_link(ctx: commands.Context):
    """Sends the base url for users to reattempt sign-in."""
    await ctx.reply(
        f"<{BASE_URL}>\nSign in through our portal, and try again.", ephemeral=True
    )


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
        realname = get_realname_from_discordid(member.id)
        await member.edit(nick=realname)


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
        await ctx.reply(
            "This server is not authorized to work with CAS-bot. Read the instructions to invite the bot in the project README",
            ephemeral=True,
        )
        await ctx.guild.leave()
        return

    await assign_role(member, server_config)
    await delete_role(member, server_config)

    try:
        await set_nickname(member, server_config)
    except discord.DiscordException:
        await ctx.reply(
            "Bot should have a role higher than you to change your nickname",
            ephemeral=True,
        )

    await ctx.reply(f"{member.mention} has been CAS-verified on this server!")


@bot.hybrid_command(name="verify")
async def verify_user(ctx: commands.Context):
    """
    Runs when the user types `.verify` in the server. First tries to find the user in the DB.
    If present, performs post-verification actions. If not verified, Sends the link to authenticate
    and waits for a minute. xits if the user still is not found after that and
    tells user to run `.verify` again.
    """
    author = ctx.message.author
    for i in range(2):
        verification = is_verified(author.id)

        if verification:
            await post_verification(ctx, author)
            break
        if i == 0:
            await send_link(ctx)
            await asyncio.sleep(60)
        else:
            await ctx.reply(
                f"Sorry {author.mention}, could not auto-detect your verification. \
                    Please run `.verify` again.",
                ephemeral=True,
            )


@bot.hybrid_command(name="backend_info")
async def backend_info(ctx: commands.Context):
    """For debugging server info; sends details of the server."""

    author = ctx.message.author
    if is_bot_admin(author.id):
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
    else:
        await ctx.reply(f"{author.id} is not a bot admin.", ephemeral=True)


def is_academic(ctx: commands.Context):
    """Checks if the server is an academic server."""
    if ctx.guild is None:
        return False

    try:
        return server_configs[ctx.guild.id]["is_academic"]
    except KeyError:
        return False


@bot.hybrid_command(name="query")
@commands.check(is_academic)
async def query(
    ctx: commands.Context,
    identifier: discord.User,
):
    """
    First checks if the server is an academic one. If so, finds the user who invoked the
    command (by Discord ID) in the DB. If present, replies with their name, email and
    roll number. Otherwise replies telling the user they are not registed with CAS.
    """
    if db is None:
        await ctx.reply(
            "The bot is currently initializing and the command cannot be processed.\n"
            "Please wait for some time and then try again.",
            ephemeral=True,
        )
        return

    user = db.users.find_one({"discordId": str(identifier.id)})
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
async def query_error(ctx: commands.Context, error: Exception):
    """
    For the `query` command, if the server is not academic, replies with error message.
    """
    if isinstance(error, commands.CheckFailure):
        await ctx.reply("This server is not for academic purposes.", ephemeral=True)


@bot.hybrid_command(name="roll")
@commands.check(is_academic)
async def roll(
    ctx: commands.Context,
    identifier: int,
):
    """
     First checks if the server is an academic one. If so, finds the user who invoked the
    command in the DB. If present, replies with their name, email and
    roll number. Otherwise replies telling the user they are not registed with CAS.

    Same as the `query` command, except this searches by roll number instead of Discord ID.
    """
    if db is None:
        await ctx.reply(
            "The bot is currently initializing and the command cannot be processed.\n"
            "Please wait for some time and then try again.",
            ephemeral=True,
        )
        return

    user = db.users.find_one({"rollno": str(identifier)})
    if user:
        await ctx.reply(
            f"Name: {user['name']}\nEmail: {user['email']}\nRoll Number: {user['rollno']}",
            ephemeral=True,
        )
    else:
        await ctx.reply(
            f"{identifier} is not registered with IIIT-CAS.", ephemeral=True
        )


@roll.error
async def roll_error(ctx: commands.Context, error: Exception):
    """
    For the `roll` command, if the server is not academic, replies with error message.
    """
    if isinstance(error, commands.CheckFailure):
        await ctx.reply("This server is not for academic purposes.", ephemeral=True)


@bot.event
async def on_guild_join(guild: discord.Guild):
    server_config = server_configs.get(guild.id)

    welcome_message = "CAS-bot has joined this server"
    first_channel = get_first_channel_with_permission(guild)

    if not server_config:
        welcome_message = "This server is not authorized to work with CAS-bot. Read the instructions to invite the bot in the project README."
        if first_channel:
            await first_channel.send(welcome_message)
        await guild.leave()
        return

    if first_channel:
        await first_channel.send(welcome_message)


@bot.event
async def on_ready():
    """This is executed when the bot connects to a server."""
    print(f"{bot.user.name} has connected to Discord!")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands.")
    except Exception as e:
        print(e)


def main():
    """
    First it checks if each server has a valid configuration. If not, it exits with an error.
    Otherwise, It iniates a client for a MongoDB instance and fetches the database from there,
    setting the global variable `db`. Then it starts the bot.
    """
    global db  # pylint: disable=global-statement

    mongo_client = MongoClient(
        f"{MONGO_URI}/{MONGO_DATABASE}?retryWrites=true&w=majority"
    )
    db = mongo_client.get_database(MONGO_DATABASE)

    bot.run(TOKEN)


if __name__ == "__main__":
    main()
