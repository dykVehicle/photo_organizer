import json, os
from collections import Counter

cache_path = r'H:\.photo_organizer\hash_cache.json'
with open(cache_path, 'r', encoding='utf-8') as f:
    data = json.load(f)
entries = data.get('entries', {})
backup_entries = {k: v for k, v in entries.items() if '相册备份_20260301' in k}
print(f'hash_cache 中 相册备份_20260301 的条目: {len(backup_entries)}')

if backup_entries:
    subdirs = Counter()
    total_size = 0
    for fp, entry in backup_entries.items():
        rel = fp.replace('H:\\相册备份_20260301\\', '')
        top = rel.split('\\')[0] if '\\' in rel else rel.split('/')[0]
        subdirs[top] += 1
        total_size += entry.get('size', 0)

    print(f'总大小: {total_size / 1024 / 1024 / 1024:.1f} GB')
    print()
    print('目录结构:')
    for d, cnt in subdirs.most_common(30):
        print(f'  {d}: {cnt} 个文件')
    print()
    items = list(backup_entries.items())[:5]
    for fp, entry in items:
        print(f'  {fp}')
        h = entry.get('hash', '?')
        s = entry.get('size', 0)
        print(f'    hash={h}, size={s}')
