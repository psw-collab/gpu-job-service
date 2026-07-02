"""
gpujob CLI -- submits jobs to and queries status from the gpu-job-service API.

Usage:
    gpujob submit -f job.yaml
    gpujob status job-a1b2c3d4
"""

import json
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

# Shared across `status` and `list` so colours stay consistent.
STATUS_COLORS = {
    "PENDING": "yellow",
    "SCHEDULED": "yellow",
    "RUNNING": "cyan",
    "SUCCEEDED": "green",
    "FAILED": "red",
    "CANCELLED": "magenta",
}


@app.command()
def submit(
    file: Path = typer.Option(
        ...,
        "-f",
        "--file",
        help="Path to the job config YAML file (e.g. job.yaml).",
    ),
):
    """
    Submit a job described by a YAML config file.

    The YAML file should look like:

        entrypoint: train.py
        requirements: requirements.txt
        python_version: "3.11"
        gpu_type: A100
        gpu_count: 2
    """
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
        # Clean, unstyled JSON for scripts/piping.
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
