from setuptools import setup, find_packages

setup(
    name="vit",
    version="0.1.0",
    description="Git for Video Editing — version control timeline metadata, not media files",
    packages=find_packages(),
    python_requires=">=3.8",
    # The CLI, snapshots and the MCP server need nothing beyond the standard
    # library. The extras are for the Gemini merge helper and the Qt panel.
    install_requires=[],
    extras_require={
        "ai": ["google-generativeai"],
        "qt": ["PySide6"],
        "all": ["google-generativeai", "PySide6"],
    },
    entry_points={
        "console_scripts": [
            "vit=vit.cli:main",
        ],
    },
)
