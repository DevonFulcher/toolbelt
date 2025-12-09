import asyncio
import os

import typer
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.state import StateBackend
from deepagents.backends.store import StoreBackend
from deepagents import create_deep_agent
from langchain.chat_models import init_chat_model
from langgraph.store.memory import InMemoryStore
from langgraph.checkpoint.memory import MemorySaver

agent_typer = typer.Typer(help="Deep agent commands")


@agent_typer.command(name="run", help="Run deep agent")
def run():
    """Run an interactive deep agent session"""
    asyncio.run(_run_agent())


async def _run_agent():
    """Run the deep agent interactively"""
    checkpointer = MemorySaver()

    # Get the workspace directory from environment variable
    workspace_dir = os.getenv("GIT_PROJECTS_WORKDIR")
    if not workspace_dir:
        raise ValueError("GIT_PROJECTS_WORKDIR environment variable is not set")

    # Create filesystem backend for workspace access
    workspace_backend = FilesystemBackend(
        root_dir=workspace_dir,
        virtual_mode=True,  # Treat paths as virtual paths under root_dir
    )

    agent = create_deep_agent(
        model=init_chat_model(model="openai:gpt-4o"),
        store=InMemoryStore(),
        checkpointer=checkpointer,
        backend=lambda runtime: CompositeBackend(
            default=StateBackend(runtime=runtime),
            routes={
                "/memories/": StoreBackend(runtime=runtime),
                "/workspace/": workspace_backend,
            },
        ),
    )

    # Use a consistent thread_id to maintain conversation history
    thread_id = "main-conversation"
    config = {"configurable": {"thread_id": thread_id}}

    while True:
        user_input = input("User: ")
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": user_input}]},
            config=config,
        ):
            if "model" in chunk and "messages" in chunk["model"]:
                for message in chunk["model"]["messages"]:
                    message.pretty_print()
