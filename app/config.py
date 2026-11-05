import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CONFIG_PATH = ROOT / "config.local.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH = ROOT / "config.yaml"
with open(CONFIG_PATH, encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

JOBS_DIR = ROOT / CFG["paths"]["jobs_dir"]
MODELS_DIR = ROOT / CFG["paths"]["models_dir"]
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
MODELS_DIR.mkdir(parents=True, exist_ok=True)

# 所有缓存/临时文件都放在工作区，不写 C 盘
CACHE_DIR = ROOT / ".cache"
TMP_DIR = CACHE_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
for k, v in {
    "MODELSCOPE_CACHE": MODELS_DIR / "modelscope",
    "HF_HOME": MODELS_DIR / "huggingface",
    "TORCH_HOME": MODELS_DIR / "torch",
    "MPLCONFIGDIR": CACHE_DIR / "matplotlib",
    "NUMBA_CACHE_DIR": CACHE_DIR / "numba",
    "XDG_CACHE_HOME": CACHE_DIR,
    "UV_CACHE_DIR": CACHE_DIR / "uv",
    "TEMP": TMP_DIR, "TMP": TMP_DIR, "TMPDIR": TMP_DIR,
}.items():
    os.environ[k] = str(v)

import tempfile  # noqa: E402

tempfile.tempdir = str(TMP_DIR)


def free_gpu():
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass
