"""Local call FIFO observation; never installed on generic gateway events."""
import asyncio

class TurnReceipt:
    def __init__(self):
        self.future = asyncio.get_running_loop().create_future()
        self.started = False
        self.admitted = False
        self.cancelled = False
        self.status = 'pending'

    def finish(self, text=None, *, status=None):
        if self.future.done():
            return
        self.status = status or ('completed' if text else 'unconfirmed')
        self.future.set_result(text or 'No completed result confirmed.')
