from datetime import timezone, datetime
from local_logging import logging
import os
import gevent
from dataclasses import dataclass
from enum import IntEnum

from steam.client import SteamClient, SteamID

import dota2
from dota2.client import Dota2Client
from dota2.enums import DOTA_GC_TEAM
from dota2.features.chat import ChatChannel
from dota2.proto_enums import DOTAChatChannelType_t
from dota2.protobufs import dota_gcmessages_client_chat_pb2
import db


class LobbyState(IntEnum):
    UI = 0
    READYUP = 4
    SERVERSETUP = 1
    RUN = 2
    POSTGAME = 3
    NOTREADY = 5
    SERVERASSIGN = 6


lobby_strings = {
    0: "UI",
    4: "READYUP",
    1: "SERVERSETUP",
    2: "RUN",
    3: "POSTGAME",
    5: "NOTREADY",
    6: "SERVERASSIGN",
}


game_servers = {
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


@dataclass
class Lobby:
    dota_client: Dota2Client
    db_engine: db.Engine
    start_votes: int
    game_start_time: datetime
    game_end_time: datetime
    queue_id: int


def create_new_lobby(dota: Dota2Client):
    dota.lobby_options = {
        "game_name": "dynia testing",
        "game_mode": dota2.enums.DOTA_GameMode.DOTA_GAMEMODE_CM,
        "server_region": game_servers[dota.server],
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


def kick_not_in_queue(dota: Dota2Client):
    # kick people from teams if they are not in queue
    players_steam = {
        SteamID(player.id).as_32: player
        for player in dota.lobby.all_members
        if player.id and player.team in (DOTA_GC_TEAM.GOOD_GUYS, DOTA_GC_TEAM.BAD_GUYS)
    }

    with db.Session(db.engine) as session:
        ladder_queue = session.query(db.LadderQueue).first()
        if not ladder_queue:
            logging.warning("queue got deleted, no players left")
            return

        players = ladder_queue.ladder_queueplayer_collection
        players_queue = [player.ladder_player.dota_id for player in players]

    # logging.warning(f"Players calculated: {players_queue}, {players_steam}")

    unnecesarry_people = (player for player in players_steam if str(player) not in players_queue)

    for player in unnecesarry_people:
        dota.channels.lobby.send(f"{players_steam[player].name}, you are not in this queue.")
        dota.practice_lobby_kick_from_team(player)


def invite_players(dota: Dota2Client):
    lobby_members = [p.id for p in dota.lobby.all_members]

    with db.Session(db.engine) as session:
        ladder_queue = session.query(db.LadderQueue).first()
        if not ladder_queue:
            logging.warning("queue got deleted, no players left")
            return
        players = [SteamID(player.ladder_player.dota_id) for player in ladder_queue.ladder_queueplayer_collection]

    logging.info(f"Inviting players: {players}")

    for player_steam_id in players:
        if player_steam_id not in lobby_members:
            dota.invite_to_lobby(player_steam_id)


def handle_cmd_start(lobby: Lobby, msg):
    lobby.start_votes[msg.account_id] = True

    votes_needed = 2
    if len(lobby.start_votes) < 2:
        lobby.dota_client.channels.lobby.send(
            "I need {} more votes to start.".format(votes_needed - len(bot.start_votes))
        )
        return

    lobby.dota_client.channels.lobby.send("Ok, let's go! GL HF")
    gevent.sleep(2)


def start_game(lobby: Lobby):
    lobby.game_start_time = timezone.now()

    # with db.Session(lobby.db_engine) as session:
    # session.

    # if bot.queue:
    #     bot.queue.active = False
    #     bot.queue.game_start_time = bot.game_start_time
    #     bot.queue.save()

    lobby.dota_client.launch_practice_lobby()


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

    with db.Session(db.engine) as session:
        # there's single ladder settings in db
        if not session.query(db.LadderSettings).first().use_queue:
            logging.error("Not a queue based ladder. Incompatible dota_bot")
            return

    lobbies = {}

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
        logging.info(f"lobby state change: {lobby_strings.get(lobby.state, int(lobby.state))}")

        if not lobby:
            logging.warning("Got lobby_changed with None lobby object")
            return

        # TODO: add separate per state handlers
        if int(lobby.state) == LobbyState.UI:
            kick_not_in_queue(dota)

    @dota.on(dota2.features.Lobby.EVENT_LOBBY_NEW)
    def dota_lobby_new(lobby: dota2.features.Lobby):
        logging.info(f"found new lobby: {dota.steam.username} {lobby.lobby_id}")

        dota.join_practice_lobby_team()  # jump to unassigned players
        dota.channels.join_lobby_channel()
        invite_players(dota)

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

        actions = {
            "!start": lambda: dota.launch_practice_lobby(),
            "!forcestart": lambda: dota.launch_practice_lobby(),
        }

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
