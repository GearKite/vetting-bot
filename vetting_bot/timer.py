import asyncio
import logging
import time

from nio import (
    AsyncClient,
    RoomMessagesError,
    RoomSendError,
    RoomSendResponse,
    UnknownEvent,
)

from vetting_bot.chat_functions import react_to_event, send_text_to_room
from vetting_bot.config import Config
from vetting_bot.storage import Storage

logger = logging.getLogger(__name__)


class Timer:
    def __init__(
        self,
        client: AsyncClient,
        store: Storage,
        config: Config,
    ):
        self.client = client
        self.store = store
        self.config = config

    async def start_all_timers(self):
        self.store.cursor.execute(
            """
            SELECT mxid, poll_event_id, voting_start_time, vote_ended
            FROM vetting
            WHERE voting_start_time IS NOT NULL
            """
        )

        rows = self.store.cursor.fetchall()
        for row in rows:
            if row[3]:
                continue

            await self.wait_for_poll_end(
                mxid=row[0], poll_event_id=row[1], start_time=row[2]
            )

    async def wait_for_poll_end(self, mxid: str, poll_event_id: str, start_time: int):
        async def _task():
            time_left = start_time + self.config.voting_time - time.time()
            await asyncio.sleep(time_left)
            await self._end_poll(mxid, poll_event_id)

        asyncio.create_task(_task())

    async def _end_poll(self, mxid: str, poll_event_id: str):
        logger.info("Ending poll for %s - %s", mxid, poll_event_id)
        # Send poll end event
        event_content = {
            "m.relates_to": {
                "rel_type": "m.reference",
                "event_id": poll_event_id,
            }
        }

        poll_resp = await self.client.room_send(
            self.config.vetting_room_id,
            message_type="org.matrix.msc3381.poll.end",
            content=event_content,
        )

        if isinstance(poll_resp, RoomSendError):
            logger.error(poll_resp, stack_info=True)
            return

        # Gather votes
        message_filter = {
            # "types": ["org.matrix.msc3381.poll.response"], # this doesn't work for some reason :/
            "rooms": [self.config.vetting_room_id],
        }

        vote_count = {
            "yes": 0,
            "no": 0,
            "blank": 0,
        }

        users_voted = set()

        # Loop until we find all events that could be related to the poll
        # (max 20 times: 20 * 20 = up to 400 events deep or until we find the poll event)
        start_token = ""
        for _ in range(0, 20):
            logger.debug("Requesting events")
            message_resp = await self.client.room_messages(
                room_id=self.config.vetting_room_id,
                start=start_token,
                limit=20,
                message_filter=message_filter,
            )

            if isinstance(message_resp, RoomMessagesError):
                logging.error(message_resp, stack_info=True)
                text = "Unable to gather votes."
                await send_text_to_room(self.client, self.config.vetting_room_id, text)
                return

            # Resume next request where this ends
            start_token = message_resp.end

            # Count votes
            for event in message_resp.chunk:
                # Only process poll response events
                if not isinstance(event, UnknownEvent):
                    continue
                if event.type != "org.matrix.msc3381.poll.response":
                    continue
                content = event.source.get("content")
                try:
                    # Check if this response is for the correct poll
                    related_event_id = content["m.relates_to"]["event_id"]
                    if related_event_id != poll_event_id:
                        continue

                    # Add vote to count
                    answer = content["org.matrix.msc3381.poll.response"]["answers"][0]

                    # Only count the last poll response event
                    if event.sender in users_voted:
                        continue
                    users_voted.add(event.sender)
                    vote_count[answer] += 1
                except KeyError:
                    pass

            # Check if we found the initial poll event
            if any([event.event_id == poll_event_id for event in message_resp.chunk]):
                break

        votes_responses = "".join(
            [f"\n{answer.title()}: {count};" for answer, count in vote_count.items()]
        )

        # Make the decision by checking requirements
        decision = (
            vote_count["yes"] >= self.config.min_yes_votes
            and vote_count["no"] <= self.config.max_no_votes
        )

        decision_text = (
            "Confirm inviting this person to the Federation by reacting."
            if decision
            else "Votes do not match the requirements, not inviting."
        )

        text = (
            f"Voting for `{mxid}` has ended. Counted votes are:\n"
            f"{votes_responses}\n\n{decision_text}"
        )
        decision_resp = await send_text_to_room(
            self.client, self.config.vetting_room_id, text
        )

        if not isinstance(decision_resp, RoomSendResponse):
            logger.error(decision_resp)
            return

        if decision:
            await react_to_event(
                self.client,
                self.config.vetting_room_id,
                decision_resp.event_id,
                "confirm",
            )

        # Finally - update database
        self.store.cursor.execute(
            "UPDATE vetting SET vote_ended = 1, decision_event_id = ? WHERE mxid = ?",
            (
                decision_resp.event_id,
                mxid,
            ),
        )
