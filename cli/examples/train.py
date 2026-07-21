import torch

if __name__ == "__main__":
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available inside this job's container.")

    count = torch.cuda.device_count()
    print(f"CUDA available. {count} GPU(s) visible.")
    for i in range(count):
        print(f"  GPU {i}: {torch.cuda.get_device_name(i)}")

    x = torch.rand(1024, 1024, device="cuda")
    y = torch.rand(1024, 1024, device="cuda")
    z = x @ y
    print(f"Matmul on GPU succeeded, result sum: {z.sum().item()}")
