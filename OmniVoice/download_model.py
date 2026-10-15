import os
import sys
import time
from huggingface_hub import hf_hub_download

REPO_ID = "k2-fsa/OmniVoice"
FILES = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "chat_template.jinja",
    "audio_tokenizer/config.json",
    "audio_tokenizer/preprocessor_config.json",
    "audio_tokenizer/model.safetensors",
    "model.safetensors",
]

print(f"=== Baixando OmniVoice ({REPO_ID}) ===", flush=True)
for filename in FILES:
    print(f"\n[+] Baixando: {filename} ...", flush=True)
    t0 = time.time()
    path = hf_hub_download(repo_id=REPO_ID, filename=filename)
    size_mb = os.path.getsize(path) / (1024 * 1024)
    elapsed = time.time() - t0
    speed = size_mb / elapsed if elapsed > 0 else 0
    print(f"[OK] {filename} ({size_mb:.1f} MB) em {elapsed:.1f}s ({speed:.1f} MB/s)", flush=True)

print("\n=== Todos os arquivos foram baixados com sucesso! ===", flush=True)
