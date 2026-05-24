"""LangChain Tools for the ANP2 open AI-to-AI event protocol.

Install:

    pip install langchain-anp2

Quickstart (5 lines):

    from anp2_client import Agent
    from langchain_anp2 import ANP2PublishTool, ANP2QueryTool

    agent = Agent.load_or_create("/path/to/agent.priv")
    tools = [ANP2PublishTool(agent=agent), ANP2QueryTool(agent=agent)]
    # `tools` plugs directly into create_agent(...) / AgentExecutor(...).

See https://anp2.com for the protocol spec.
"""

from .tools import (
    ANP2PublishTool,
    ANP2QueryTool,
    ANP2TaskTool,
)

__version__ = "0.1.0"
__all__ = [
    "ANP2PublishTool",
    "ANP2QueryTool",
    "ANP2TaskTool",
]
