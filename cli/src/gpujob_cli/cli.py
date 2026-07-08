"""
gpujob CLI -- submit, track, list, cancel, and stream logs for GPU jobs
against the gpu-job-service API.

Usage:
    gpujob submit -f job.yaml
    gpujob status job-a1b2c3d4
    gpujob list [--status RUNNING] [--output table|json]
    gpujob cancel job-a1b2c3d4
    gpujob logs job-a1b2c3d4 [--follow]
"""

import json
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import api_client
from .config import get_api_url
from .job_config import JobConfigError, load_job_config

app = typer.Typer(
    name="gpujob",
    help="Submit and track GPU jobs against the gpu-job-service API.",
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)

# Shared across status / list / cancel / logs so colours stay consistent.
STATUS_COLORS = {
    "PENDING": "yellow",
    "SCHEDULED": "yellow",
    "RUNNING": "cyan",
    "SUCCEEDED": "green",
    "FAILED": "red",
    "CANCELLED": "magenta",
}

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}

# Seconds between polls in `logs --follow`. Module-level so tests can zero it.
LOGS_POLL_INTERVAL = 2.0


@app.command()
def submit(
    file: Path = typer.Option(
        ..., "-f", "--file", help="Path to the job config YAML file (e.g. job.yaml)."
    ),
):
    """Submit a job described by a YAML config file."""
    try:
        job_config = load_job_config(file)
    except JobConfigError as e:
        err_console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    payload = job_config.to_request_payload()
    base_url = get_api_url()

    try:
        result = api_client.submit_job(base_url, payload)
    except api_client.ApiError as e:
        err_console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    job_id = result.get("job_id", "<unknown>")
    console.print(f"Job submitted: [bold green]{job_id}[/bold green]")
    console.print(f"Track it with: gpujob status {job_id}")


@app.command()
def status(
    job_id: str = typer.Argument(..., help="The job ID returned by 'gpujob submit'."),
):
    """Look up and display the status of a previously submitted job."""
    base_url = get_api_url()
    try:
        result = api_client.get_job_status(base_url, job_id)
    except api_client.ApiError as e:
        err_console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)
    _print_status(result)


@app.command(name="list")
def list_command(
    status: Optional[str] = typer.Option(
        None, "--status", help="Only show jobs in this status (e.g. RUNNING)."
    ),
    output: str = typer.Option(
        "table", "--output", "-o", help="Output format: 'table' or 'json'."
    ),
):
    """List submitted jobs and their current status."""
    if output not in ("table", "json"):
        err_console.print(
            f"[bold red]Error:[/bold red] unknown --output '{output}'. "
            f"Use 'table' or 'json'."
        )
        raise typer.Exit(code=2)

    base_url = get_api_url()
    try:
        result = api_client.list_jobs(base_url, status=status)
    except api_client.ApiError as e:
        err_console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    jobs = result.get("jobs", [])

    if output == "json":
        typer.echo(json.dumps(result, indent=2, default=str))
        return

    if not jobs:
        console.print("No jobs found.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("JOB ID")
    table.add_column("STATUS")
    table.add_column("GPU")
    table.add_column("SUBMITTED")
    for job in jobs:
        status_value = job.get("status", "UNKNOWN")
        color = STATUS_COLORS.get(status_value, "white")
        gpu = f"{job.get('gpu_type', '-')} x{job.get('gpu_count', '-')}"
        table.add_row(
            job.get("id", "-"),
            f"[{color}]{status_value}[/{color}]",
            gpu,
            str(job.get("submitted_at", "-")),
        )
    console.print(table)


@app.command()
def cancel(
    job_id: str = typer.Argument(..., help="The job ID to cancel."),
):
    """Cancel a submitted job that hasn't finished yet."""
    base_url = get_api_url()
    try:
        result = api_client.cancel_job(base_url, job_id)
    except api_client.ApiError as e:
        err_console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    new_status = result.get("status", "CANCELLED")
    color = STATUS_COLORS.get(new_status, "magenta")
    console.print(
        f"Job [bold]{result.get('id', job_id)}[/bold] is now "
        f"[{color}]{new_status}[/{color}]."
    )


@app.command()
def logs(
    job_id: str = typer.Argument(..., help="The job ID to fetch logs for."),
    follow: bool = typer.Option(
        False, "--follow", "-f",
        help="Stream new log output until the job finishes.",
    ),
):
    """
    Print a job's logs.

    Without --follow, prints whatever logs exist right now and exits.
    With --follow, polls for new output until the job reaches a terminal
    state (SUCCEEDED / FAILED / CANCELLED).
    """
    base_url = get_api_url()
    since = 0
    try:
        while True:
            resp = api_client.get_job_logs(base_url, job_id, since=since)
            chunk = resp.get("logs", "")
            if chunk:
                typer.echo(chunk, nl=False)
            since = resp.get("next_since", since)
            job_status = resp.get("status", "")

            if not follow or job_status in TERMINAL_STATUSES:
                if job_status in TERMINAL_STATUSES:
                    _print_logs_footer(resp)
                break
            time.sleep(LOGS_POLL_INTERVAL)
    except api_client.ApiError as e:
        err_console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)
    except KeyboardInterrupt:
        err_console.print("\n[dim]Stopped following logs.[/dim]")
        raise typer.Exit(code=0)


def _print_logs_footer(resp: dict) -> None:
    """After a job is terminal, print a one-line status footer."""
    status_value = resp.get("status", "UNKNOWN")
    color = STATUS_COLORS.get(status_value, "white")
    err_console.print(f"\n[bold {color}]-- job {status_value} --[/bold {color}]")
    failure_reason = resp.get("failure_reason")
    if failure_reason:
        err_console.print(f"[bold red]Failure reason:[/bold red] {failure_reason}")


def _print_status(result: dict) -> None:
    """Pretty-print a JobStatusResponse dict."""
    status_value = result.get("status", "UNKNOWN")
    color = STATUS_COLORS.get(status_value, "white")

    console.print(f"Job ID:       {result.get('id', '-')}")
    console.print(f"Status:       [bold {color}]{status_value}[/bold {color}]")

    status_message = result.get("status_message")
    if status_message:
        console.print(f"Message:      {status_message}")

    console.print(f"Entrypoint:   {result.get('entrypoint', '-')}")
    console.print(f"Python:       {result.get('python_version', '-')}")
    console.print(f"GPU:          {result.get('gpu_type', '-')} x{result.get('gpu_count', '-')}")
    console.print(f"Submitted at: {result.get('submitted_at', '-')}")
    console.print(f"Started at:   {result.get('started_at') or '-'}")
    console.print(f"Completed at: {result.get('completed_at') or '-'}")

    failure_reason = result.get("failure_reason")
    if failure_reason:
        console.print(f"[bold red]Failure reason:[/bold red] {failure_reason}")


if __name__ == "__main__":
    app()
