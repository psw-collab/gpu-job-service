def check_gpu_status(is_available: bool):
    if not is_available:
        # Raising a RuntimeError explicitly
        raise RuntimeError("CRITICAL: Failed to connect to GCP GPU Cluster. Drivers missing.")
    print("GPU is ready for job submission.")

# --- Testing the exception ---
if __name__ == "__main__":
    # 1. This will raise the exception directly and crash the script
    check_gpu_status(is_available=False)
    
    # 2. Alternatively, here is how you catch it if you don't want it to crash:
    # try:
    #     check_gpu_status(is_available=False)
    # except RuntimeError as e:
    #     print(f"Caught an expected error: {e}")