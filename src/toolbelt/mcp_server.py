import typer
from mcp.server import FastMCP

from toolbelt.git.cli import git_typer

mcp = FastMCP()
mcp_typer = typer.Typer(help="MCP server")

for command in git_typer.registered_commands:
    if command.callback:
        mcp.tool(name=command.name, description=command.help)(command.callback)


@mcp_typer.command()
def run():
    """Run the MCP server"""
    mcp.run()
