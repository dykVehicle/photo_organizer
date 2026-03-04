# -*- coding: utf-8 -*-
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

root = r"H:\All_相册_20260225_V2"
nsfw = os.path.join(root, "All_6_NSFW")

print("root:", os.path.isdir(root))
print("nsfw:", os.path.isdir(nsfw))

if os.path.isdir(nsfw):
    cnt = sum(len(f) for _,_,f in os.walk(nsfw))
    print("nsfw files:", cnt)
else:
    print("No All_6_NSFW dir")

# Just list top-level dirs
for d in os.listdir(root):
    fp = os.path.join(root, d)
    if os.path.isdir(fp):
        print("  dir:", d)
    else:
        print("  file:", d)
