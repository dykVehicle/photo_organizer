# -*- coding: utf-8 -*-
import os, sys, subprocess
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

d = r"H:\All_相册_20260225_V2\All_6_NSFW"
if not os.path.exists(d):
    print("Already gone")
    sys.exit(0)

# Use subprocess with cmd /c rd to bypass Python rmtree issues
result = subprocess.run(
    ["cmd", "/c", "rd", "/s", "/q", d],
    capture_output=True, text=True, encoding="utf-8", errors="replace"
)
print("stdout:", result.stdout)
print("stderr:", result.stderr)
print("returncode:", result.returncode)

if os.path.exists(d):
    remaining = sum(len(fs) for _, _, fs in os.walk(d))
    print("Still exists, remaining files:", remaining)
    # Retry: delete files first, then dirs
    for root, dirs, files in os.walk(d, topdown=False):
        for f in files:
            try:
                os.remove(os.path.join(root, f))
            except Exception:
                pass
        try:
            os.rmdir(root)
        except Exception:
            pass
    if os.path.exists(d):
        print("Still exists after retry")
    else:
        print("Deleted after retry")
else:
    print("Deleted successfully")
