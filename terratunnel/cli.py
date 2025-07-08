import click
import logging
import os
from .server.app import run_server
from .client.app import run_client


@click.group()
@click.option("--log-level", default="INFO", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]), 
              envvar="TERRATUNNEL_LOG_LEVEL", help="Log level (can be set via TERRATUNNEL_LOG_LEVEL)")
def cli(log_level):
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    # Suppress httpx request logs to reduce noise
    logging.getLogger("httpx").setLevel(logging.WARNING)


@cli.command()
@click.option("--host", default="0.0.0.0", envvar="TERRATUNNEL_HOST", 
              help="Host to bind (default: 0.0.0.0, env: TERRATUNNEL_HOST)")
@click.option("--port", default=8000, type=int, envvar="TERRATUNNEL_PORT",
              help="Port to bind (default: 8000, env: TERRATUNNEL_PORT)")
@click.option("--domain", default="tunnel.terrateam.dev", envvar="TERRATUNNEL_DOMAIN",
              help="Domain name for tunnel hostnames (default: tunnel.terrateam.dev, env: TERRATUNNEL_DOMAIN)")
@click.option("--github-only", is_flag=True, envvar="TERRATUNNEL_GITHUB_ONLY",
              help="Only allow requests from GitHub webhook IPs (env: TERRATUNNEL_GITHUB_ONLY)")
@click.option("--validate-endpoint", is_flag=True, envvar="TERRATUNNEL_VALIDATE_ENDPOINT",
              help="Validate endpoint ownership via .well-known/terratunnel (env: TERRATUNNEL_VALIDATE_ENDPOINT)")
def server(host, port, domain, github_only, validate_endpoint):
    """Start a tunnel server."""
    run_server(host=host, port=port, domain=domain, github_only=github_only, validate_endpoint=validate_endpoint)


@cli.command()
@click.option("--server-url", default="ws://tunnel.terrateam.dev", envvar="TERRATUNNEL_SERVER_URL",
              help="Server URL to connect to (default: ws://tunnel.terrateam.dev, env: TERRATUNNEL_SERVER_URL)")
@click.option("--local-endpoint", envvar="TERRATUNNEL_LOCAL_ENDPOINT", required=True,
              help="Local endpoint to forward to (e.g. http://localhost:3000, env: TERRATUNNEL_LOCAL_ENDPOINT)")
@click.option("--dashboard", is_flag=True, envvar="TERRATUNNEL_DASHBOARD",
              help="Enable webhook dashboard (env: TERRATUNNEL_DASHBOARD)")
@click.option("--dashboard-port", default=8080, type=int, envvar="TERRATUNNEL_DASHBOARD_PORT",
              help="Port for webhook dashboard (default: 8080, env: TERRATUNNEL_DASHBOARD_PORT)")
@click.option("--api-port", default=8081, type=int, envvar="TERRATUNNEL_API_PORT",
              help="Port for JSON API (default: 8081, env: TERRATUNNEL_API_PORT)")
@click.option("--update-github-webhook", is_flag=True, envvar="TERRATUNNEL_UPDATE_GITHUB_WEBHOOK",
              help="Automatically update GitHub App webhook URL when tunnel connects (env: TERRATUNNEL_UPDATE_GITHUB_WEBHOOK)")
def client(server_url, local_endpoint, dashboard, dashboard_port, api_port, update_github_webhook):
    """Connect to a tunnel server and forward requests to a local endpoint."""
    run_client(server_url=server_url, local_endpoint=local_endpoint, dashboard=dashboard, dashboard_port=dashboard_port, api_port=api_port, update_github_webhook=update_github_webhook)


if __name__ == "__main__":
    cli()