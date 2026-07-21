"""
gpujob CLI -- submits jobs to and queries status from the gpu-job-service API.

Usage:
    gpujob submit -f job.yaml
    gpujob status job-a1b2c3d4
    gpujob logs job-a1b2c3d4
    gpujob logs -f job-a1b2c3d4
    gpujob outputs job-a1b2c3d4
    gpujob outputs job-a1b2c3d4 --download --dest ./results
"""

from pathlib import Path
from typing import Optional
import time

import typer
from rich.console import Console
from rich.table import Table

from . import api_client
from .config import get_api_key, get_api_url, get_identity_token
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
        result = api_client.submit_job(base_url, payload, api_key=get_api_key(),
                                        identity_token=get_identity_token())
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
        result = api_client.get_job_status(base_url, job_id, api_key=get_api_key(),
                                            identity_token=get_identity_token())
    except api_client.ApiError as e:
        err_console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    _print_status(result)


_TERMINAL_STATES = {"SUCCEEDED", "FAILED"}


@app.command()
def logs(
    job_id: str = typer.Argument(..., help="The job ID returned by 'gpujob submit'."),
    follow: bool = typer.Option(
        False,
        "-f",
        "--follow",
        help="Stream new log output as the job runs, until it finishes (like 'tail -f').",
    ),
    interval: float = typer.Option(
        2.0,
        "--interval",
        help="Seconds between polls when following (default: 2).",
        min=0.5,
    ),
):
    """
    Print the logs for a job, by ID.

    Without --follow, prints whatever logs are available right now and exits.
    With --follow, polls until the job reaches a terminal state, printing only
    newly-appended output each time.
    """
    base_url = get_api_url()

    if not follow:
        try:
            log_text = api_client.get_job_logs(base_url, job_id, api_key=get_api_key(),
                                                identity_token=get_identity_token())
        except api_client.ApiError as e:
            err_console.print(f"[bold red]Error:[/bold red] {e}")
            raise typer.Exit(code=1)
        console.print(log_text, markup=False, highlight=False)
        return

    _follow_logs(base_url, job_id, interval)


def _follow_logs(base_url: str, job_id: str, interval: float) -> None:
    """
    Poll the status and logs endpoints until the job is terminal, printing only
    the newly-appended tail each iteration.

    The gateway returns 409 while the job hasn't produced logs yet and 404 for a
    terminal job that captured none; both are treated as "nothing new yet" here.
    Because the server may only expose the full log at completion today, this
    still works end to end -- it just prints everything in one go when the job
    finishes. Once the backend ships partial logs during RUNNING, the same code
    starts printing incremental output with no CLI change.
    """
    api_key = get_api_key()
    identity_token = get_identity_token()
    printed = 0
    waiting_notified = False

    while True:
        try:
            status_result = api_client.get_job_status(
                base_url, job_id, api_key=api_key, identity_token=identity_token
            )
        except api_client.ApiError as e:
            err_console.print(f"[bold red]Error:[/bold red] {e}")
            raise typer.Exit(code=1)

        status = status_result.get("status", "UNKNOWN")

        try:
            log_text = api_client.get_job_logs(
                base_url, job_id, api_key=api_key, identity_token=identity_token
            )
        except api_client.ApiError as e:
            if e.status_code in (404, 409):
                log_text = ""
            else:
                err_console.print(f"[bold red]Error:[/bold red] {e}")
                raise typer.Exit(code=1)

        if len(log_text) > printed:
            console.print(log_text[printed:], markup=False, highlight=False, end="")
            printed = len(log_text)

        if status in _TERMINAL_STATES:
            if printed == 0:
                console.print("[dim](no logs were captured for this job)[/dim]")
            else:
                console.print()  # ensure the shell prompt lands on a fresh line
            return

        if printed == 0 and not waiting_notified:
            err_console.print(f"[dim]Waiting for logs (job is {status})...[/dim]")
            waiting_notified = True

        time.sleep(interval)


@app.command()
def outputs(
    job_id: str = typer.Argument(..., help="The job ID returned by 'gpujob submit'."),
    download: bool = typer.Option(
        False,
        "-d",
        "--download",
        help="Download the output files instead of just listing them.",
    ),
    dest: Optional[Path] = typer.Option(
        None,
        "--dest",
        help="Directory to download into (with --download). Subdirectory "
             "structure is preserved. Defaults to ./<job-id>/.",
    ),
):
    """
    List (or download) the files a job wrote to its /outputs directory.

    Without --download, prints a table of the available files and their sizes.
    With --download, fetches each file into --dest (default ./<job-id>/),
    recreating any subdirectories (e.g. models/best.pt).
    """
    base_url = get_api_url()

    try:
        result = api_client.get_job_outputs(base_url, job_id, api_key=get_api_key(),
                                             identity_token=get_identity_token())
    except api_client.ApiError as e:
        if e.status_code == 409:
            console.print(
                "[yellow]Outputs aren't available yet -- this job is still running. "
                "Check back once it completes.[/yellow]"
            )
            raise typer.Exit(code=1)
        err_console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    files = result.get("outputs", [])
    status = result.get("status")
    if not files:
        note = f" (job {status})" if status else ""
        console.print(f"[dim]This job produced no output files{note}.[/dim]")
        return

    if not download:
        _print_outputs_table(files, status)
        return

    download_dir = dest if dest is not None else Path(job_id)
    _download_outputs(files, download_dir)


def _print_outputs_table(files: list, status: Optional[str] = None) -> None:
    """Render the output file listing as a table."""
    if status:
        console.print(f"Job status: [bold]{status}[/bold]")
    table = Table(show_edge=False, header_style="bold")
    table.add_column("File")
    table.add_column("Size", justify="right")
    for entry in files:
        table.add_row(entry.get("path", "-"), _human_size(entry.get("size_bytes")))
    console.print(table)
    console.print(f"\n{len(files)} file(s). Re-run with --download to fetch them.")


def _download_outputs(files: list, dest: Path) -> None:
    """Download every output file into ``dest``, preserving subdirectories."""
    written = 0
    for entry in files:
        rel = entry.get("path")
        url = entry.get("url")
        if not rel or not url:
            err_console.print(f"[yellow]Skipping malformed output entry:[/yellow] {entry}")
            continue

        target = _safe_join(dest, rel)
        if target is None:
            err_console.print(f"[yellow]Skipping output with suspicious path:[/yellow] {rel}")
            continue

        try:
            api_client.download_output(url, target)
        except api_client.ApiError as e:
            err_console.print(f"[bold red]Error downloading {rel}:[/bold red] {e}")
            raise typer.Exit(code=1)

        console.print(f"  {rel} -> {target}")
        written += 1

    console.print(f"Downloaded [bold green]{written}[/bold green] file(s) to {dest}")


def _safe_join(dest: Path, rel: str) -> Optional[Path]:
    """
    Join a server-provided relative path onto ``dest``, refusing anything that
    would escape ``dest`` (absolute paths, or '..' traversal). Returns the
    resolved target path, or None if the path is unsafe.
    """
    candidate = (dest / rel).resolve()
    dest_resolved = dest.resolve()
    try:
        candidate.relative_to(dest_resolved)
    except ValueError:
        return None
    return candidate


def _human_size(num_bytes: Optional[int]) -> str:
    """Format a byte count as a short human-readable string."""
    if num_bytes is None:
        return "-"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def _print_status(result: dict) -> None:
    """Pretty-print a JobStatusResponse dict."""
    status_value = result.get("status", "UNKNOWN")
    status_colors = {
        "PENDING": "yellow",
        "BUILDING": "yellow",
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
