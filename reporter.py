"""报告生成模块：HTML + CSV 双格式溯源报告（支持视频+设备过滤统计）"""

import csv
import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Set

from jinja2 import Template

from organizer import CopyRecord, OrganizeResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 扫描目录树构建 & HTML 渲染
# ---------------------------------------------------------------------------

def _build_scan_tree(records: list, scan_dirs: Optional[List[str]]) -> list:
    """从源文件路径构建目录树结构 [{name, total_count, children}, ...]。"""
    dir_counts: Dict[str, int] = defaultdict(int)
    for r in records:
        folder = os.path.normpath(os.path.dirname(r.source))
        dir_counts[folder] += 1

    roots = [os.path.normpath(d) for d in (scan_dirs or [])]
    if not roots:
        drives: set = set()
        for folder in dir_counts:
            drives.add(folder.split(os.sep)[0] + os.sep)
        roots = sorted(drives)

    def _sum_subtree(node_dict: dict) -> int:
        total = 0
        for info in node_dict.values():
            total += info["own"] + _sum_subtree(info["children"])
        return total

    def _to_list(node_dict: dict) -> list:
        items = []
        for name in sorted(node_dict):
            info = node_dict[name]
            subtotal = info["own"] + _sum_subtree(info["children"])
            items.append({
                "name": name,
                "total_count": subtotal,
                "children": _to_list(info["children"]),
            })
        return items

    tree = []
    for root_path in roots:
        trie: dict = {}
        root_own = 0
        for folder, count in dir_counts.items():
            if not folder.lower().startswith(root_path.lower()):
                continue
            rel = os.path.relpath(folder, root_path)
            if rel == ".":
                root_own = count
                continue
            parts = rel.split(os.sep)
            node = trie
            for i, part in enumerate(parts):
                if part not in node:
                    node[part] = {"children": {}, "own": 0}
                if i == len(parts) - 1:
                    node[part]["own"] = count
                node = node[part]["children"]

        root_total = root_own + _sum_subtree(trie)
        tree.append({
            "name": root_path,
            "total_count": root_total,
            "children": _to_list(trie),
        })
    return tree


def _render_tree_html(nodes: list, depth: int = 0) -> str:
    """递归渲染目录树为嵌套 <details>/<ul> HTML。"""
    if not nodes:
        return ""
    html = '<ul class="dir-tree">'
    for node in nodes:
        html += "<li>"
        if node["children"]:
            open_attr = " open" if depth < 1 else ""
            html += (
                f'<details{open_attr}>'
                f'<summary>{node["name"]}/ '
                f'<span class="fc">({node["total_count"]})</span></summary>'
            )
            html += _render_tree_html(node["children"], depth + 1)
            html += "</details>"
        else:
            html += f'{node["name"]}/ <span class="fc">({node["total_count"]})</span>'
        html += "</li>"
    html += "</ul>"
    return html

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>照片/视频整理报告 - {{ timestamp }}</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:"Microsoft YaHei","Segoe UI",sans-serif;background:#f0f2f5;color:#333;padding:0}
  .top-nav{position:sticky;top:0;z-index:100;background:#1a1a2e;color:#fff;padding:10px 20px;display:flex;align-items:center;gap:18px;font-size:14px;box-shadow:0 2px 8px rgba(0,0,0,.2)}
  .top-nav .title{font-size:18px;font-weight:700;margin-right:auto}
  .top-nav a{color:#dfe6e9;text-decoration:none;padding:4px 10px;border-radius:6px;transition:.2s}
  .top-nav a:hover{background:rgba(255,255,255,.15);color:#fff}
  .container{max-width:1400px;margin:0 auto;padding:20px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:24px}
  .card{background:#fff;border-radius:10px;padding:14px;text-align:center;box-shadow:0 2px 6px rgba(0,0,0,.05)}
  .card .num{font-size:28px;font-weight:700}
  .card .label{font-size:11px;color:#888;margin-top:2px}
  .card.camera .num{color:#e17055}.card.phone .num{color:#0984e3}.card.unknown .num{color:#636e72}
  .card.dup .num{color:#fdcb6e}.card.err .num{color:#d63031}.card.total .num{color:#00b894}
  .card.skip .num{color:#a29bfe}.card.nodev .num{color:#b2bec3}
  .card.photo .num{color:#e17055}.card.video .num{color:#6c5ce7}
  section{background:#fff;border-radius:10px;padding:20px;margin-bottom:20px;box-shadow:0 2px 6px rgba(0,0,0,.05)}
  section h2{font-size:17px;color:#2d3436;margin-bottom:14px;border-left:4px solid #0984e3;padding-left:10px}
  table{width:100%;border-collapse:collapse;font-size:12px}
  th{background:#f8f9fa;padding:8px 6px;text-align:left;font-weight:600;color:#555;border-bottom:2px solid #dee2e6}
  td{padding:6px;border-bottom:1px solid #eee;word-break:break-all}
  tr:hover td{background:#f8f9fa}
  .status-ok{color:#00b894}.status-dup{color:#fdcb6e}.status-skip{color:#a29bfe}.status-nodev{color:#b2bec3}.status-err{color:#d63031}
  .badge{display:inline-block;padding:2px 6px;border-radius:8px;font-size:10px;color:#fff}
  .badge-camera{background:#e17055}.badge-phone{background:#0984e3}.badge-unknown{background:#636e72}
  .target-yes{font-weight:700;color:#00b894}.target-no{color:#b2bec3}
  .media-video{color:#6c5ce7;font-weight:600}.media-photo{color:#636e72}
  #detailTbl{table-layout:fixed}
  #detailTbl th,#detailTbl td{overflow:hidden;text-overflow:ellipsis}
  .detail-wrap{max-height:80vh;overflow:auto;border:1px solid #dee2e6;border-radius:8px}
  #detailTbl thead tr:first-child th{position:sticky;top:0;z-index:12;background:#f8f9fa;box-shadow:0 1px 0 #dee2e6}
  #detailTbl thead tr.col-filter td{position:sticky;top:30px;z-index:11;background:#fff;border-bottom:2px solid #dee2e6;padding:4px 3px}
  .col-filter input{width:100%;padding:3px 5px;border:1px solid #dee2e6;border-radius:4px;font-size:11px;box-sizing:border-box}
  .match-info{font-size:12px;color:#888;margin:8px 0}
</style>
</head>
<body>
<nav class="top-nav">
  <span class="title">照片/视频整理报告</span>
  <a href="#sec-summary">概览</a>
  <a href="#sec-folders">来源文件夹</a>
  <a href="#sec-devices">设备统计</a>
  <a href="#sec-dups">重复文件</a>
  <a href="#sec-detail">详细溯源</a>
</nav>
<div class="container">
  <p style="text-align:center;color:#666;font-size:13px;margin:12px 0">{{ timestamp }} | 目标设备: {{ target_devices_display }}</p>

  <div id="sec-summary" class="cards">
    <div class="card total"><div class="num">{{ total }}</div><div class="label">发现总数</div></div>
    <div class="card photo"><div class="num">{{ photo_count }}</div><div class="label">照片</div></div>
    <div class="card video"><div class="num">{{ video_count }}</div><div class="label">视频</div></div>
    <div class="card camera"><div class="num">{{ camera_count }}</div><div class="label">相机</div></div>
    <div class="card phone"><div class="num">{{ phone_count }}</div><div class="label">手机</div></div>
    <div class="card unknown"><div class="num">{{ unknown_count }}</div><div class="label">未识别</div></div>
    <div class="card total"><div class="num">{{ copied }}</div><div class="label">成功复制</div></div>
    <div class="card dup"><div class="num">{{ dup_count }}</div><div class="label">重复跳过</div></div>
    <div class="card skip"><div class="num">{{ not_target_count }}</div><div class="label">非目标跳过</div></div>
    <div class="card nodev"><div class="num">{{ no_device_count }}</div><div class="label">无设备信息</div></div>
    <div class="card dup"><div class="num">{{ filtered_count }}</div><div class="label">小图过滤</div></div>
    <div class="card err"><div class="num">{{ error_count }}</div><div class="label">错误</div></div>
  </div>

  <section id="sec-folders">
    <h2>来源文件夹汇总（按文件数排序）</h2>
    <table>
      <tr><th>#</th><th>来源文件夹</th><th>总数</th><th>相机</th><th>手机</th><th>未识别</th><th>已复制</th><th>重复</th><th>非目标</th><th>过滤</th></tr>
      {% for f in source_folders %}
      <tr><td>{{ loop.index }}</td><td>{{ f.path }}</td><td><b>{{ f.total }}</b></td>
        <td>{{ f.camera or "-" }}</td><td>{{ f.phone or "-" }}</td><td>{{ f.unknown or "-" }}</td>
        <td class="status-ok">{{ f.copied or "-" }}</td><td>{{ f.dup or "-" }}</td><td>{{ f.not_target or "-" }}</td><td>{{ f.filtered or "-" }}</td></tr>
      {% endfor %}
    </table>
  </section>

  <section id="sec-devices">
    <h2>设备统计</h2>
    <table>
      <tr><th>品牌</th><th>型号</th><th>类型</th><th>数量</th><th>照片</th><th>视频</th><th>目标</th></tr>
      {% for item in device_stats %}
      <tr><td>{{ item.make or "未知" }}</td><td>{{ item.model or "未知" }}</td>
        <td><span class="badge badge-{{ item.dtype }}">{{ item.dtype_label }}</span></td>
        <td>{{ item.count }}</td><td>{{ item.photos }}</td><td>{{ item.videos }}</td>
        <td class="{{ 'target-yes' if item.is_target else 'target-no' }}">{{ "✓" if item.is_target else "—" }}</td></tr>
      {% endfor %}
    </table>
  </section>

  {% if dup_records %}
  <section id="sec-dups">
    <h2>重复文件对比（{{ dup_records|length }} 组）</h2>
    <p style="color:#888;font-size:12px;margin-bottom:10px">以下文件因哈希相同被跳过，列出重复文件与保留的原始文件</p>
    <table>
      <tr><th>#</th><th>重复文件（已跳过）</th><th>原始文件（已保留）</th><th>哈希</th><th>大小</th></tr>
      {% for d in dup_records %}
      <tr><td>{{ loop.index }}</td><td>{{ d.source }}</td><td>{{ d.dup_of }}</td>
        <td style="font-family:monospace;font-size:11px">{{ d.file_hash }}</td><td>{{ d.size_display }}</td></tr>
      {% endfor %}
    </table>
  </section>
  {% endif %}

  <section id="sec-detail">
    <h2>详细溯源表（共 {{ records|length }} 条）</h2>
    <p class="match-info" id="matchInfo"></p>
    <div class="detail-wrap">
    <table id="detailTbl">
      <thead>
        <tr><th style="width:40px">#</th><th style="width:25%">原始路径</th><th style="width:20%">目标路径</th><th style="width:50px">类型</th><th style="width:40px">媒体</th><th style="width:12%">品牌/型号</th><th style="width:80px">日期</th><th style="width:55px">大小</th><th style="width:60px">状态</th><th>备注</th></tr>
        <tr class="col-filter">
          <td></td>
          <td><input data-col="1" list="dl1" placeholder="原始路径" oninput="doFilter()"></td>
          <td><input data-col="2" list="dl2" placeholder="目标路径" oninput="doFilter()"></td>
          <td><input data-col="3" list="dl3" placeholder="类型" oninput="doFilter()"></td>
          <td><input data-col="4" list="dl4" placeholder="媒体" oninput="doFilter()"></td>
          <td><input data-col="5" list="dl5" placeholder="品牌/型号" oninput="doFilter()"></td>
          <td><input data-col="6" list="dl6" placeholder="日期" oninput="doFilter()"></td>
          <td><input data-col="7" list="dl7" placeholder="大小" oninput="doFilter()"></td>
          <td><input data-col="8" list="dl8" placeholder="状态" oninput="doFilter()"></td>
          <td><input data-col="9" list="dl9" placeholder="备注" oninput="doFilter()"></td>
        </tr>
      </thead>
      <tbody id="dtBody"></tbody>
    </table>
    </div>
  </section>
</div>

<script>
var DATA={{ records_json }};
var COLS=[1,2,3,4,5,6,7,8,9];
var allRows=[];
(function buildRows(){
  var frag=document.createDocumentFragment();
  for(var i=0;i<DATA.length;i++){
    var r=DATA[i],tr=document.createElement('tr');
    tr._d=[
      '',
      r[1],
      r[2]||'-',
      r[3],
      r[4]==='video'?'视频':'照片',
      r[5],
      r[6]||'-',
      r[7],
      r[9],
      r[10]
    ];
    tr.innerHTML='<td>'+(i+1)+'</td><td>'+tr._d[1]+'</td><td>'+tr._d[2]+'</td>'
      +'<td><span class="badge badge-'+r[3]+'">'+r[3]+'</span></td>'
      +'<td class="'+(r[4]==='video'?'media-video':'media-photo')+'">'+tr._d[4]+'</td>'
      +'<td>'+tr._d[5]+'</td><td>'+tr._d[6]+'</td><td>'+tr._d[7]+'</td>'
      +'<td class="status-'+r[8]+'">'+tr._d[8]+'</td><td>'+tr._d[9]+'</td>';
    allRows.push(tr);
    frag.appendChild(tr);
  }
  document.getElementById('dtBody').appendChild(frag);
  document.getElementById('matchInfo').textContent='显示 '+DATA.length+' / '+DATA.length+' 条';
})();

(function buildDataLists(){
  var colSets={};
  for(var c=1;c<=9;c++)colSets[c]={};
  for(var i=0;i<allRows.length;i++){
    var d=allRows[i]._d;
    for(var c=1;c<=9;c++){
      var v=d[c];
      if(v&&v!=='-')colSets[c][v]=1;
    }
  }
  var skip={1:500,2:500};
  for(var c=1;c<=9;c++){
    var keys=Object.keys(colSets[c]);
    if(skip[c]&&keys.length>skip[c])continue;
    var dl=document.createElement('datalist');dl.id='dl'+c;
    keys.sort();
    for(var j=0;j<Math.min(keys.length,200);j++){
      var o=document.createElement('option');o.value=keys[j];dl.appendChild(o);
    }
    document.body.appendChild(dl);
  }
})();

var _timer=null;
function doFilter(){
  if(_timer)clearTimeout(_timer);
  _timer=setTimeout(_applyFilter,200);
}
function _applyFilter(){
  var inputs=document.querySelectorAll('.col-filter input');
  var filters=[];
  inputs.forEach(function(inp){
    var v=inp.value.trim().toLowerCase();
    if(v)filters.push({col:parseInt(inp.dataset.col),val:v});
  });
  var shown=0;
  for(var i=0;i<allRows.length;i++){
    var tr=allRows[i],vis=true;
    for(var j=0;j<filters.length;j++){
      var f=filters[j];
      if(tr._d[f.col].toLowerCase().indexOf(f.val)<0){vis=false;break;}
    }
    tr.style.display=vis?'':'none';
    if(vis)shown++;
  }
  document.getElementById('matchInfo').textContent='显示 '+shown+' / '+DATA.length+' 条';
}
</script>
</body>
</html>"""

STATUS_LABELS = {
    "ok": ("ok", "已复制"),
    "skipped_dup": ("dup", "重复跳过"),
    "skipped_exists": ("dup", "已存在"),
    "skipped_filtered": ("dup", "小图过滤"),
    "skipped_not_target": ("skip", "非目标设备"),
    "skipped_no_device": ("nodev", "无设备信息"),
    "dry_run": ("ok", "试运行"),
    "error": ("err", "错误"),
}

DTYPE_LABELS = {
    "camera": "相机",
    "phone": "手机",
    "unknown": "未识别",
}


def _human_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def _prepare_record(r: CopyRecord) -> dict:
    status_class, status_label = STATUS_LABELS.get(r.status, ("err", r.status))
    return {
        "source": r.source,
        "destination": r.destination,
        "device_type": r.device_type,
        "media_type": r.media_type,
        "dtype_label": DTYPE_LABELS.get(r.device_type, "未识别"),
        "make": r.make,
        "model": r.model,
        "date_taken": r.date_taken,
        "size_display": _human_size(r.file_size),
        "status_class": status_class,
        "status_label": status_label,
        "file_hash": r.file_hash,
        "dup_of": r.dup_of,
    }


def _record_to_js_array(r: CopyRecord) -> list:
    """将 CopyRecord 转为 JS 数组 [idx, source, dest, dtype, media, make_model, date, size, status_cls, status_lbl, note]"""
    status_class, status_label = STATUS_LABELS.get(r.status, ("err", r.status))
    make_model = r.make + (" / " + r.model if r.model else "")
    note = ""
    if r.dup_of:
        note = "重复: " + r.dup_of
    elif r.error_msg:
        note = r.error_msg
    return [
        0,  # placeholder for idx
        r.source,
        r.destination or "",
        r.device_type,
        r.media_type,
        make_model,
        r.date_taken or "",
        _human_size(r.file_size),
        status_class,
        status_label,
        note,
    ]


def _is_target_make(make: str, target_devices: Optional[Set[str]]) -> bool:
    if not target_devices or not make:
        return False
    make_lower = make.lower().strip()
    for kw in target_devices:
        if kw in make_lower:
            return True
    return False


def generate_report(
    result: OrganizeResult,
    report_dir: str,
    target_devices: Optional[Set[str]] = None,
    scan_dirs: Optional[List[str]] = None,
):
    """生成 HTML 和 CSV 报告。"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamp_display = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    os.makedirs(report_dir, exist_ok=True)
    html_path = os.path.join(report_dir, f"整理报告_{timestamp}.html")
    csv_path = os.path.join(report_dir, f"整理报告_{timestamp}.csv")

    # ── 统计数据 ──
    type_counter = Counter(r.device_type for r in result.records)
    status_counter = Counter(r.status for r in result.records)
    media_counter = Counter(r.media_type for r in result.records)

    # 设备统计（含照片/视频分类和目标设备标记）
    device_detail: dict = {}
    for r in result.records:
        key = (r.make, r.model, r.device_type)
        if key not in device_detail:
            device_detail[key] = {"count": 0, "photos": 0, "videos": 0}
        device_detail[key]["count"] += 1
        if r.media_type == "video":
            device_detail[key]["videos"] += 1
        else:
            device_detail[key]["photos"] += 1

    device_stats = []
    for (make, model, dtype), d in sorted(device_detail.items(), key=lambda x: -x[1]["count"]):
        device_stats.append({
            "make": make,
            "model": model,
            "dtype": dtype,
            "dtype_label": DTYPE_LABELS.get(dtype, "未识别"),
            "count": d["count"],
            "photos": d["photos"],
            "videos": d["videos"],
            "is_target": _is_target_make(make, target_devices),
        })

    # 来源文件夹统计
    folder_data = defaultdict(lambda: {
        "total": 0, "camera": 0, "phone": 0, "unknown": 0,
        "copied": 0, "dup": 0, "filtered": 0, "not_target": 0,
    })
    for r in result.records:
        folder = os.path.dirname(r.source)
        d = folder_data[folder]
        d["total"] += 1
        dtype = r.device_type
        if dtype == "camera":
            d["camera"] += 1
        elif dtype == "phone":
            d["phone"] += 1
        else:
            d["unknown"] += 1
        if r.status in ("ok", "dry_run"):
            d["copied"] += 1
        elif r.status == "skipped_dup":
            d["dup"] += 1
        elif r.status == "skipped_filtered":
            d["filtered"] += 1
        elif r.status in ("skipped_not_target", "skipped_no_device"):
            d["not_target"] += 1

    source_folders = []
    for path, d in sorted(folder_data.items(), key=lambda x: -x[1]["total"]):
        source_folders.append({"path": path, **d})

    target_display = ", ".join(sorted(target_devices)) if target_devices else "全部"

    # ── 详情表 JS 数据（分页渲染，避免大 DOM） ──
    records_js = [_record_to_js_array(r) for r in result.records]

    # ── 重复文件对比列表 ──
    dup_records = []
    for r in result.records:
        if r.status == "skipped_dup" and r.dup_of:
            dup_records.append({
                "source": r.source,
                "dup_of": r.dup_of,
                "file_hash": r.file_hash[:16] + "..." if r.file_hash else "-",
                "size_display": _human_size(r.file_size),
            })

    # ── 生成 HTML ──
    template = Template(HTML_TEMPLATE)
    html_content = template.render(
        timestamp=timestamp_display,
        target_devices_display=target_display,
        total=len(result.records),
        photo_count=media_counter.get("photo", 0),
        video_count=media_counter.get("video", 0),
        camera_count=type_counter.get("camera", 0),
        phone_count=type_counter.get("phone", 0),
        unknown_count=type_counter.get("unknown", 0),
        copied=status_counter.get("ok", 0) + status_counter.get("dry_run", 0),
        dup_count=status_counter.get("skipped_dup", 0) + status_counter.get("skipped_exists", 0),
        not_target_count=status_counter.get("skipped_not_target", 0),
        no_device_count=status_counter.get("skipped_no_device", 0),
        filtered_count=status_counter.get("skipped_filtered", 0),
        error_count=status_counter.get("error", 0),
        device_stats=device_stats,
        source_folders=source_folders,
        dup_records=dup_records,
        records_json=json.dumps(records_js, ensure_ascii=False),
        records=result.records,
    )

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    logger.info("HTML 报告已生成: %s", html_path)

    # ── 生成 CSV ──
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "序号", "原始路径", "目标路径", "设备类型", "媒体类型",
            "设备品牌", "设备型号", "拍摄日期", "文件大小(字节)",
            "有EXIF日期", "状态", "哈希", "重复原始文件", "错误信息",
        ])
        for i, r in enumerate(result.records, 1):
            writer.writerow([
                i, r.source, r.destination, DTYPE_LABELS.get(r.device_type, r.device_type),
                "视频" if r.media_type == "video" else "照片",
                r.make, r.model, r.date_taken, r.file_size,
                "是" if r.has_exif_date else "否",
                STATUS_LABELS.get(r.status, ("", r.status))[1],
                r.file_hash, r.dup_of, r.error_msg,
            ])
    logger.info("CSV 报告已生成: %s", csv_path)

    return html_path, csv_path
