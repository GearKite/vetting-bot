import logging
import random
import re
import time

from nio import (
    AsyncClient,
    MatrixRoom,
    RoomCreateError,
    RoomMessageText,
    RoomPutStateResponse,
    RoomSendError,
)

from vetting_bot.chat_functions import react_to_event, send_text_to_room
from vetting_bot.config import Config
from vetting_bot.storage import Storage
from vetting_bot.timer import Timer

logger = logging.getLogger(__name__)


class Command:
    def __init__(
        self,
        client: AsyncClient,
        store: Storage,
        config: Config,
        command: str,
        room: MatrixRoom,
        event: RoomMessageText,
    ):
        """A command made by a user.

        Args:
            client: The client to communicate to matrix with.

            store: Bot storage.

            config: Bot configuration parameters.

            command: The command and arguments.

            room: The room the command was sent in.

            event: The event describing the command.
        """
        self.client = client
        self.store = store
        self.config = config
        self.command = command
        self.room = room
        self.event = event
        self.args = self.command.split()[1:]

    async def process(self):
        """Process the command"""
        if self.command.startswith("echo"):
            await self._echo()
        elif self.command.startswith("react"):
            await self._react()
        elif self.command.startswith("help"):
            await self._show_help()
        elif self.command.startswith("start"):
            await self._start_vetting()
        elif self.command.startswith("vote"):
            await self._start_vote()
        else:
            await self._unknown_command()

    async def _echo(self):
        """Echo back the command's arguments"""
        response = " ".join(self.args)
        await send_text_to_room(self.client, self.room.room_id, response)

    async def _react(self):
        """Make the bot react to the command message"""
        # React with a start emoji
        reaction = "⭐"
        await react_to_event(
            self.client, self.room.room_id, self.event.event_id, reaction
        )

        # React with some generic text
        reaction = "Some text"
        await react_to_event(
            self.client, self.room.room_id, self.event.event_id, reaction
        )

    async def _show_help(self):
        """Show the help text"""
        if not self.args:
            text = (
                "Hello, I am the vetting bot! Use `help commands` to view "
                "available commands."
            )
            await send_text_to_room(self.client, self.room.room_id, text)
            return

        topic = self.args[0]
        if topic == "rules":
            text = "These are the rules!"
        elif topic == "commands":
            text = "Available commands: \n" "`start {user_id}`\n" "`vote {user_id}`\n"
        else:
            text = "Unknown help topic!"
        await send_text_to_room(self.client, self.room.room_id, text)

    async def _start_vetting(self):
        """Starts the vetting process"""
        if self.room.room_id != self.config.vetting_room_id:
            text = f"This command can only be used in https://matrix.to/#/{self.config.vetting_room_id} !"
            await send_text_to_room(self.client, self.room.room_id, text)
            return
        if not self.args:
            text = "Usage: `start {user_id}\nExample: `start @someone:example.com`"
            await send_text_to_room(self.client, self.room.room_id, text)
            return

        vetted_user_id = self.args[0]

        if not validate_user_id(vetted_user_id):
            text = (
                "The entered user id is invalid. "
                f"It should be in the format of `{self.client.user_id}`"
            )
            await send_text_to_room(self.client, self.room.room_id, text)
            return

        # Check if vetting room already exists for user
        self.store.cursor.execute(
            "SELECT room_id FROM vetting WHERE mxid=?", (vetted_user_id,)
        )
        row = self.store.cursor.fetchone()
        if row is not None:
            text = f"A vetting room already exists for this user: https://matrix.to/#/{row[0]}"
            await send_text_to_room(self.client, self.room.room_id, text)
            return

        # Get members to invite
        invitees = [member_id for member_id in self.room.users.keys()]
        invitees.append(vetted_user_id)
        invitees.remove(self.client.user_id)

        # Create new room
        random_string = hex(random.randrange(4096, 65535))[2:].upper()
        initial_state = [
            {  # Enable encryption
                "type": "m.room.encryption",
                "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                "state_key": "",
            }
        ]
        room_resp = await self.client.room_create(
            name=f"Vetting {random_string}",
            invite=invitees,
            initial_state=initial_state,
        )

        if isinstance(room_resp, RoomCreateError):
            text = f"Unable to create room: {room_resp}"
            await send_text_to_room(self.client, self.room.room_id, text)
            logging.error(room_resp, stack_info=True)
            return

        # Create new vetting entry
        self.store.cursor.execute(
            "INSERT INTO vetting (mxid, room_id, vetting_create_time) VALUES (?, ?, ?)",
            (vetted_user_id, room_resp.room_id, time.time()),
        )

        # Add newly created room to space
        space_child_content = {
            "suggested": False,
            "via": [self.client.server],
        }
        space_resp = await self.client.room_put_state(
            room_id=self.config.vetting_space_id,
            event_type="m.space.child",
            content=space_child_content,
            state_key=room_resp.room_id,
        )
        if not isinstance(space_resp, RoomPutStateResponse):
            logging.error(space_resp, exc_info=True)

    async def _start_vote(self):
        """Starts the vote"""
        if self.room.room_id != self.config.vetting_room_id:
            text = f"This command can only be used in https://matrix.to/#/{self.config.vetting_room_id} !"
            await send_text_to_room(self.client, self.room.room_id, text)
            return
        if not self.args:
            text = "Usage: `vote {user_id}\nExample: `vote @someone:example.com`"
            await send_text_to_room(self.client, self.room.room_id, text)
            return

        vetted_user_id = self.args[0]

        # Check if vetting room exists for user and poll hasn't been started yet
        self.store.cursor.execute(
            "SELECT room_id, poll_event_id FROM vetting WHERE mxid=?", (vetted_user_id,)
        )
        row = self.store.cursor.fetchone()
        if row is None:
            text = "This user hasn't been vetted, can't vote on them!"
            await send_text_to_room(self.client, self.room.room_id, text)
            return
        if row[1] is not None:
            event_link = f"https://matrix.to/#/{self.config.vetting_room_id}/{row[1]}?via={self.client.server}"
            text = (
                f"A poll has already been started for this user: {event_link}"
            )
            await send_text_to_room(self.client, self.room.room_id, text)
            return

        poll_text = f"Accept {vetted_user_id} into the Federation?"
        choices = ["yes", "no", "blank"]
        choices_text = "".join(
            [
                f"\n{i}. {choice.title()}"
                for choice, i in zip(choices, range(1, len(choices) + 1))
            ]
        )
        answers = [
            {"id": choice, "org.matrix.msc1767.text": choice.title()}
            for choice in choices
        ]

        event_content = {
            "org.matrix.msc1767.text": f"{poll_text}{choices_text}",
            "org.matrix.msc3381.poll.start": {
                "kind": "org.matrix.msc3381.poll.disclosed",
                "max_selections": 1,
                "question": {
                    "org.matrix.msc1767.text": poll_text,
                },
                "answers": answers,
            },
        }

        poll_resp = await self.client.room_send(
            self.room.room_id,
            message_type="org.matrix.msc3381.poll.start",
            content=event_content,
        )

        if isinstance(poll_resp, RoomSendError):
            logging.error(poll_resp, stack_info=True)
            text = "Failed to send poll."
            await send_text_to_room(self.client, self.room.room_id, text)
            return

        voting_start_time = time.time()

        self.store.cursor.execute(
            "UPDATE vetting SET poll_event_id = ?, voting_start_time = ? WHERE mxid = ?",
            (poll_resp.event_id, voting_start_time, vetted_user_id),
        )

        timer = Timer(self.client, self.store, self.config)
        await timer.wait_for_poll_end(
            vetted_user_id, poll_resp.event_id, voting_start_time
        )

    async def _unknown_command(self):
        await send_text_to_room(
            self.client,
            self.room.room_id,
            f"Unknown command '{self.command}'. Try the 'help' command for more information.",
        )


def validate_user_id(user_id):
    return (
        re.match(
            (
                r"^@[!-9;-~]*:"
                r"((\d{1,3}\.){3}\d{1,3}|\[[0-9A-Fa-f:.]{2,45}\]|[0-9A-Za-z.-]{1,255})(:\d{1,5})?$"
            ),
            user_id,
        )
        is not None
    )
