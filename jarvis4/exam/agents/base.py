from __future__ import annotations

from ..scenario import Event, Scenario
from ..world import Message, World


class Agent:
    """One exam candidate. deliver() returns at once; the work may go on in the background until busy() is False."""
    name = "base"

    async def start(self, world: World, scenario: Scenario) -> None:
        self.world, self.scenario = world, scenario

    async def deliver(self, index: int, event: Event, msg: Message | None) -> None:
        """msg: the world message the event became (None for events that are not messages)."""
        raise NotImplementedError

    def busy(self) -> bool:
        return False

    def cost(self) -> float:
        return 0.0

    async def stop(self) -> None:
        pass
