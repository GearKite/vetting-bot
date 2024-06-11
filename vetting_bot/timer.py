import asyncio
import logging
import time

from nio import AsyncClient, RoomSendError

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

        self.store.cursor.execute(
            "UPDATE vetting SET vote_ended = 1 WHERE mxid = ?",
            (mxid,),
        )
