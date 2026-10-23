#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行测题库打包脚本：把 app.py 打包成单个 exe。

打包进去（内嵌）：
  - app.py / xingce_solver.py 及其依赖（fastapi、uvicorn、openai、yaml 等）

不打包（运行时放到 exe 同级目录）：
  - static/                     （前端目录）
  - config.yaml                 （AI 接口密钥，用户自己填）
  - 01-政治理论 ~ 06-资料分析   （题库 md 数据）
  - 90-图片                     （题目图片）

用法：
  python build.py

产物：
  dist/xingcezhenti-web.exe

提速说明（--onefile 已保留）：
  - 已排除 numpy/pandas/torch/matplotlib 等无关大库，减小体积、加快打包。
  - 默认不 --clean，重复打包走增量缓存。
  - 仍嫌慢的话：给 dist/、build/ 加 Windows Defender 排除项（实时扫描是最大隐藏开销）；
    以及在只装 fastapi uvicorn pyyaml openai pydantic 的干净 venv 里打包。
"""

import os
import sys
import PyInstaller.__main__

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

# uvicorn 动态导入的模块，需显式声明，否则打包后启动会缺模块
HIDDEN_IMPORTS = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

# 未使用但体积巨大、会拖慢打包的第三方库：明确排除，防止被 hook 误打包进 exe。
# （本应用只需 fastapi/uvicorn/pydantic/openai/pyyaml，下面这些都不需要）
EXCLUDES = [
    "numpy", "pandas", "scipy", "matplotlib", "torch", "cv2", "PIL",
    "IPython", "jupyter", "sklearn", "lxml", "cryptography", "tkinter",
    "PyQt5", "PyQt6", "PySide2", "PySide6",
]

# 默认不 --clean：保留 PyInstaller 缓存，重复打包走增量，明显更快。
# 需要全量重建时用 `python build.py --clean`。
args = [
    "app.py",
    "--onefile",
    "--name", "xingcezhenti-web",
    "--console",           # 保留控制台窗口，显示启动日志与访问地址
    "--noconfirm",
]
if "--clean" in sys.argv:
    args.append("--clean")
for m in EXCLUDES:
    args += ["--exclude-module", m]
for h in HIDDEN_IMPORTS:
    args += ["--hidden-import", h]

print("=" * 60)
print("开始打包 xingcezhenti-web …")
print("=" * 60)
PyInstaller.__main__.run(args)

print("\n" + "=" * 60)
print("打包完成！")
print("产物：dist/xingcezhenti-web.exe")
print("=" * 60)
print("运行时请把以下内容放到 exe 同级目录：")
print("  - static/（前端目录）")
print("  - config.yaml（AI 密钥）")
print("  - 01-政治理论 ~ 06-资料分析（题库 md）")
print("  - 90-图片（题目图片）")
print("然后双击 exe，浏览器打开 http://127.0.0.1:8000")
