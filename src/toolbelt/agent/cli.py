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
def run(
    root_dir: str = typer.Option(
        None,
        "--root-dir",
        "-r",
        help="Root directory for the workspace (defaults to GIT_PROJECTS_WORKDIR env var)",
    ),
):
    """Run an interactive deep agent session"""
    asyncio.run(_run_agent(root_dir=root_dir))


async def _run_agent(root_dir: str | None = None):
    """Run the deep agent interactively"""
    checkpointer = MemorySaver()

    # Get the workspace directory from CLI arg or environment variable
    working_directory = root_dir or os.getenv("GIT_PROJECTS_WORKDIR")
    if not working_directory:
        raise ValueError(
            "Workspace directory must be provided via --root-dir option or "
            "GIT_PROJECTS_WORKDIR environment variable"
        )

    # Validate the path
    if not os.path.exists(working_directory):
        raise ValueError(f"Path does not exist: {working_directory}")
    if not os.path.isdir(working_directory):
        raise ValueError(f"Path is not a directory: {working_directory}")
    if not os.access(working_directory, os.R_OK):
        raise ValueError(f"Path is not readable: {working_directory}")

    # Create filesystem backend for workspace access
    workspace_backend = FilesystemBackend(
        root_dir=working_directory,
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
