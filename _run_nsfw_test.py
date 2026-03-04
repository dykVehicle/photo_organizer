"""直接调用 main 验证 NSFW 检测"""
import sys
sys.argv = [
    "main.py",
    "--nsfw",
    "--dry-run",
    "--copy-all",
    "--copy-unknown-photo",
    "--scan-dirs", r"H:\All_相册_20260225_V2",
    "--output-dir", r"H:\All_相册_NSFW_test",
]
from main import main
main()
