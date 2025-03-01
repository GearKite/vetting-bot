import logging

from nio import (
    AsyncClient,
    InviteMemberEvent,
    JoinError,
    MatrixRoom,
    MegolmEvent,
    ReactionEvent,
    RoomGetEventError,
    RoomMessageText,
    UnknownEvent,
)

from vetting_bot.bot_commands import Command
from vetting_bot.chat_functions import make_pill, send_text_to_room
from vetting_bot.config import Config
from vetting_bot.message_responses import Message
from vetting_bot.storage import Storage

logger = logging.getLogger(__name__)


class Callbacks:
    def __init__(self, client: AsyncClient, store: Storage, config: Config):
        """
        Args:
            client: nio client used to interact with matrix.

            store: Bot storage.

            config: Bot configuration parameters.
        """
        self.client = client
        self.store = store
        self.config = config
        self.command_prefix = config.command_prefix

    async def message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        """Callback for when a message event is received

        Args:
            room: The room the event came from.

            event: The event defining the message.
        """
        # Extract the message text
        msg = event.body

        # Ignore messages from ourselves
        if event.sender == self.client.user:
            return

        logger.debug(
            f"Bot message received for room {room.display_name} | "
            f"{room.user_name(event.sender)}: {msg}"
        )

        # Process as message if in a public room without command prefix
        has_command_prefix = msg.startswith(self.command_prefix)

        # room.is_group is often a DM, but not always.
        # room.is_group does not allow room aliases
        # room.member_count > 2 ... we assume a public room
        # room.member_count <= 2 ... we assume a DM
        if not has_command_prefix:
            # General message listener
            message = Message(self.client, self.store, self.config, msg, room, event)
            await message.process()
            return

        # Otherwise if this is in a 1-1 with the bot or features a command prefix,
        # treat it as a command
        if has_command_prefix:
            # Remove the command prefix
            msg = msg[len(self.command_prefix) :]

        command = Command(self.client, self.store, self.config, msg, room, event)
        await command.process()

    async def invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        """Callback for when an invite is received. Join the room specified in the invite.

        Args:
            room: The room that we are invited to.

            event: The invite event.
        """
        logger.debug(f"Got invite to {room.room_id} from {event.sender}.")

        # Attempt to join 3 times before giving up
        for attempt in range(3):
            result = await self.client.join(room.room_id)
            if type(result) == JoinError:
                logger.error(
                    f"Error joining room {room.room_id} (attempt %d): %s",
                    attempt,
                    result.message,
                )
            else:
                break
        else:
            logger.error("Unable to join room: %s", room.room_id)

        # Successfully joined room
        logger.info(f"Joined {room.room_id}")

    async def invite_event_filtered_callback(
        self, room: MatrixRoom, event: InviteMemberEvent
    ) -> None:
        """
        Since the InviteMemberEvent is fired for every m.room.member state received
        in a sync response's `rooms.invite` section, we will receive some that are
        not actually our own invite event (such as the inviter's membership).
        This makes sure we only call `callbacks.invite` with our own invite events.
        """
        if event.state_key == self.client.user_id:
            # This is our own membership (invite) event
            await self.invite(room, event)

    async def _reaction(
        self, room: MatrixRoom, event: UnknownEvent, reacted_to_id: str
    ) -> None:
        """A reaction was sent to one of our messages. Let's send a reply acknowledging it.

        Args:
            room: The room the reaction was sent in.

            event: The reaction event.

            reacted_to_id: The event ID that the reaction points to.
        """
        logger.debug(f"Got reaction to {room.room_id} from {event.sender}.")

        # Get the original event that was reacted to
        event_response = await self.client.room_get_event(room.room_id, reacted_to_id)
        if isinstance(event_response, RoomGetEventError):
            logger.warning(
                "Error getting event that was reacted to (%s)", reacted_to_id
            )
            return
        reacted_to_event = event_response.event

        # Only acknowledge reactions to events that we sent
        if reacted_to_event.sender != self.config.user_id:
            return

        # Send a message acknowledging the reaction
        reaction_sender_pill = make_pill(event.sender)
        reaction_content = (
            event.source.get("content", {}).get("m.relates_to", {}).get("key")
        )
        message = (
            f"{reaction_sender_pill} reacted to this event with `{reaction_content}`!"
        )
        await send_text_to_room(
            self.client,
            room.room_id,
            message,
            reply_to_event_id=reacted_to_id,
        )

    async def decryption_failure(self, room: MatrixRoom, event: MegolmEvent) -> None:
        """Callback for when an event fails to decrypt. Inform the user.

        Args:
            room: The room that the event that we were unable to decrypt is in.

            event: The encrypted event that we were unable to decrypt.
        """
        logger.error(
            f"Failed to decrypt event '{event.event_id}' in room '{room.room_id}'!"
            f"\n\n"
            f"Tip: try using a different device ID in your config file and restart."
            f"\n\n"
            f"If all else fails, delete your store directory and let the bot recreate "
            f"it (your reminders will NOT be deleted, but the bot may respond to existing "
            f"commands a second time)."
        )

        # red_x_and_lock_emoji = "❌ 🔐"

        # React to the undecryptable event with some emoji
        # await react_to_event(
        #    self.client,
        #    room.room_id,
        #    event.event_id,
        #    red_x_and_lock_emoji,
        # )

    async def reaction(self, room: MatrixRoom, event: ReactionEvent):
        """Callback when a reaction event is received.

        Args:
            room (MatrixRoom): The room the reaction was sent in.
            event (ReactionEvent): The event itself.
        """

        # Ignore own events
        if event.sender == self.client.user_id:
            return

        if room.room_id == self.config.vetting_room_id:
            if event.key == "confirm":
                # Check which user the reaction is for (if any)
                self.store.cursor.execute(
                    "SELECT mxid FROM vetting WHERE decision_event_id = ?",
                    (event.reacts_to,),
                )
                row = self.store.cursor.fetchone()
                if row is None:
                    return

                # Invite the user
                logger.info("Inviting new user (%s) to the main space.", row[0])
                await self.client.room_invite(self.config.main_space_id, row[0])

    async def unknown(self, room: MatrixRoom, event: UnknownEvent) -> None:
        """Callback for when an event with a type that is unknown to matrix-nio is received.

        Args:
            room: The room the reaction was sent in.

            event: The event itself.
        """
        logger.debug(
            f"Got unknown event with type to {event.type} from {event.sender} in {room.room_id}."
        )
