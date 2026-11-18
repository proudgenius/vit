"""Allow `python -m vit ...`, which MCP clients can launch without PATH set up."""

from .cli import main

if __name__ == "__main__":
    main()
