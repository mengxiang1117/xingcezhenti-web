#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行测题库 Web 应用（FastAPI）。

功能:
  - 加载全部 md 题目，按【模块 → 题型】分类浏览。
  - 每题可选选项并提交，即时判对错。
  - 提交后可查看「参考答案（正确答案+官方解析）」与「AI 解析」。
  - AI 解析结果本地缓存（ai_cache/{qid}.json），下次直接加载，不再调用 AI。

启动:
  python -m uvicorn app:app --host 127.0.0.1 --port 8000
  或:  python app.py
"""

import os
import re
import json
import sys
import random
import uuid
from datetime import datetime
from collections import defaultdict, OrderedDict

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import xingce_solver as solver

ROOT = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    # 打包运行：所有资源（题库 md / config.yaml / static / 90-图片 / exams / 进度）都在 exe 同级目录
    ROOT = os.path.dirname(sys.executable)

IMG_DIR = os.path.join(ROOT, "90-图片")
STATIC_DIR = os.path.join(ROOT, "static")
INDEX_CACHE = os.path.join(ROOT, "question_index.json")
AI_CACHE_DIR = os.path.join(ROOT, "ai_cache")
PROGRESS_FILE = os.path.join(ROOT, "progress.json")
BACKUP_DIR = os.path.join(ROOT, "_backup_qid_dedup")

# 只扫描这 6 个模块目录（排除 待修正-材料h3过多、90-图片、备份目录等）
MODULE_DIRS = [
    "01-政治理论",
    "02-常识判断",
    "03-言语理解与表达",
    "04-数量关系",
    "05-判断推理",
    "06-资料分析",
]

# 套题存储目录
EXAMS_DIR = os.path.join(ROOT, "exams")

# 三类套题的题量配置（模块 -> 题数）
EXAM_TYPES = {
    "省级综合管理类": {
        "01-政治理论": 20, "02-常识判断": 15, "03-言语理解与表达": 30,
        "04-数量关系": 15, "05-判断推理": 35, "06-资料分析": 20,
    },
    "市地级综合管理类": {
        "01-政治理论": 20, "02-常识判断": 15, "03-言语理解与表达": 30,
        "04-数量关系": 10, "05-判断推理": 35, "06-资料分析": 20,
    },
    "行政执法类": {
        "01-政治理论": 20, "02-常识判断": 15, "03-言语理解与表达": 30,
        "04-数量关系": 10, "05-判断推理": 35, "06-资料分析": 20,
    },
}

HEADER_RE = re.compile(r"^#{1,6}\s*第\s*\d+\s*题")
MATERIAL_RE = re.compile(r"^#{1,6}\s*材料")
QID_RE = re.compile(r"qid\s+(\d+)")
CAT_RE = re.compile(r"qid\s+\d+\s*[·|]\s*([^<]+?)\s*</")
OPTION_RE = re.compile(r"^-\s*\*\*([A-Z])\*\*[\.．、]?\s*(.*)$")
ANSWER_RE = re.compile(r"^\*\*答案\*\*[:：]\s*(.*)$")
ANALYSIS_RE = re.compile(r"^\*\*官方解析\*\*")
HR_RE = re.compile(r"^\s*-{3,}\s*$")
IMG_SRC_RE = re.compile(r'(<img[^>]*?src=")([^"]+)("[^>]*?>)', re.I)
FM_PAPER_RE = re.compile(r'^试卷[:：]\s*"?([^"]+)"?\s*$')


# ----------------------------- 解析 / 索引 ----------------------------- #

def rewrite_img(html):
    """把 md 里的图片路径重写为 /img/... 便于前端加载。"""
    def repl(m):
        src = m.group(2).strip()
        if src.startswith(("http://", "https://")):
            return m.group(0)
        # 去掉形如 ../90-图片/ 或 90-图片/ 的前缀
        src = re.sub(r"^(\.\./)*", "", src)
        src = re.sub(r"^90-图片/", "", src)
        return f"{m.group(1)}/img/{src}{m.group(3)}"
    return IMG_SRC_RE.sub(repl, html)


def module_name(module_dir):
    """01-政治理论 -> 政治理论。"""
    return re.sub(r"^\d+\s*[-_]\s*", "", module_dir)


def parse_file(md_path):
    """解析一个 md 文件，返回该文件内所有题目（保留 HTML）。"""
    rel = os.path.relpath(md_path, ROOT).replace("\\", "/")
    module = rel.split("/")[0]
    with open(md_path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    # frontmatter 里的试卷名
    paper = None
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                break
            m = FM_PAPER_RE.match(lines[j].strip())
            if m:
                paper = m.group(1).strip()
    if not paper:
        paper = os.path.splitext(os.path.basename(md_path))[0]

    q_idx = [i for i, l in enumerate(lines) if HEADER_RE.match(l)]
    mat_idx = [i for i, l in enumerate(lines) if MATERIAL_RE.match(l)]
    all_headers = sorted(q_idx + mat_idx)

    def next_header_after(i):
        for h in all_headers:
            if h > i:
                return h
        return len(lines)

    # 预解析各材料块（资料分析："## 材料 N" + 其后文字/图表）
    materials = {}  # 材料起始行 -> {no, html}
    for mno, mstart in enumerate(mat_idx, 1):
        mbody = lines[mstart + 1:next_header_after(mstart)]
        materials[mstart] = {
            "no": mno,
            "html": rewrite_img("\n".join(mbody).strip()),
        }

    questions = []
    for start in q_idx:
        end = next_header_after(start)
        header = lines[start]
        body = lines[start + 1:end]

        qm = QID_RE.search(header)
        if not qm:
            continue
        qid = qm.group(1)
        cm = CAT_RE.search(header)
        category = cm.group(1).strip() if cm else "未分类"

        # 该题所属材料：仅 ###（依赖材料）的题归属其前方最近的材料；
        # ##（独立题）不归属任何材料，否则会把材料后的独立题都算进最后一则材料
        prev_mats = [m for m in mat_idx if m < start]
        is_material_q = bool(re.match(r"^###\s", header))
        material = materials.get(prev_mats[-1]) if (is_material_q and prev_mats) else None

        stem_lines, options, answer = [], {}, None
        analysis_lines, in_analysis = [], False
        for line in body:
            s = line.strip()
            if ANALYSIS_RE.match(s):
                in_analysis = True
                continue
            if in_analysis:
                if HR_RE.match(line):
                    continue
                analysis_lines.append(line)
                continue
            am = ANSWER_RE.match(s)
            if am:
                answer = am.group(1).strip()
                continue
            if HR_RE.match(line):
                continue
            om = OPTION_RE.match(s)
            if om:
                letter, otext = om.group(1), om.group(2)
                otext = otext.replace("✅", "").replace("√", "").rstrip("　 ")
                options[letter] = rewrite_img(otext)
                continue
            if s or stem_lines:
                stem_lines.append(line)

        # 答案归一化成字母（如 "A" / "A项" / "AB"）
        ans_letters = "".join(sorted(set(re.findall(r"[A-Z]", answer or ""))))
        questions.append({
            "qid": qid,
            "module": module,
            "module_name": module_name(module),
            "category": category,
            "paper": paper,
            "file": rel,
            "stem_html": rewrite_img("\n".join(stem_lines).strip()),
            "options": options,
            "answer": ans_letters or (answer or ""),
            "official_analysis_html": rewrite_img("\n".join(analysis_lines).strip()),
            "material_no": material["no"] if material else 0,
            "material_html": material["html"] if material else "",
            # 材料组内的稳定标识：文件 + 材料序号
            "material_key": f"{rel}#{material['no']}" if material else "",
        })
    return questions


def parse_file_meta(md_path):
    """解析一个 md 文件，返回元数据列表（轻量：qid/分类/预览等，不含题干/选项/解析全文）。"""
    rel = os.path.relpath(md_path, ROOT).replace("\\", "/")
    module = rel.split("/")[0]
    with open(md_path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    paper = None
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                break
            m = FM_PAPER_RE.match(lines[j].strip())
            if m:
                paper = m.group(1).strip()
    if not paper:
        paper = os.path.splitext(os.path.basename(md_path))[0]

    q_idx = [i for i, l in enumerate(lines) if HEADER_RE.match(l)]
    mat_idx = [i for i, l in enumerate(lines) if MATERIAL_RE.match(l)]
    all_headers = sorted(q_idx + mat_idx)

    def next_header_after(i):
        for h in all_headers:
            if h > i:
                return h
        return len(lines)

    mat_no = {mstart: mno for mno, mstart in enumerate(mat_idx, 1)}

    meta_list = []
    for start in q_idx:
        end = next_header_after(start)
        header = lines[start]
        qm = QID_RE.search(header)
        if not qm:
            continue
        qid = qm.group(1)
        cm = CAT_RE.search(header)
        category = cm.group(1).strip() if cm else "未分类"

        is_material_q = bool(re.match(r"^###\s", header))
        prev_mats = [m for m in mat_idx if m < start]
        material_no = mat_no.get(prev_mats[-1], 0) if (is_material_q and prev_mats) else 0
        material_key = f"{rel}#{material_no}" if material_no else ""

        preview = ""
        has_img = False
        for line in lines[start + 1:end]:
            s = line.strip()
            if s.startswith("- **") or s.startswith("**答案**") or s.startswith("**官方解析**") or HR_RE.match(line):
                break
            if "<img" in line:
                has_img = True
            txt = re.sub(r"<[^>]+>", "", line).replace("&nbsp;", " ").strip()
            if txt:
                preview = (preview + " " + txt).strip()
                if len(preview) >= 60:
                    preview = preview[:60] + "…"
                    break

        meta_list.append({
            "qid": qid, "module": module, "module_name": module_name(module),
            "category": category, "paper": paper, "file": rel,
            "material_no": material_no, "material_key": material_key,
            "preview": preview, "has_img": has_img,
        })
    return meta_list


def iter_md_files():
    skip_names = {"readme.md", "蒸馏方法参考.md"}
    for mod in MODULE_DIRS:
        d = os.path.join(ROOT, mod)
        if not os.path.isdir(d):
            continue
        for dp, _, fs in os.walk(d):
            for name in fs:
                if name.lower().endswith(".md") and name.lower() not in skip_names:
                    yield os.path.join(dp, name)


def build_index():
    """构建全量题目索引（仅元数据，轻量）。"""
    questions = []
    for path in sorted(iter_md_files()):
        try:
            questions.extend(parse_file_meta(path))
        except Exception as e:  # 单文件出错不影响整体
            print(f"[解析失败] {path}: {e}")
    return questions


# 全文解析缓存：文件路径 -> {qid: 完整题目 dict}（LRU，避免反复解析同一文件）
_FILE_CACHE = OrderedDict()
FILE_CACHE_MAX = 300


def _parse_file_cached(rel):
    if rel in _FILE_CACHE:
        _FILE_CACHE.move_to_end(rel)
        return _FILE_CACHE[rel]
    full = {}
    try:
        for q in parse_file(os.path.join(ROOT, rel)):
            full[q["qid"]] = q
    except Exception:
        pass
    _FILE_CACHE[rel] = full
    if len(_FILE_CACHE) > FILE_CACHE_MAX:
        _FILE_CACHE.popitem(last=False)
    return full


def get_full_question(qid):
    """按 qid 从 md 文件按需解析出完整题目（含题干/选项/答案/解析/材料）。"""
    meta = BY_QID.get(qid)
    if not meta:
        return None
    return _parse_file_cached(meta["file"]).get(qid)


# ----------------------------- 内存状态 ----------------------------- #

QUESTIONS = []          # list[dict]  元数据索引
BY_QID = {}             # qid -> 元数据
TREE = []               # 分类树
PROGRESS = {}           # qid -> {"choice":..., "correct":bool}  做题进度（服务端本地文件）


def load_progress():
    global PROGRESS
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, encoding="utf-8") as f:
                PROGRESS = json.load(f)
        except Exception:
            PROGRESS = {}
    else:
        PROGRESS = {}


def save_progress():
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(PROGRESS, f, ensure_ascii=False)


def rebuild_tree():
    global TREE
    mod_map = {}  # module -> {module_name, count, categories:{cat:count}}
    for q in QUESTIONS:
        m = mod_map.setdefault(q["module"], {
            "module": q["module"], "module_name": q["module_name"],
            "count": 0, "cats": {}})
        m["count"] += 1
        m["cats"][q["category"]] = m["cats"].get(q["category"], 0) + 1
    tree = []
    for module in sorted(mod_map):
        m = mod_map[module]
        cats = [{"name": c, "count": n} for c, n in sorted(m["cats"].items())]
        tree.append({"module": module, "module_name": m["module_name"],
                     "count": m["count"], "categories": cats})
    TREE = tree


def load_or_build(force=False):
    global QUESTIONS, BY_QID
    if (not force) and os.path.exists(INDEX_CACHE):
        with open(INDEX_CACHE, encoding="utf-8") as f:
            QUESTIONS = json.load(f)
        print(f"[索引] 从缓存加载 {len(QUESTIONS)} 题")
    else:
        print("[索引] 正在解析全部 md ...")
        QUESTIONS = build_index()
        with open(INDEX_CACHE, "w", encoding="utf-8") as f:
            json.dump(QUESTIONS, f, ensure_ascii=False)
        print(f"[索引] 构建完成 {len(QUESTIONS)} 题，已写入 {INDEX_CACHE}")
    BY_QID = {q["qid"]: q for q in QUESTIONS}
    rebuild_tree()


# ----------------------------- 套题生成与存储 ----------------------------- #

def _exam_path(exam_id):
    return os.path.join(EXAMS_DIR, f"{exam_id}.json")


def save_exam(exam):
    os.makedirs(EXAMS_DIR, exist_ok=True)
    with open(_exam_path(exam["id"]), "w", encoding="utf-8") as f:
        json.dump(exam, f, ensure_ascii=False, indent=2)


def load_exam(exam_id):
    p = _exam_path(exam_id)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def list_exams():
    if not os.path.isdir(EXAMS_DIR):
        return []
    exams = []
    for name in os.listdir(EXAMS_DIR):
        if name.endswith(".json"):
            try:
                with open(os.path.join(EXAMS_DIR, name), encoding="utf-8") as f:
                    exams.append(json.load(f))
            except Exception:
                continue
    exams.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return exams


def delete_exam(exam_id):
    p = _exam_path(exam_id)
    if os.path.exists(p):
        os.remove(p)


def select_questions(pool, target, material_only=False):
    """从某模块题库里抽 target 道题。

    材料题（material_key 非空）按整则材料抽（不拆材料），独立题按单题抽。
    material_only=True 时（资料分析）只按整则材料抽，允许略超 target。
    """
    groups = defaultdict(list)
    standalone = []
    for q in pool:
        key = q.get("material_key")
        if key:
            groups[key].append(q)
        else:
            standalone.append(q)

    glist = list(groups.values())
    random.shuffle(glist)
    random.shuffle(standalone)

    if material_only:
        # 整则材料抽：多次随机顺序，取最接近 target 的组合（尽量精确凑到 20）
        best = None
        for _ in range(40):
            random.shuffle(glist)
            picked = []
            for g in glist:
                picked.extend(g)
                if len(picked) >= target:
                    break
            if best is None or abs(len(picked) - target) < abs(len(best) - target):
                best = picked
            if best and len(best) == target:
                break
        return best or []

    # 材料组 + 独立题混排抽取，尽量接近 target 且不拆材料
    units = glist + [[q] for q in standalone]
    random.shuffle(units)
    picked = []
    for u in units:
        if len(picked) + len(u) > target:
            continue
        picked.extend(u)
        if len(picked) >= target:
            break

    # 因跳过超大材料导致不足 target 时，用独立题补足
    if len(picked) < target:
        for q in standalone:
            if len(picked) >= target:
                break
            if not any(x["qid"] == q["qid"] for x in picked):
                picked.append(q)
    return picked


def generate_exam(exam_type, time_limit=120):
    if exam_type not in EXAM_TYPES:
        raise ValueError("未知套题类型")
    config = EXAM_TYPES[exam_type]
    exam_id = f"exam_{uuid.uuid4().hex[:12]}"
    questions = []
    order = 0
    for module in MODULE_DIRS:
        target = config.get(module, 0)
        if target <= 0:
            continue
        pool = [q for q in QUESTIONS if q["module"] == module]
        material_only = (module == "06-资料分析")
        picked = select_questions(pool, target, material_only)
        for q in picked:
            questions.append({"section": module, "qid": q["qid"], "order": order})
            order += 1

    exam = {
        "id": exam_id,
        "type": exam_type,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(questions),
        "questions": questions,
        "answers": {},
        "completed": False,
        "finished_at": None,
        "score": None,
        "time_limit": int(time_limit),   # 时间限制（分钟）
        "elapsed": 0,                     # 已用秒数（答题时累计，下次进入按剩余时间续算）
    }
    save_exam(exam)
    return exam


# ----------------------------- FastAPI ----------------------------- #

app = FastAPI(title="行测题库")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def _startup():
    os.makedirs(AI_CACHE_DIR, exist_ok=True)
    load_or_build(force=True)   # 每次启动都重新解析 md、重建索引
    load_progress()
    print(f"[进度] 已加载 {len(PROGRESS)} 条做题记录")


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/tree")
def api_tree():
    return TREE


@app.get("/api/questions")
def api_questions(module: str = "", category: str = "", page: int = 1,
                  page_size: int = 20):
    items = [q for q in QUESTIONS
             if (not module or q["module"] == module)
             and (not category or q["category"] == category)]
    total = len(items)
    page = max(1, page)
    start = (page - 1) * page_size
    chunk = items[start:start + page_size]

    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [{"qid": q["qid"], "preview": q.get("preview", ""),
                   "has_img": q.get("has_img", False)} for q in chunk],
    }


@app.get("/api/question_ids")
def api_question_ids(module: str = "", category: str = ""):
    """返回某模块/题型下按顺序排列的全部 qid（供上一题/下一题导航）。"""
    ids = [q["qid"] for q in QUESTIONS
           if (not module or q["module"] == module)
           and (not category or q["category"] == category)]
    return {"qids": ids}


@app.get("/api/question/{qid}")
def api_question(qid: str):
    q = get_full_question(qid)
    if not q:
        raise HTTPException(404, "题目不存在")
    # 不下发 answer / official_analysis_html
    return {
        "qid": q["qid"], "module": q["module"], "module_name": q["module_name"],
        "category": q["category"], "paper": q["paper"],
        "stem_html": q["stem_html"], "options": q["options"],
        "has_material": bool(q.get("material_key")),
        "material_key": q.get("material_key", ""),
    }


@app.get("/api/materials")
def api_materials(module: str = ""):
    """资料分析用：列出某模块下的所有材料，按【来源(试卷) → 材料N】分组。"""
    groups = {}
    for q in QUESTIONS:
        if module and q["module"] != module:
            continue
        key = q.get("material_key")
        if not key:
            continue
        g = groups.get(key)
        if not g:
            g = {"material_key": key, "material_no": q.get("material_no", 0),
                 "paper": q["paper"], "first_qid": q["qid"], "count": 0, "qids": []}
            groups[key] = g
        g["count"] += 1
        g["qids"].append(q["qid"])

    mats = sorted(groups.values(), key=lambda m: (m["paper"], m["material_no"]))
    # 再按 来源(paper) 聚合
    papers, order = {}, []
    for m in mats:
        if m["paper"] not in papers:
            papers[m["paper"]] = []
            order.append(m["paper"])
        papers[m["paper"]].append(m)
    return {"total_materials": len(mats),
            "papers": [{"paper": p, "materials": papers[p]} for p in order]}


@app.get("/api/material/{qid}")
def api_material(qid: str):
    """返回某题所在的材料组：材料 + 组内全部题目（不含答案/解析）。"""
    meta = BY_QID.get(qid)
    if not meta:
        raise HTTPException(404, "题目不存在")
    key = meta.get("material_key")
    if not key:
        raise HTTPException(400, "该题不属于任何材料")
    group_meta = [x for x in QUESTIONS if x.get("material_key") == key]
    qs = []
    material_html = ""
    for x in group_meta:
        fq = get_full_question(x["qid"])
        if not fq:
            continue
        material_html = material_html or fq.get("material_html", "")
        qs.append({"qid": fq["qid"], "category": fq["category"],
                   "stem_html": fq["stem_html"], "options": fq["options"]})
    return {
        "material_key": key,
        "material_no": meta.get("material_no", 0),
        "material_html": material_html,
        "module_name": meta["module_name"], "paper": meta["paper"],
        "questions": qs,
    }


class SubmitBody(BaseModel):
    qid: str
    choice: str


@app.post("/api/submit")
def api_submit(body: SubmitBody):
    q = get_full_question(body.qid)
    if not q:
        raise HTTPException(404, "题目不存在")
    choice = "".join(sorted(set(re.findall(r"[A-Z]", body.choice.upper()))))
    correct = bool(q["answer"]) and choice == q["answer"]
    # 记录到服务端本地文件
    PROGRESS[body.qid] = {"choice": choice, "correct": correct}
    save_progress()
    return {"correct": correct, "answer": q["answer"]}


@app.get("/api/progress")
def api_progress():
    """返回全部做题进度（qid -> {choice, correct}）。"""
    return PROGRESS


@app.get("/api/reference/{qid}")
def api_reference(qid: str):
    q = get_full_question(qid)
    if not q:
        raise HTTPException(404, "题目不存在")
    return {"answer": q["answer"],
            "official_analysis_html": q["official_analysis_html"]}


@app.get("/api/ai/{qid}")
def api_ai(qid: str):
    q = BY_QID.get(qid)
    if not q:
        raise HTTPException(404, "题目不存在")

    cache_path = os.path.join(AI_CACHE_DIR, f"{qid}.json")
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        data["cached"] = True
        return data

    # 1) 配置检查：没有 config 文件或读取失败 -> 模型不可用（不缓存）
    if not os.path.exists(solver.CONFIG_PATH):
        raise HTTPException(503, "模型不可用：未找到配置文件 config.yaml")
    try:
        api_key, base_url, model_id = solver.load_config()
    except Exception as e:
        raise HTTPException(503, f"模型不可用：配置文件读取失败（{type(e).__name__}）")

    # 2) 现场生成：复用 solver，天然不含参考答案。模型不可达/报错 -> 模型不可用（不缓存）
    try:
        qinfo = solver.extract_question(q["file"], qid)
        text = solver.format_question_text(qinfo)
        analysis = solver.solve(text, qinfo["images"])
    except Exception as e:
        raise HTTPException(503, f"模型不可用：{type(e).__name__}")

    if not (analysis and analysis.strip()):
        raise HTTPException(503, "模型不可用：返回为空（当前模型可能不支持图片）")

    # 3) 仅在成功时写入缓存
    data = {"analysis": analysis, "model": model_id}
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    data["cached"] = False
    return data


@app.get("/api/rebuild")
def api_rebuild():
    load_or_build(force=True)
    return {"ok": True, "count": len(QUESTIONS)}


# ----------------------------- 套题接口 ----------------------------- #

class GenerateBody(BaseModel):
    type: str
    time_limit: int = 120


class ExamAnswerBody(BaseModel):
    qid: str
    choice: str


class SyncBody(BaseModel):
    elapsed: int


@app.post("/api/exam/generate")
def api_exam_generate(body: GenerateBody):
    try:
        exam = generate_exam(body.type, body.time_limit)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return exam


@app.get("/api/exam/list")
def api_exam_list():
    return {"exams": [{
        "id": e["id"], "type": e["type"], "total": e["total"],
        "answered": len(e.get("answers", {})),
        "completed": e.get("completed", False),
        "created_at": e.get("created_at", ""),
        "score": e.get("score"),
        "time_limit": e.get("time_limit", 120),
        "elapsed": e.get("elapsed", 0),
    } for e in list_exams()]}


@app.get("/api/exam/{exam_id}")
def api_exam(exam_id: str):
    exam = load_exam(exam_id)
    if not exam:
        raise HTTPException(404, "套题不存在")
    items = []
    for qq in exam["questions"]:
        q = get_full_question(qq["qid"])
        if not q:
            continue
        item = {
            "qid": q["qid"], "section": qq["section"], "order": qq["order"],
            "module_name": q["module_name"], "category": q["category"],
            "stem_html": q["stem_html"], "options": q["options"],
            "material_key": q.get("material_key", ""),
            "material_no": q.get("material_no", 0),
            "material_html": q.get("material_html", ""),
        }
        if exam.get("completed"):
            item["answer"] = q["answer"]  # 交卷后下发参考答案供复盘
            item["official_analysis_html"] = q["official_analysis_html"]
        items.append(item)
    return {
        "id": exam["id"], "type": exam["type"], "total": exam["total"],
        "created_at": exam.get("created_at", ""),
        "questions": items,
        "answers": exam.get("answers", {}),
        "completed": exam.get("completed", False),
        "score": exam.get("score"),
        "time_limit": exam.get("time_limit", 120),
        "elapsed": exam.get("elapsed", 0),
    }


@app.post("/api/exam/{exam_id}/answer")
def api_exam_answer(exam_id: str, body: ExamAnswerBody):
    exam = load_exam(exam_id)
    if not exam:
        raise HTTPException(404, "套题不存在")
    exam.setdefault("answers", {})[body.qid] = body.choice
    save_exam(exam)
    return {"ok": True}


@app.post("/api/exam/{exam_id}/sync")
def api_exam_sync(exam_id: str, body: SyncBody):
    """同步已用秒数（答题时前端倒计时，定期回传，保证下次进入按剩余时间续算）。"""
    exam = load_exam(exam_id)
    if not exam:
        raise HTTPException(404, "套题不存在")
    exam["elapsed"] = max(0, int(body.elapsed))
    save_exam(exam)
    return {"ok": True}


@app.post("/api/exam/{exam_id}/finish")
def api_exam_finish(exam_id: str):
    exam = load_exam(exam_id)
    if not exam:
        raise HTTPException(404, "套题不存在")
    total = correct = wrong = unanswered = 0
    by_section = {}
    for qq in exam["questions"]:
        q = get_full_question(qq["qid"])
        if not q:
            continue
        total += 1
        sec = by_section.setdefault(qq["section"], {"total": 0, "correct": 0, "wrong": 0, "unanswered": 0})
        sec["total"] += 1
        ans = q["answer"]
        choice = exam.get("answers", {}).get(qq["qid"], "")
        if not choice:
            unanswered += 1
            sec["unanswered"] += 1
        elif "".join(sorted(set(re.findall(r"[A-Z]", choice.upper())))) == ans:
            correct += 1
            sec["correct"] += 1
        else:
            wrong += 1
            sec["wrong"] += 1
    exam["completed"] = True
    exam["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    exam["score"] = {
        "total": total, "correct": correct, "wrong": wrong, "unanswered": unanswered,
        "by_section": by_section,
    }
    save_exam(exam)
    return exam["score"]


@app.delete("/api/exam/{exam_id}")
def api_exam_delete(exam_id: str):
    if not load_exam(exam_id):
        raise HTTPException(404, "套题不存在")
    delete_exam(exam_id)
    return {"ok": True}


@app.get("/img/{path:path}")
def img(path: str):
    full = os.path.normpath(os.path.join(IMG_DIR, path))
    if not full.startswith(os.path.abspath(IMG_DIR)) or not os.path.exists(full):
        raise HTTPException(404, "图片不存在")
    return FileResponse(full)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
