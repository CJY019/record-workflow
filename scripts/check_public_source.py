"""Check the Git index for common credentials and private runtime artifacts."""

import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_DIRS = {
    ".venv", "venv", ".python", ".cache", "data", "models", "samples", "out",
    "__pycache__", ".work", "原始录音", "课件资料", "转录稿", "总结", "问答记录",
}
PRIVATE_SUFFIXES = {
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".pdf", ".ppt", ".pptx",
    ".doc", ".docx", ".xls", ".xlsx", ".pem", ".key", ".p12", ".pfx",
    ".zip", ".7z", ".safetensors", ".pt", ".pth", ".pyc", ".log",
}
SECRET_PATTERNS = {
    "API credential": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{20,})\b"),
    "GitHub credential": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "Slack credential": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    "credential in URL": re.compile(r"https?://[^\s/@]+:[^\s/@]+@"),
    "personal Windows path": re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\s\\/]+", re.I),
}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main() -> int:
    names = git("ls-files", "-z").decode("utf-8").split("\0")
    paths = [PurePosixPath(name) for name in names if name]
    findings = []
    for path in paths:
        name = path.name.lower()
        if (
            any(part.lower() in PRIVATE_DIRS for part in path.parts)
            or (name.startswith(".env") and name != ".env.example")
            or name.startswith("config.local")
            or path.suffix.lower() in PRIVATE_SUFFIXES
            or name == "笔记.md"
        ):
            findings.append(f"{path}: private or generated file")
            continue
        content = git("show", f":{path.as_posix()}")
        if len(content) > 2 * 1024 * 1024:
            findings.append(f"{path}: exceeds 2 MiB")
            continue
        if path.suffix.lower() in {".png", ".ico", ".woff2", ".woff", ".ttf"}:
            continue
        try:
            lines = content.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            findings.append(f"{path}: unexpected binary file")
            continue
        for number, line in enumerate(lines, 1):
            for label, pattern in SECRET_PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{path}:{number}: {label}")
    if findings:
        print("Public source check failed. Review these locations before publishing.")
        print("\n".join(findings))
        return 1
    if not paths:
        print("Public source check failed. No files in the Git index.")
        return 1
    print(f"Public source check passed for {len(paths)} indexed files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
