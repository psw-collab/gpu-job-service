"""
gpujob CLI -- submits jobs to and queries status from the gpu-job-service API.

Usage:
    gpujob submit -f job.yaml
    gpujob status job-a1b2c3d4
"""

from pathlib import Path

import typer
from rich.console import Console

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


def _print_status(result: dict) -> None:
    """Pretty-print a JobStatusResponse dict."""
    status_value = result.get("status", "UNKNOWN")
    status_colors = {
        "PENDING": "yellow",
        "SCHEDULED": "yellow",
        "RUNNING": "cyan",
        "SUCCEEDED": "green",
        "FAILED": "red",
    }
    color = status_colors.get(status_value, "white")

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
