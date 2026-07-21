import torch

if __name__ == "__main__":
    if torch.cuda.is_available():
        count = torch.cuda.device_count()
        print(f"CUDA available. {count} GPU(s) visible.")
        for i in range(count):
            print(f"  GPU {i}: {torch.cuda.get_device_name(i)}")
        x = torch.rand(1024, 1024, device="cuda")
        y = torch.rand(1024, 1024, device="cuda")
        z = x @ y
        result = f"matmul sum: {z.sum().item()}"
        print(f"Matmul on GPU succeeded, result sum: {z.sum().item()}")
    else:
        print("CUDA not available on this node -- running a CPU fallback for now.")
        x = torch.rand(1024, 1024)
        y = torch.rand(1024, 1024)
        z = x @ y
        result = f"matmul sum (CPU fallback): {z.sum().item()}"
        print(result)

    with open("/outputs/result.txt", "w") as f:
        f.write(result + "\n")
