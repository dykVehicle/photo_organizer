# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

with open(r"C:\Users\dyk\.cursor\projects\d-Cursor\terminals\98900.txt", 
          "r", encoding="utf-8", errors="replace") as f:
    lines = f.readlines()

print("Total lines:", len(lines))
for line in lines[-20:]:
    print(line.rstrip()[:200])
