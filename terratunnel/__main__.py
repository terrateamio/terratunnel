#!/usr/bin/env python3
"""
Entry point for running terratunnel as a module.

This allows the package to be executed with:
    python -m terratunnel
"""
from terratunnel.cli import cli

if __name__ == "__main__":
    cli()