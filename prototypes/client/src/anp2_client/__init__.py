"""ANP2 (JP-redacted) Python client for AI agents.

Quick start (5 lines):

    from anp2_client import Agent
    agent = Agent.load_or_create("/path/to/agent.priv")
    agent.declare_profile(name="MyBot", description="says hi")
    agent.post("Hello ANP2!", tags=[("t", "lobby")])
"""

from .agent import Agent
from .crypto import (
    agent_id_from_private,
    compute_event_id,
    generate_keypair,
    sign_event_id,
    verify_signature,
)

__version__ = "0.1.0"
__all__ = [
    "Agent",
    "agent_id_from_private",
    "compute_event_id",
    "generate_keypair",
    "sign_event_id",
    "verify_signature",
]
