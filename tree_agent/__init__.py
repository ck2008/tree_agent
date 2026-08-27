"""Tree Agent — tree-structured project manager for the Codex CLI."""

__version__ = "0.1.0"

# `main` used to be imported eagerly, which dragged tkinter into every consumer
# of this package. The server (`tree_agent.server`) runs headless, so the GUI is
# now resolved on first access instead.
def __getattr__(name: str):
    if name == "main":
        from .app import main

        return main
    raise AttributeError(name)
