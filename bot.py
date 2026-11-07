import os
import json
import discord
from discord.ext import tasks
from dotenv import load_dotenv
from mcstatus import JavaServer

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
SERVER_HOST = os.getenv("SERVER_HOST", "ktu121113gmailcom.aternos.me")
SERVER_PORT = int(os.getenv("SERVER_PORT", "51687"))

# 확인 주기(초)
CHECK_INTERVAL = 60
# True  = 서버 상태(열림/닫힘/인원)가 바뀔 때만 메시지 수정
# False = 1분마다 항상 메시지 수정
EDIT_ONLY_ON_CHANGE = True

STATE_FILE = "message_id.json"

intents = discord.Intents.default()
client = discord.Client(intents=intents)

last_snapshot = None
status_message = None


async def check_server():
    """서버 상태를 확인해서 dict로 돌려준다."""
    try:
        server = JavaServer(SERVER_HOST, SERVER_PORT, timeout=5)
        status = await server.async_status()
        version_name = (status.version.name or "").lower()
        motd = status.motd.to_plain().lower()

        # Aternos는 꺼져있을 때 "Offline" 이라고 응답하는 경우가 있음
        if "offline" in version_name or "offline" in motd:
            return {"open": False, "online": 0, "max": 0, "starting": False}
        # 켜지는 중일 때
        if "starting" in version_name or "starting" in motd or "loading" in motd:
            return {"open": False, "online": 0, "max": 0, "starting": True}

        return {
            "open": True,
            "online": status.players.online,
            "max": status.players.max,
            "starting": False,
        }
    except Exception:
        return {"open": False, "online": 0, "max": 0, "starting": False}


def make_embed(info):
    if info["open"]:
        embed = discord.Embed(title="아테노스 서버", description="🟢 **열려있음**", color=0x57F287)
        embed.add_field(name="접속자", value=f"{info['online']} / {info['max']}명")
    elif info["starting"]:
        embed = discord.Embed(title="아테노스 서버", description="🟡 **켜지는 중**", color=0xFEE75C)
    else:
        embed = discord.Embed(title="아테노스 서버", description="🔴 **닫혀있음**", color=0xED4245)
    embed.add_field(name="주소", value=f"`{SERVER_HOST}:{SERVER_PORT}`", inline=False)
    embed.timestamp = discord.utils.utcnow()
    embed.set_footer(text="마지막 확인")
    return embed


def load_message_id():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f).get("message_id")
    except Exception:
        return None


def save_message_id(message_id):
    with open(STATE_FILE, "w") as f:
        json.dump({"message_id": message_id}, f)


async def get_or_create_message(channel, info):
    """저장된 메시지가 있으면 재사용하고, 없으면 새로 보낸다."""
    message_id = load_message_id()
    if message_id:
        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound:
            pass
    message = await channel.send(embed=make_embed(info))
    save_message_id(message.id)
    return message


@tasks.loop(seconds=CHECK_INTERVAL)
async def update_status():
    global last_snapshot, status_message

    info = await check_server()
    snapshot = (info["open"], info["online"], info["starting"])

    if EDIT_ONLY_ON_CHANGE and snapshot == last_snapshot:
        return

    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print("채널을 찾을 수 없어요. CHANNEL_ID를 확인하세요.")
        return

    try:
        if status_message is None:
            status_message = await get_or_create_message(channel, info)
        await status_message.edit(embed=make_embed(info))
        last_snapshot = snapshot
    except discord.NotFound:
        # 누가 메시지를 지웠다면 새로 만든다
        status_message = await channel.send(embed=make_embed(info))
        save_message_id(status_message.id)
        last_snapshot = snapshot
    except Exception as e:
        print("메시지 수정 실패:", e)


@client.event
async def on_ready():
    print(f"로그인 완료: {client.user}")
    if not update_status.is_running():
        update_status.start()


client.run(TOKEN)
