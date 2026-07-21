"""
Client-side validation constants.

These lists exist to catch obvious typos before making a network call.
The server is the source of truth and re-validates everything regardless,
so keeping these in sync with the server isn't safety-critical -- but it
should still be updated if the server's supported values change.
"""

# Matches the schema's gpu_count constraint: 1 <= gpu_count <= 8 (single-node, v1 scope)
MIN_GPU_COUNT = 1
MAX_GPU_COUNT = 8

ALLOWED_GPU_TYPES = {
    "A100",
    "H100",
}

ALLOWED_PYTHON_VERSIONS = {
    "3.11",
    "3.12",
    "3.13",
}
