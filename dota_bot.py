import logging
import os
import gevent
from dataclasses import dataclass
from enum import IntEnum

from steam.client import SteamClient

import dota2
from dota2.client import Dota2Client
from dota2.proto_enums import DOTAChatChannelType_t
from dota2.features.chat import ChatChannel
from dota2.protobufs import dota_gcmessages_client_chat_pb2


logging.basicConfig(level=logging.INFO)


class LobbyState(IntEnum):
    UI = 0
    READYUP = 4
    SERVERSETUP = 1
    RUN = 2
    POSTGAME = 3
    NOTREADY = 5
    SERVERASSIGN = 6


GameServers = {
    "EU": dota2.enums.EServerRegion.Europe,
    "USE": dota2.enums.EServerRegion.USEast,
    "USW": dota2.enums.EServerRegion.USWest,
    "AU": dota2.enums.EServerRegion.Australia,
    "SEA": dota2.enums.EServerRegion.Singapore,
}


@dataclass
class Credentials:
    login: str
    password: str


def create_new_lobby(dota: Dota2Client):
    dota.lobby_options = {
        "game_name": "dynia testing",
        "game_mode": dota2.enums.DOTA_GameMode.DOTA_GAMEMODE_CM,
        "server_region": GameServers[dota.server],
        "leagueid": int(os.getenv("LEAGUE_ID", 0)),
        "fill_with_bots": True,
        "allow_spectating": True,
        "allow_cheats": True,
        "allchat": False,
        # 'dota_tv_delay': LobbyDotaTVDelay.LobbyDotaTV_10,  # TODO: this is LobbyDotaTV_10
        "pause_setting": 0,  # TODO: LobbyDotaPauseSetting_Unlimited
        "do_player_draft": True,
    }

    dota.create_practice_lobby(password=os.getenv("LOBBY_PASSWORD", ""), options=dota.lobby_options)


credentials = Credentials(login=os.getenv("BOT_LOGIN"), password=os.getenv("BOT_PASSWORD"))

bot = None


def bot_loop(credentials):
    logging.info("loop starting")
    client = SteamClient()
    dota = Dota2Client(client)

    dota.server = "EU"
    logging.info("clients created")

    # TODO: get rid of this asap
    global bot
    bot = dota

    @client.on("logged_on")
    def logged_on():
        dota.launch()

    @dota.on("ready")
    def dota_started():
        logging.info(f"Logged in: {dota.steam.username} {dota.account_id}")

        create_new_lobby(dota)

    @dota.on("notready")
    def dota_connection_lost():
        logging.error(f"Dota connection lost: {dota.steam.username} {dota.account_id}")
        delay = 30
        gevent.sleep(delay)

        logging.info("Trying to launch dota again.")
        dota.launch()

    @dota.on(dota2.features.Lobby.EVENT_LOBBY_CHANGED)
    def dota_lobby_changed(lobby: dota2.features.Lobby):
        if not lobby:
            logging.info("Lobby is NONE. Doing nothing")
            return

        logging.info(f"lobby state changed: {lobby.state}, {int(lobby.state)}")

    @dota.on(dota2.features.Lobby.EVENT_LOBBY_NEW)
    def dota_lobby_new(lobby: dota2.features.Lobby):
        logging.info(f"found new lobby: {dota.steam.username} {lobby.lobby_id}")

        dota.join_practice_lobby_team()  # jump to unassigned players
        dota.channels.join_lobby_channel()

    @dota.channels.on(dota2.features.chat.ChannelManager.EVENT_JOINED_CHANNEL)
    def chat_joined(channel):
        logging.info(f"{dota.steam.username} joined chat channel {channel.name}")

    @client.on("disconnected")
    def handle_disconnect():
        logging.info(f"Disconnected: {credentials.login}")

    @dota.channels.on(dota2.features.chat.ChannelManager.EVENT_MESSAGE)
    def chat_message(channel: ChatChannel, msg: dota_gcmessages_client_chat_pb2.CMsgDOTAChatMessage):
        if channel.type != DOTAChatChannelType_t.DOTAChannelType_Lobby:
            return  # ignore postgame and other chats

        if not msg.text.startswith("!"):
            return

        actions = {"!start": lambda: dota.launch_practice_lobby()}

        actions.get(msg.text, lambda: logging.info(f"unsupported command: {msg.text}"))()

    logging.info("attempting login")
    client.login(credentials.login, credentials.password)
    logging.info("we running")
    client.run_forever()


logging.info("attempting spawn")
try:
    gevent.joinall([gevent.spawn(bot_loop, credentials)])
finally:
    logging.info("all greenlets exitted")
    if bot:
        bot.destroy_lobby()
        bot.exit()
        bot.steam.logout()
        logging.info("cleanup done")
