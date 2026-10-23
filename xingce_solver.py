#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行测做题助手 —— 调用 OpenAI 兼容接口，支持文本 + 图片输入，
输出【分析思路】和【答案】。

逻辑:
  - 从 md 题库抽取题目时，自动剥离原始参考答案（✅标记、**答案**、**官方解析**），
    绝不把参考答案发给模型。
  - 有参考答案的题：模型作答后，在本地判定模型答案对/错。
  - 没有参考答案的题（如 --text 自由输入）：只生成推理与结果，不做判定。
  - 题干 / 选项里的 <img src="..."> 会被解析成真实图片，以多模态方式随文本一起上传。

依赖: openai>=1.0, pyyaml
配置: 读取同目录 config.yaml

用法示例:
  # 1) 解某道题库题目（自动附图、隐藏答案；有参考答案则自动判定对错）
  python xingce_solver.py --file "01-政治理论/2016年423联考《行测》题（新疆卷）.md" --qid 1797776

  # 2) 直接给文本
  python xingce_solver.py --text "下列属于生态移民的是：A... B... C... D..."

  # 3) 文本 + 图片
  python xingce_solver.py --text "观察图形规律，选出答案" --image 90-图片/题目图/1673a8f72539eee.png
"""

import os
import re
import sys
import base64
import argparse
import mimetypes

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    # 打包运行：config.yaml / 题库 md 放在 exe 同级目录
    ROOT = os.path.dirname(sys.executable)
CONFIG_PATH = os.path.join(ROOT, "config.yaml")

HEADER_RE = re.compile(r"^#{1,6}\s*第\s*\d+\s*题")
MATERIAL_RE = re.compile(r"^#{1,6}\s*材料")
QID_RE = re.compile(r"qid\s+(\d+)")
# <sub>qid 123 · 新思想</sub> 里的类别
CAT_RE = re.compile(r"qid\s+\d+\s*[·|]\s*([^<]+?)\s*</")
IMG_SRC_RE = re.compile(r'<img[^>]*?src="([^"]+)"[^>]*?>', re.I)
OPTION_RE = re.compile(r"^-\s*\*\*([A-Z])\*\*[\.．、]?\s*(.*)$")
ANSWER_RE = re.compile(r"^\*\*答案\*\*[:：]")
ANALYSIS_RE = re.compile(r"^\*\*官方解析\*\*")
HR_RE = re.compile(r"^\s*-{3,}\s*$")

SYSTEM_PROMPT = (
    "你是一名资深的公务员行测考试辅导老师。请仔细阅读题目（含图片），"
    "先给出清晰的【分析思路】，逐步推理，再给出明确的【答案】（如果是选择题，"
    "请指出选项字母）。不要编造题目中没有的信息。"
)

SYSTEM_PROMPT_MULTIMODAL_NOTE = (
    "题目中的图片已作为图像附在消息中，图片在文中以 [图片N] 占位标注，"
    "请结合对应图片作答。"
)


# --------------------------- 题库解析 --------------------------- #

def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    oai = cfg["openai"]
    model_id = oai["models"][0]["id"]
    return oai["api_key"], oai["base_url"], model_id


def _clean_html(text):
    """去掉 <p> 等简单标签，保留纯文本（<img> 需在外部先处理成占位符）。"""
    text = re.sub(r"</?(p|div|span|br|sub|sup|strong|em|b|i)[^>]*>", "", text, flags=re.I)
    return text.strip()


def _resolve_img(src, md_dir):
    """把 md 里的相对图片路径解析成绝对路径。"""
    src = src.strip()
    if src.startswith(("http://", "https://")):
        return src  # 远程图片，原样返回
    # 相对 md 文件所在目录
    path = os.path.normpath(os.path.join(md_dir, src))
    return path


def extract_question(md_path, qid=None):
    """从 md 文件中抽取指定 qid（或第一道）的题目。

    返回 dict:
      qid, category, prompt_text(含[图片N]占位), images(绝对路径/URL列表),
      options(dict 字母->文本), ref_answer(参考答案字母, 仅供本地比对)
    """
    md_path = md_path if os.path.isabs(md_path) else os.path.join(ROOT, md_path)
    md_dir = os.path.dirname(md_path)
    with open(md_path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    # 题目头 与 材料头（资料分析为 "## 材料 N"）
    q_idx = [i for i, l in enumerate(lines) if HEADER_RE.match(l)]
    mat_idx = [i for i, l in enumerate(lines) if MATERIAL_RE.match(l)]
    all_headers = sorted(q_idx + mat_idx)
    if not q_idx:
        raise ValueError(f"文件中没有找到题目: {md_path}")

    def next_header_after(i):
        for h in all_headers:
            if h > i:
                return h
        return len(lines)

    target = None
    for start in q_idx:
        m = QID_RE.search(lines[start])
        cur_qid = m.group(1) if m else None
        if qid is None or cur_qid == str(qid):
            # 题块到下一个任意标题（题/材料）前为止，避免把下一则材料并进题干
            end = next_header_after(start)
            target = (cur_qid, lines[start], lines[start + 1:end], start)
            break
    if target is None:
        raise ValueError(f"未找到 qid={qid} 的题目")

    cur_qid, header_line, body, q_start = target
    cat_m = CAT_RE.search(header_line)
    category = cat_m.group(1).strip() if cat_m else ""

    # 该题所属材料：仅 ###（依赖材料）的题归属其前方最近的材料；##（独立题）无材料
    material_lines = []
    if re.match(r"^###\s", header_line):
        prev_mats = [m for m in mat_idx if m < q_start]
        if prev_mats:
            mstart = prev_mats[-1]
            material_lines = lines[mstart + 1:next_header_after(mstart)]

    images = []

    def register_images(s):
        """把一段文本里的 <img> 替换成 [图片N] 占位，并登记图片路径。"""
        def repl(m):
            src = m.group(1)
            resolved = _resolve_img(src, md_dir)
            images.append(resolved)
            return f"[图片{len(images)}]"
        return IMG_SRC_RE.sub(repl, s)

    # 先登记材料里的图片（保证 [图片N] 编号材料在前、题干在后）
    material_text = ""
    if material_lines:
        mt = [_clean_html(register_images(l)) for l in material_lines]
        material_text = re.sub(r"\n{3,}", "\n\n", "\n".join(mt).strip())

    stem_lines = []
    options = {}
    ref_answer = None
    in_analysis = False

    for line in body:
        if ANALYSIS_RE.match(line.strip()):
            in_analysis = True           # 从这里开始是官方解析，全部丢弃（不发给模型）
            continue
        if in_analysis:
            continue
        if ANSWER_RE.match(line.strip()):
            # 记录参考答案仅供本地比对，绝不进入发送内容
            ra = re.sub(r"^\*\*答案\*\*[:：]\s*", "", line.strip())
            ref_answer = ra.strip()
            continue
        if HR_RE.match(line):
            continue

        opt_m = OPTION_RE.match(line.strip())
        if opt_m:
            letter, otext = opt_m.group(1), opt_m.group(2)
            # 去掉正确标记 ✅ 及其前面的全角/半角空格
            otext = otext.replace("✅", "").replace("√", "").rstrip("　 ")
            otext = _clean_html(register_images(otext))
            options[letter] = otext
            continue

        # 题干行
        txt = register_images(line)
        txt = _clean_html(txt)
        if txt or stem_lines:
            stem_lines.append(txt)

    stem = "\n".join(stem_lines).strip()
    stem = re.sub(r"\n{3,}", "\n\n", stem)

    return {
        "qid": cur_qid,
        "category": category,
        "material": material_text,
        "stem": stem,
        "options": options,
        "images": images,
        "ref_answer": ref_answer,
    }


def format_question_text(q):
    """把题目组装成要发给模型的纯文本（不含任何参考答案）。"""
    parts = []
    if q.get("category"):
        parts.append(f"【题型】{q['category']}")
    if q.get("material"):
        parts.append("【材料】")
        parts.append(q["material"])
        parts.append("")
    parts.append("【题目】")
    parts.append(q["stem"])
    if q["options"]:
        parts.append("")
        parts.append("【选项】")
        for letter in sorted(q["options"]):
            parts.append(f"{letter}. {q['options'][letter]}")
    parts.append("")
    parts.append("请给出【分析思路】和【答案】。")
    return "\n".join(parts)


# --------------------------- 多模态消息 --------------------------- #

def image_to_data_url(path):
    """本地图片 -> data URL；远程 URL 原样返回。"""
    if path.startswith(("http://", "https://")):
        return path
    if not os.path.exists(path):
        print(f"[警告] 图片不存在，已跳过: {path}", file=sys.stderr)
        return None
    mime, _ = mimetypes.guess_type(path)
    mime = mime or "image/png"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def build_messages(text, images):
    """构造 OpenAI chat 多模态 messages。"""
    system = SYSTEM_PROMPT
    if images:
        system += "\n" + SYSTEM_PROMPT_MULTIMODAL_NOTE

    content = [{"type": "text", "text": text}]
    for img in images:
        url = image_to_data_url(img)
        if url:
            content.append({"type": "image_url", "image_url": {"url": url}})

    # 无图片时用纯字符串 content，兼容纯文本模型
    user_content = content if images else text
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


def extract_model_answer(out):
    """从模型输出里尽量稳妥地解析出答案。
    返回标准化字符串：选择题为字母集合（如 'A' 或 'ABD'）；判断题为 '正确'/'错误'；
    解析不到返回 ''。
    """
    if not out:
        return ""
    # 优先取【答案】之后的内容
    m = re.search(r"【?\s*答案\s*】?\s*[:：]?\s*(.+)", out)
    tail = m.group(1) if m else out
    tail = tail.splitlines()[0] if m else out  # 答案通常在同一行

    # 判断题
    if re.search(r"正确|错误|对|错|是|否", tail) and not re.search(r"[A-Da-d]", tail):
        if re.search(r"正确|对|是", tail):
            return "正确"
        if re.search(r"错误|错|否", tail):
            return "错误"

    letters = re.findall(r"[A-Da-d]", tail)
    if letters:
        return "".join(sorted(set(c.upper() for c in letters)))

    # 兜底：全文找字母
    letters = re.findall(r"[A-D]", out)
    return "".join(sorted(set(letters))) if letters else ""


def normalize_ref(ref):
    """标准化参考答案，便于比对。"""
    if not ref:
        return ""
    ref = ref.strip()
    if re.search(r"正确|对", ref) and not re.search(r"[A-Da-d]", ref):
        return "正确"
    if re.search(r"错误|错", ref) and not re.search(r"[A-Da-d]", ref):
        return "错误"
    letters = re.findall(r"[A-Da-d]", ref)
    if letters:
        return "".join(sorted(set(c.upper() for c in letters)))
    return ref


def judge(model_ans, ref_answer):
    """比对模型答案与参考答案，返回 (是否正确, 标准化模型答案, 标准化参考答案)。"""
    ma = extract_model_answer(model_ans)
    ra = normalize_ref(ref_answer)
    correct = bool(ma) and bool(ra) and ma == ra
    return correct, ma, ra


def solve(text, images=None, temperature=0.3, max_retries=4):
    """调用模型，返回模型输出文本。遇到 429 限频自动退避重试。"""
    import time
    from openai import OpenAI
    from openai import RateLimitError
    api_key, base_url, model_id = load_config()
    client = OpenAI(api_key=api_key, base_url=base_url)
    messages = build_messages(text, images or [])

    delay = 5
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model_id,
                messages=messages,
                temperature=temperature,
            )
            return resp.choices[0].message.content
        except RateLimitError:
            if attempt == max_retries - 1:
                raise
            print(f"[限频 429] {delay}s 后重试（第 {attempt + 1}/{max_retries - 1} 次）...",
                  file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


# --------------------------- CLI --------------------------- #

def main():
    ap = argparse.ArgumentParser(description="行测做题助手（OpenAI 接口，支持文本+图片）")
    ap.add_argument("--file", help="题库 md 文件路径（相对项目根或绝对路径）")
    ap.add_argument("--qid", help="题目 qid（配合 --file；不填则取文件第一题）")
    ap.add_argument("--text", help="直接输入的题目文本")
    ap.add_argument("--image", action="append", default=[], help="附加图片路径，可重复")
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--dry-run", action="store_true", help="只打印将发送的内容，不调用接口")
    args = ap.parse_args()

    images = list(args.image)
    ref_answer = None
    qinfo = None

    if args.file:
        qinfo = extract_question(args.file, args.qid)
        text = format_question_text(qinfo)
        images = qinfo["images"] + images       # 题库图片 + 附加图片
        ref_answer = qinfo["ref_answer"]
    elif args.text:
        text = args.text
    else:
        ap.error("请至少提供 --file 或 --text")

    print("=" * 60)
    print("【发送给模型的内容（已剔除参考答案）】")
    print("=" * 60)
    print(text)
    if images:
        print(f"\n[附带图片 {len(images)} 张]")
        for i, im in enumerate(images, 1):
            exists = "✓" if (im.startswith("http") or os.path.exists(im)) else "✗缺失"
            print(f"  图片{i}: {im}  [{exists}]")

    has_ref = bool(ref_answer and ref_answer.strip())
    print(f"\n[本题{'有' if has_ref else '无'}参考答案]"
          + ("（将判定模型答案对错，参考答案不发送给模型）"
             if has_ref else "（仅生成推理与结果，不做判定）"))

    if args.dry_run:
        if has_ref:
            print(f"[dry-run] 本地参考答案（不发送）: {ref_answer}")
        return

    print("\n" + "=" * 60)
    print("【模型分析与答案】")
    print("=" * 60)
    try:
        out = solve(text, images, args.temperature)
    except Exception as e:
        print(f"[调用失败] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    if not (out and out.strip()):
        print("[提示] 模型返回为空。若本题含图片，可能是当前模型不支持图像输入"
              "（需在 config.yaml 配置一个支持 vision 的多模态模型）。", file=sys.stderr)
        return
    print(out)

    # 有参考答案 -> 判定对错；无参考答案 -> 到此为止
    if has_ref:
        correct, model_ans, ref_norm = judge(out, ref_answer)
        print("\n" + "=" * 60)
        print("【判定结果】")
        print("=" * 60)
        print(f"参考答案: {ref_norm or ref_answer}")
        print(f"模型答案: {model_ans or '(未能解析)'}")
        print(f"判定: {'✅ 正确' if correct else '❌ 错误'}")


if __name__ == "__main__":
    main()
