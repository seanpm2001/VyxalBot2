from typing import Optional, cast, Any
from time import time
from datetime import datetime
from pathlib import Path
from asyncio import create_task, wait_for
from html import unescape
from string import ascii_letters

import logging
import sys
import json
import os
import random
import re
import codecs
import base64
import subprocess

import tomli
import yaml

from aiohttp import ClientSession
from aiohttp.web import Application, Request, Response, run_app
from aiohttp.client_exceptions import ContentTypeError
from sechat import Bot, Room, MessageEvent, EventType
from gidgethub import HTTPException as GitHubHTTPException, ValidationError
from gidgethub.aiohttp import GitHubAPI as AsyncioGitHubAPI
from gidgethub.abc import GitHubAPI
from gidgethub.routing import Router
from gidgethub.sansio import Event as GitHubEvent
from gidgethub.apps import get_installation_access_token, get_jwt
from cachetools import LRUCache
from platformdirs import user_state_path
from dateutil.parser import parse as parseDatetime
from uwuivy import uwuipy

from vyxalbot2.userdb import UserDB
from vyxalbot2.util import (
    GITHUB_MERGE_QUEUE,
    formatUser,
    formatRepo,
    formatIssue,
    formatRef,
    msgify,
    RAPTOR,
)
from vyxalbot2.types import PublicConfigType, PrivateConfigType, MessagesType, AppToken
from vyxalbot2.commands import COMMAND_REGEXES, MESSAGE_REGEXES, COMMAND_ALIASES

__version__ = "2.0.0"


class VyxalBot2(Application):
    ADMIN_COMMANDS = ["die"]

    def __init__(
        self,
        publicConfig: PublicConfigType,
        privateConfig: PrivateConfigType,
        messages: MessagesType,
        storagePath: str,
        statuses: list[str],
    ) -> None:
        self.logger = logging.getLogger("VyxalBot2")
        super().__init__(logger=self.logger)

        self.publicConfig = publicConfig
        self.privateConfig = privateConfig
        self.messages = messages
        self.statuses = list(filter(lambda i: hash(i) != -327901152, statuses))
        self.userDB = UserDB(storagePath, self.publicConfig["groups"])
        self.errorsSinceStartup = 0
        self.lu = False

        self.bot = Bot(logger=self.logger)
        self._appToken: Optional[AppToken] = None
        self.session = ClientSession()
        self.cache = LRUCache(maxsize=5000)
        self.ghRouter = Router()
        self.gh = AsyncioGitHubAPI(self.session, "VyxalBot2", cache=self.cache)
        self.runningTasks = set()

        with open(self.privateConfig["pem"], "r") as f:
            self.privkey = f.read()

        self.router.add_post("/webhook", self.onHookRequest)
        self.on_startup.append(self.onStartup)
        self.on_cleanup.append(self.onShutdown)

        self.ghRouter.add(self.onPushAction, "push")
        self.ghRouter.add(self.onIssueAction, "issues")
        self.ghRouter.add(self.onPRAction, "pull_request")

        self.ghRouter.add(self.onThingCreated, "create")
        self.ghRouter.add(self.onThingDeleted, "delete")
        self.ghRouter.add(self.onReleaseCreated, "release", action="released")
        self.ghRouter.add(self.onFork, "fork")
        self.ghRouter.add(
            self.onReviewSubmitted, "pull_request_review", action="submitted"
        )

        self.ghRouter.add(self.onRepositoryCreated, "repository", action="created")
        self.ghRouter.add(self.onRepositoryDeleted, "repository", action="deleted")

    async def onStartup(self, _):
        await self.bot.authenticate(
            self.privateConfig["chat"]["email"],
            self.privateConfig["chat"]["password"],
            self.privateConfig["chat"]["host"],
        )
        self.room = await self.bot.joinRoom(self.privateConfig["chat"]["room"])
        self.room.register(self.onMessage, EventType.MESSAGE)
        await self.room.send(
            "Well, here we are again."
            if random.random() > 0.01
            else "GOOD MORNING, MOTHERF***ERS"
        )
        self.startupTime = datetime.now()

    async def onShutdown(self, _):
        try:
            await self.room.send("Ah'll be bahk.")
        except RuntimeError:
            pass
        await wait_for(
            self.bot.__aexit__(None, None, None), 6
        )  # DO NOT TRY THIS AT HOME
        await wait_for(self.session.close(), 3)

    async def appToken(self, gh: GitHubAPI) -> AppToken:
        if self._appToken != None:
            if self._appToken.expires.timestamp() > time():
                return self._appToken
        jwt = get_jwt(app_id=self.privateConfig["appID"], private_key=self.privkey)
        async for installation in gh.getiter(
            "/app/installations",
            jwt=jwt,
        ):
            if installation["account"]["login"] == self.privateConfig["account"]:
                tokenData = await get_installation_access_token(
                    gh,
                    installation_id=installation["id"],
                    app_id=self.privateConfig["appID"],
                    private_key=self.privkey,
                )
                self._appToken = AppToken(
                    tokenData["token"], parseDatetime(tokenData["expires_at"])
                )
                return self._appToken
        raise ValueError("Unable to locate installation")

    async def onHookRequest(self, request: Request) -> Response:
        event = None
        try:
            body = await request.read()
            event = GitHubEvent.from_http(
                request.headers, body, secret=self.privateConfig["webhookSecret"]
            )
            self.logger.info(f"Recieved delivery #{event.delivery_id} ({event.event})")
            if event.event == "ping":
                return Response(status=200)
            if repo := event.data.get("repository", False):
                if repo["visibility"] == "private":
                    return Response(status=200)
            await self.ghRouter.dispatch(event, self.gh)
            return Response(status=200)
        except Exception:
            self.errorsSinceStartup += 1
            if event:
                msg = f"An error occured while processing event {event.delivery_id}!"
            else:
                msg = f"An error occured while processing a request!"
            self.logger.exception(msg)
            try:
                await self.room.send(f"@Ginger " + msg)
            except RuntimeError:
                pass
            return Response(status=500)

    async def permissionsCommand(self, event: MessageEvent, args: dict[str, Any]):
        target = self.userDB.getUserInfo(
            int(args["user"]) if args["user"] != "me" else event.user_id
        )
        sender = self.userDB.getUserInfo(event.user_id)
        if not sender:
            await self.room.reply(
                event.message_id, "You are not in my database. Please run !!/register."
            )
            return
        if not target:
            await self.room.reply(event.message_id, "That user is not in my database.")
            return
        match args["action"]:
            case "list":
                await self.room.reply(
                    event.message_id,
                    f"User {target['name']} is a member of groups {', '.join(target['groups'])}.",
                )
            case "grant" | "revoke" as action:
                if not args.get("permission"):
                    await self.room.reply(
                        event.message_id,
                        "You need to specify a permission!",
                    )
                    return
                args["permission"] = args["permission"].removesuffix("s")
                try:
                    promotionRequires = self.publicConfig["groups"][
                        args["permission"]
                    ].get("promotionRequires", [])
                    if (not any([i in promotionRequires for i in sender["groups"]])) and len(promotionRequires):  # type: ignore
                        await self.room.reply(
                            event.message_id,
                            "Insufficient permissions!",
                        )
                        return
                except KeyError:
                    await self.room.reply(
                        event.message_id,
                        "No such group!",
                    )
                    return
                if action == "grant":
                    if self.userDB.addUserToGroup(target, args["permission"]):
                        await self.room.reply(
                            event.message_id,
                            f"User {target['name']} is now a member of group {args['permission']}.",
                        )
                    else:
                        await self.room.reply(
                            event.message_id,
                            f"User {target['name']} is already a member of group {args['permission']}.",
                        )
                else:
                    self.userDB.removeUserFromGroup(target, args["permission"])
                    await self.room.reply(
                        event.message_id,
                        f"User {target['name']} is no longer a member of group {args['permission']}.",
                    )

    async def runVyxalCommand(self, event: MessageEvent, args: dict[str, Any]):
        async with self.session.get(
            "https://vyxal.pythonanywhere.com/session"
        ) as sessionData:
            messageID = await self.room.reply(event.message_id, "Running...")
            async with self.session.post(
                f"https://vyxal.pythonanywhere.com/execute",
                data=json.dumps(
                    {
                        "code": args["code"],
                        "flags": args["flags"] if args["flags"] else "",
                        "footer": "",
                        "header": "",
                        "inputs": "",
                        "session": await sessionData.text(),
                    }
                ),
                headers={"Content-Type": "application/json"},
            ) as result:
                message = ""
                try:
                    responseJson = await result.json()
                except ContentTypeError:
                    await self.room.edit(
                        messageID,
                        f":{event.message_id} An error occured: " + await result.text(),
                    )
                else:
                    if responseJson["stdout"]:
                        message += "stdout:\n" + responseJson["stdout"].strip()
                    if responseJson["stderr"]:
                        message += "\nstderr:\n" + responseJson["stderr"].strip()
                    await self.room.edit(
                        messageID, f":{event.message_id} " + message.strip()
                    )

    async def runCommand(
        self, room: Room, event: MessageEvent, command: str, args: dict[str, Any]
    ):
        if event.user_id == room.userID:
            return
        for groupName, group in self.publicConfig["groups"].items():
            if command in group.get("canRun", []) and not (
                groupName in r["groups"]
                if (r := self.userDB.getUserInfo(event.user_id))
                else False
            ):
                await self.room.reply(
                    event.message_id,
                    f'You do not have permission to run that command (must be a member of group "{groupName}"). If you think you should be able to, ping Ginger.',
                )
                return
        match command:
            case "die":
                exit(-42)
            case "help":
                if commandName := args.get("command", ""):
                    if commandName == "me":
                        await self.room.reply(
                            event.message_id, "I'd love to, but I don't have any limbs."
                        )
                    else:
                        await self.room.reply(
                            event.message_id,
                            self.messages["commandhelp"].get(
                                commandName, "No help is available for that command."
                            ),
                        )
                else:
                    await self.room.reply(
                        event.message_id,
                        self.messages["help"].format(version=__version__)
                        + f"{', '.join(sorted(map(lambda i: i if not i.startswith('!') else COMMAND_ALIASES[i], set(COMMAND_REGEXES.values()))))}",
                    )
            case "info":
                await self.room.reply(event.message_id, self.messages["info"])
            case "status":
                if args.get("mood", ""):
                    msg = f"Bot status: Online\nUptime: {datetime.now() - self.startupTime}\nRunning since: {self.startupTime.isoformat()}\nErrors since startup: {self.errorsSinceStartup}"
                    match args.get("mood", ""):
                        case "boring":
                            pass
                        case "exciting":
                            msg = "\n".join(
                                line + ("!" * random.randint(2, 5))
                                for line in msg.upper().splitlines()
                            )
                        case "tingly":
                            uwu = uwuipy(None, 0.3, 0.2, 0.2, 1)  # type: ignore Me when the developers of uwuipy don't annotate their types correctly
                            msg = uwu.uwuify(msg)
                        case "sleepy":
                            msg = (
                                "\n".join(
                                    msg.splitlines()[
                                        : random.randint(1, len(msg.splitlines()))
                                    ]
                                )
                                + " *yawn*\n"
                                + "z" * random.randint(5, 10)
                            )
                        case "cryptic":
                            msg = codecs.encode(msg, "rot13")
                        case "goofy":
                            msg = "\n".join(
                                map(
                                    lambda i: i + "🤓" * random.randint(1, 3),
                                    msg.splitlines(),
                                )
                            )
                    await self.room.reply(event.message_id, msg)
                else:
                    if self.lu:
                        await self.room.reply(
                            event.message_id, f"I am doing {event.user_name}."
                        )
                        self.lu = False
                    else:
                        await self.room.reply(
                            event.message_id,
                            (
                                i + "."
                                if not (i := random.choice(self.statuses)).endswith(".")
                                and i.endswith(tuple(ascii_letters))
                                else i.removesuffix(";")
                            ),
                        )
                        if hash(i) == -322735823:
                            self.lu = True
            case "permissions":
                await self.permissionsCommand(event, args)
            case "register":
                if self.userDB.getUserInfo(event.user_id):
                    await self.room.reply(
                        event.message_id,
                        "You are already registered. To refresh your details, use !!/refresh.",
                    )
                    return
                self.userDB.addUserToDatabase(
                    await (
                        await self.session.get(
                            f"https://chat.stackexchange.com/users/thumbs/{event.user_id}"
                        )
                    ).json()
                )
                await self.room.reply(
                    event.message_id,
                    "You have been registered! You don't have any permissions yet; ping an admin if you think you should.",
                )
            case "refresh":
                if not (info := self.userDB.getUserInfo(event.user_id)):
                    await self.room.reply(
                        event.message_id,
                        "You are not in my database. Please run !!/register.",
                    )
                    return
                self.userDB.refreshUserData(
                    await (
                        await self.session.get(
                            f"https://chat.stackexchange.com/users/thumbs/{event.user_id}"
                        )
                    ).json()
                )
                await self.room.reply(
                    event.message_id, "Your details have been updated."
                )
            case "groups":
                match args["action"]:
                    case "list":
                        await self.room.reply(
                            event.message_id,
                            f"All groups: {', '.join(self.publicConfig['groups'].keys())}",
                        )
                    case "members":
                        args["group"] = args["group"].removesuffix("s")
                        await self.room.reply(
                            event.message_id,
                            f"Members of group {args['group']}: {', '.join(map(lambda i: i['name'], self.userDB.membersOfGroup(args['group'])))}",
                        )
            case "ping":
                args["group"] = args["group"].removesuffix("s")
                if not len(
                    message := " ".join(
                        [
                            "@" + user["name"].replace(" ", "")
                            for user in self.userDB.membersOfGroup(args["group"])
                            if user["id"] != event.user_id
                        ]
                    )
                ):
                    await self.room.send("Nobody to ping.")
                else:
                    await self.room.send(message + " ^")
            case "coffee":
                await self.room.send(
                    f"@{event.user_name if args['user'] == 'me' else args['user']} Here's your coffee: ☕"
                )
            case "maul":
                if args["user"].lower() == "vyxalbot":
                    await self.room.send("No.")
                else:
                    await self.room.send(RAPTOR.format(user=args["user"].upper()))
            case "cookie":
                if info := self.userDB.getUserInfo(event.user_id):
                    if "admin" in info["groups"]:
                        await self.room.reply(event.message_id, "Here you go: 🍪")
                        return
                if random.random() <= 0.75:
                    await self.room.reply(event.message_id, "Here you go: 🍪")
                else:
                    await self.room.reply(event.message_id, "No.")
            case "hug":
                await self.room.reply(
                    event.message_id, random.choice(self.messages["hugs"])
                )
            case "!repo-list":
                await self.room.reply(
                    event.message_id,
                    "Repositories: "
                    + " | ".join(
                        [
                            formatRepo(item, False)
                            async for item in self.gh.getiter(
                                f"/users/{self.privateConfig['account']}/repos",
                                {"sort": "created"},
                                oauth_token=(await self.appToken(self.gh)).token,
                            )
                        ][:5]
                    ),
                )
            case "!issue-open":
                try:
                    repo = args["repo"] or self.privateConfig["baseRepo"]
                    # ICKY SPECIAL CASING
                    if repo == "Vyxal":
                        if not isinstance(args["labels"], str):
                            return await self.room.reply(
                                event.message_id,
                                'You must specify one of "version-2" or "version-3" as a label!',
                            )
                        if "version-3" not in args["labels"].split(
                            ";"
                        ) and "version-2" not in args["labels"].split(";"):
                            return await self.room.reply(
                                event.message_id,
                                'You must specify one of "version-2" or "version-3" as a label!',
                            )
                    await self.gh.post(
                        f"/repos/{self.privateConfig['account']}/{repo}/issues",
                        data={
                            "title": args["title"],
                            "body": args["content"]
                            + f"\n\n_Issue created by {event.user_name} [here]({f'https://chat.stackexchange.com/transcript/{event.room_id}?m={event.message_id}#{event.message_id}'})_",
                            "labels": (
                                args["labels"].split(";") if args["labels"] else []
                            ),
                        },
                        oauth_token=(await self.appToken(self.gh)).token,
                    )
                except GitHubHTTPException as e:
                    await self.room.reply(
                        event.message_id,
                        f"Failed to create issue: {e.status_code.value} {e.status_code.description}",
                    )
            case "sus":
                if (
                    "__msg__" in args
                    and random.random() >= 0.25
                    and event.user_id != self.room.userID
                ):
                    return
                await self.room.reply(event.message_id, "ඞ" * random.randint(8, 64))
            case "amilyxal":
                await self.room.reply(
                    event.message_id,
                    f"You are {'' if (event.user_id == 354515) != (random.random() <= 0.1) else 'not '}lyxal.",
                )
            case "prod":
                if (
                    repo := (
                        args["repo"] if args["repo"] else self.privateConfig["baseRepo"]
                    )
                ) not in self.publicConfig["production"].keys():
                    return await self.room.reply(
                        event.message_id,
                        f"That repository isn't listed in config.json.",
                    )
                try:
                    await self.gh.post(
                        f"/repos/{self.privateConfig['account']}/{repo}/pulls",
                        data={
                            "title": f"Update production ({datetime.now().strftime('%b %d %Y')})",
                            "head": self.publicConfig["production"][repo]["head"],
                            "base": self.publicConfig["production"][repo]["base"],
                            "body": f"Requested by {event.user_name} [here]({f'https://chat.stackexchange.com/transcript/{event.room_id}?m={event.message_id}#{event.message_id})'}.",
                        },
                        oauth_token=(await self.appToken(self.gh)).token,
                    )
                except ValidationError as e:
                    await self.room.reply(
                        event.message_id,
                        f"Failed to create issue: Webhook validation failed: {str(e)}",
                    )
                except GitHubHTTPException as e:
                    await self.room.reply(
                        event.message_id,
                        f"Failed to create issue: {e.status_code.value} {e.status_code.description}",
                    )
            case "idiom":
                match args["action"]:
                    case "add":
                        file = await self.gh.getitem(
                            f"/repos/{self.privateConfig['account']}/vyxal.github.io/contents/src/data/idioms.yaml",
                            oauth_token=(await self.appToken(self.gh)).token,
                        )
                        idioms = yaml.safe_load(base64.b64decode(file["content"]))
                        if not idioms:
                            idioms = []
                        idioms.append(
                            {
                                "name": args["title"],
                                "code": args["code"],
                                "description": args["description"],
                                "link": "#"
                                + base64.b64encode(
                                    json.dumps(["", "", "", args["code"], ""]).encode(
                                        "utf-8"
                                    )
                                ).decode("utf-8"),
                                "keywords": args["keywords"].split(),
                            }
                        )
                        await self.gh.put(
                            f"/repos/{self.privateConfig['account']}/vyxal.github.io/contents/src/data/idioms.yaml",
                            data={
                                "message": f"Added \"{args['title']}\" to the idiom list.\nRequested by {event.user_name} here: {f'https://chat.stackexchange.com/transcript/{event.room_id}?m={event.message_id}#{event.message_id}'}",
                                "content": base64.b64encode(
                                    yaml.dump(
                                        idioms, encoding="utf-8", allow_unicode=True
                                    )
                                ).decode("utf-8"),
                                "sha": file["sha"],
                            },
                            oauth_token=(await self.appToken(self.gh)).token,
                        )
                        await self.room.reply(
                            event.message_id, f"Idiom added successfully."
                        )
            case "run":
                await self.room.reply(event.message_id, "This command is disabled.")
                return
                task = create_task(self.runVyxalCommand(event, args))
                task.add_done_callback(self.runningTasks.discard)
                self.runningTasks.add(task)
            case "blame":
                await self.room.reply(
                    event.message_id,
                    f"It was {random.choice(self.userDB.users())['name']}'s fault!",
                )
            case "!good-bot":
                await self.room.send(":3")
            case "hello":
                await self.room.reply(
                    event.message_id, random.choice(self.messages["hello"])
                )
            case "goodbye":
                await self.room.reply(
                    event.message_id, random.choice(self.messages["goodbye"])
                )
            case "pull":
                if subprocess.run(["git", "pull"]).returncode == 0:
                    await self.room.reply(event.message_id, "Restarting...")
                    exit(-43)
                else:
                    await self.room.reply(event.message_id, "Failed to pull!")

    async def onMessage(self, room: Room, event: MessageEvent):
        try:
            if match := re.fullmatch(r"!!\/(?P<command>.+)", unescape(event.content)):
                rawCommand = match["command"]
                for regex, command in COMMAND_REGEXES.items():
                    if match := re.fullmatch(regex, rawCommand):
                        return await self.runCommand(
                            room, event, command, match.groupdict()
                        )
                return await self.room.send(
                    f"Sorry {event.user_name}, I'm afraid I can't do that."
                )
            for regex, command in MESSAGE_REGEXES.items():
                if match := re.fullmatch(regex, unescape(event.content), re.I):
                    await self.runCommand(
                        room, event, command, match.groupdict() | {"__msg__": True}
                    )
        except Exception:
            msg = (
                f"@Ginger An error occurred while handling message {event.message_id}!"
            )
            await self.room.send(msg)
            self.logger.exception(msg)
            self.errorsSinceStartup += 1

    async def autoTagPR(self, event: GitHubEvent, gh: GitHubAPI):
        pullRequest = event.data["pull_request"]
        if (
            event.data["repository"]["name"]
            not in self.publicConfig["importantRepositories"]
        ):
            return
        if len(pullRequest["labels"]):
            return
        
        token = (await self.appToken(gh)).token
        autotagConfig = self.publicConfig["autotag"].get(
            event.data["repository"]["full_name"], self.publicConfig["autotag"].get("*", {"prregex": {}, "issue2pr": {}})
        )
        tags = set()
        for regex, tag in autotagConfig["prregex"].items():
            if re.fullmatch(regex, pullRequest["head"]["ref"]) is not None:
                tags.add(tag)
        if pullRequest["body"]:
            for match in re.finditer(
                r"(([Cc]lose[sd]?)|([Ff]ix(e[sd])?)|([Rr]esolve[sd]?)) #(?P<number>\d+)",
                pullRequest["body"],
            ):
                issue = await gh.getitem(
                    f"/repos/{event.data['repository']['full_name']}/issues/{int(match.group('number'))}",
                    oauth_token=token,
                )
                tags.update(
                    filter(
                        None,
                        map(
                            lambda i: autotagConfig["issue2pr"].get(i["name"], False),
                            issue["labels"],
                        ),
                    )
                )

        await gh.patch(
            f"/repos/{event.data['repository']['full_name']}/issues/{pullRequest['number']}",
            data={"labels": list(tags)},
            oauth_token=token,
        )

    async def onPushAction(self, event: GitHubEvent, gh: GitHubAPI):
        if (
            event.data["ref"].split("/")[1] != "heads"
            or event.data["pusher"]["name"] == GITHUB_MERGE_QUEUE
        ):
            return  # It's probably a tag push
        branch = event.data["ref"].split("/")[2]
        for commit in event.data["commits"]:
            if not commit["distinct"]:
                continue
            if event.data["pusher"]["name"] == event.data["sender"]["login"]:
                user = formatUser(event.data["sender"])
            else:
                user = event.data["pusher"]["name"]
            await self.room.send(
                f"{user} {'force-pushed' if event.data['forced'] else 'pushed'} a [commit]({commit['url']}) to {formatRef(branch, event.data['repository'])} in {formatRepo(event.data['repository'])}: {commit['message'].splitlines()[0]}"
            )

    async def onIssueAction(self, event: GitHubEvent, gh: GitHubAPI):
        issue = event.data["issue"]
        match event.data["action"]:
            case "assigned":
                assignee = event.data["assignee"]
                self.logger.info(
                    f'Issue {issue["number"]} assigned to {assignee["login"]} by {event.data["sender"]["login"]} in {issue["repository_url"]}'
                )
                await self.room.send(
                    f'{formatUser(event.data["sender"])} assigned {formatUser(assignee)} to issue {formatIssue(issue)} in {formatRepo(event.data["repository"])}'
                )
                if assignee["login"] == event.data["sender"]["login"]:
                    await self.room.send(
                        "https://i.stack.imgur.com/1VzAJ.jpg"
                    )  # Obama gives himself a medal image
            case "unassigned":
                issue = event.data["issue"]
                assignee = event.data["assignee"]
                self.logger.info(
                    f'Issue {issue["number"]} unassigned from {assignee["login"]} by {event.data["sender"]["login"]} in {issue["repository_url"]}'
                )
                await self.room.send(
                    f'{formatUser(event.data["sender"])} unassigned {formatUser(assignee)} from issue {formatIssue(issue)} in {formatRepo(event.data["repository"])}'
                )
            case "closed":
                self.logger.info(
                    f'Issue {issue["number"]} closed as {issue["state_reason"]} in {issue["repository_url"]}'
                )
                await self.room.send(
                    f'{formatUser(event.data["sender"])} closed issue {formatIssue(issue)} as {issue["state_reason"]} in {formatRepo(event.data["repository"])}'
                )
            case _ as action if action in ["opened", "reopened"]:
                self.logger.info(
                    f'Issue {issue["number"]} {action} in {issue["repository_url"]}'
                )
                await self.room.send(
                    f'{formatUser(event.data["sender"])} {action} issue {formatIssue(issue)} in {formatRepo(event.data["repository"])}'
                )

    async def onPRAction(self, event: GitHubEvent, gh: GitHubAPI):
        pullRequest = event.data["pull_request"]
        match event.data["action"]:
            case "assigned":
                assignee = event.data["assignee"]
                self.logger.info(
                    f'Pull request {pullRequest["number"]} assigned to {assignee["login"]} by {event.data["sender"]["login"]} in {event.data["repository"]["html_url"]}'
                )
                await self.room.send(
                    f'{formatUser(event.data["sender"])} assigned {formatUser(assignee)} to pull request {formatIssue(pullRequest)} in {formatRepo(event.data["repository"])}'
                )
            case "unassigned":
                pullRequest = event.data["pull_request"]
                assignee = event.data["assignee"]
                self.logger.info(
                    f'Pull request {pullRequest["number"]} unassigned from {assignee["login"]} by {event.data["sender"]["login"]} in {event.data["repository"]["html_url"]}'
                )
                await self.room.send(
                    f'{formatUser(event.data["sender"])} unassigned {formatUser(assignee)} from pull request {formatIssue(pullRequest)} in {formatRepo(event.data["repository"])}'
                )
            case "closed":
                self.logger.info(
                    f'Pull request {pullRequest["number"]} {"merged" if pullRequest["merged"] else "closed"} in {event.data["repository"]["html_url"]}'
                )
                await self.room.send(
                    f'{formatUser(event.data["sender"])} {"merged" if pullRequest["merged"] else "closed"} pull request {formatIssue(pullRequest)} in {formatRepo(event.data["repository"])}'
                )
            case "review_requested":
                return  # user doesn't want this apparently
                await self.room.send(
                    f'{formatUser(event.data["sender"])} requested {formatUser(event.data["requested_reviewer"])}\'s review on {formatIssue(pullRequest)}'
                )
            case "ready_for_review":
                await self.room.send(
                    f'{formatUser(event.data["sender"])} marked pull request {formatIssue(pullRequest)} ready for review'
                )
                
            case _ as action if action in ["opened", "reopened", "enqueued"]:
                self.logger.info(
                    f'Pull request {pullRequest["number"]} {action} in {event.data["repository"]["html_url"]}'
                )
                await self.room.send(
                    f'{formatUser(event.data["sender"])} {action} pull request {formatIssue(pullRequest)} in {formatRepo(event.data["repository"])}'
                )
                if action == "opened":
                    await self.autoTagPR(event, gh)

    async def onThingCreated(self, event: GitHubEvent, gh: GitHubAPI):
        if event.data["ref_type"] == "tag":
            return
        if event.data["sender"]["login"] == GITHUB_MERGE_QUEUE:
            return
        self.logger.info(
            f'{event.data["sender"]["login"]} created {event.data["ref_type"]} {event.data["ref"]} in {event.data["repository"]["html_url"]}'
        )
        await self.room.send(
            f'{formatUser(event.data["sender"])} created {event.data["ref_type"]} {event.data["ref"]} in {formatRepo(event.data["repository"])}'
        )

    async def onThingDeleted(self, event: GitHubEvent, gh: GitHubAPI):
        if (
            event.data["ref_type"] == "tag"
            or event.data["sender"]["login"] == GITHUB_MERGE_QUEUE
        ):
            return
        self.logger.info(
            f'{event.data["sender"]["login"]} deleted {event.data["ref_type"]} {event.data["ref"]} in {event.data["repository"]["html_url"]}'
        )
        await self.room.send(
            f'{formatUser(event.data["sender"])} deleted {event.data["ref_type"]} {event.data["ref"]} in {formatRepo(event.data["repository"])}'
        )

    async def onReleaseCreated(self, event: GitHubEvent, gh: GitHubAPI):
        release = event.data["release"]
        self.logger.info(
            f'{event.data["sender"]["login"]} released {release["html_url"]}'
        )

        releaseName = release["name"].lower()
        # attempt to match version number, otherwise default to previous behaviour
        if match := re.search(r"\d.*", releaseName):
            releaseName = match[0]
        message = await self.room.send(
            f'__[{event.data["repository"]["name"]} {releaseName}]({release["html_url"]})__'
        )
        if (
            event.data["repository"]["name"]
            in self.publicConfig["importantRepositories"]
        ):
            await self.room.pin(message)

    async def onFork(self, event: GitHubEvent, gh: GitHubAPI):
        self.logger.info(
            f'{event.data["sender"]["login"]} forked {event.data["forkee"]["full_name"]} from {event.data["repository"]["full_name"]}'
        )
        await self.room.send(
            f'{formatUser(event.data["sender"])} forked {formatRepo(event.data["forkee"])} from {formatRepo(event.data["repository"])}'
        )

    async def onReviewSubmitted(self, event: GitHubEvent, g: GitHubAPI):
        review = event.data["review"]
        match review["state"]:
            case "commented":
                if not review["body"]:
                    return
                action = "commented on"
            case "approved":
                action = "approved"
            case "changes_requested":
                action = "requested changes on"
            case _:
                action = "did something to"
        await self.room.send(
            f'{formatUser(event.data["sender"])} [{action}]({review["html_url"]}) {formatIssue(event.data["pull_request"])} in {formatRepo(event.data["repository"])}'
            + (': "' + msgify(review["body"]) + '"' if review["body"] else "")
        )

    async def onRepositoryCreated(self, event: GitHubEvent, g: GitHubAPI):
        await self.room.send(
            f'{formatUser(event.data["sender"])} created repository {formatRepo(event.data["repository"])}'
        )

    async def onRepositoryDeleted(self, event: GitHubEvent, g: GitHubAPI):
        await self.room.send(
            f'{formatUser(event.data["sender"])} deleted repository {formatRepo(event.data["repository"])}'
        )


def run():
    PUBLIC_CONFIG_PATH = os.environ.get("VYXALBOT_CONFIG_PUBLIC", "config.json")
    PRIVATE_CONFIG_PATH = os.environ.get("VYXALBOT_CONFIG_PRIVATE", "private.json")
    STORAGE_PATH = user_state_path("vyxalbot2", None, __version__)
    os.makedirs(STORAGE_PATH, exist_ok=True)
    DATA_PATH = Path(__file__).resolve().parent.parent / "data"
    MESSAGES_PATH = DATA_PATH / "messages.toml"
    STATUSES_PATH = DATA_PATH / "statuses.txt"

    logging.basicConfig(
        format="[%(name)s] %(levelname)s: %(message)s",
        stream=sys.stdout,
        level=logging.DEBUG,
    )

    with open(PUBLIC_CONFIG_PATH, "r") as f:
        publicConfig = json.load(f)
    with open(PRIVATE_CONFIG_PATH, "r") as f:
        privateConfig = json.load(f)
    with open(MESSAGES_PATH, "rb") as f:
        messages = tomli.load(f)
    with open(STATUSES_PATH, "r") as f:
        statuses = f.read().splitlines()

    async def makeApp():
        return VyxalBot2(
            publicConfig,
            privateConfig,
            cast(Any, messages),
            str(STORAGE_PATH / "storage.json"),
            statuses,
        )

    run_app(makeApp(), port=privateConfig["port"])
