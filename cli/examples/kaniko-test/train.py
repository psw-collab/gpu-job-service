import os
from utils import greet

# 1. Execute our internal module check
greet()

# 2. Make sure the directory path exists
os.makedirs("/outputs", exist_ok=True)

# 3. Write an artifact to the designated outputs volume
with open("/outputs/result.txt", "w") as f:
    f.write("Success! Hello from the GKE cluster GPU job.\n")

print("train.py finished executing successfully.")