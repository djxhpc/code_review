"""
本地多模型交叉 Code Review 工具

用四個本地 Ollama 模型分別審查同一份程式碼，各自的結果先預覽，
最後再用一個整合模型把四份意見合併成一份繁體中文報告（十級優先度）。

需求：本機已啟動 Ollama (http://localhost:11434)。
執行：python code_review_gui.py
"""

import json
import os
import queue
import re
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import requests

try:
    import opencc
    _CN_CONVERTER = opencc.OpenCC("s2twp")  # 簡體/其他中文變體 -> 繁體中文（台灣用語）
except Exception:
    _CN_CONVERTER = None


def to_traditional(text):
    """不論模型輸出簡體、繁體或中英夾雜，都強制轉換成繁體中文（台灣用語）。"""
    if not text or _CN_CONVERTER is None:
        return text
    return _CN_CONVERTER.convert(text)


OLLAMA_URL = "http://localhost:11434"

REVIEW_MODELS = [
    "ornith:9b",
    "codellama:7b",
    "richardyoung/qwythos-9b-abliterated:Q8_0",
    "gemma4:e4b",
]

DEFAULT_INTEGRATOR = "qwen2.5:latest"

CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala",
    ".sql", ".sh", ".ps1", ".bat", ".json", ".yaml", ".yml", ".xml",
}

# 各語言註解規則
LINE_COMMENT_CHARS = {
    ".py": ["#"],
    ".rb": ["#"],
    ".sh": ["#"],
    ".ps1": ["#"],
    ".yaml": ["#"], ".yml": ["#"],
    ".js": ["//"], ".ts": ["//"], ".jsx": ["//"], ".tsx": ["//"],
    ".java": ["//"], ".c": ["//"], ".cpp": ["//"], ".h": ["//"], ".hpp": ["//"],
    ".cs": ["//"], ".go": ["//"], ".rs": ["//"], ".php": ["//"],
    ".swift": ["//"], ".kt": ["//"], ".scala": ["//"],
    ".sql": ["--"],
    ".bat": ["REM ", "::"],
}

BLOCK_COMMENT_PAIRS = {
    ".py": [('"""', '"""'), ("'''", "'''")],
    ".js": [("/*", "*/")], ".ts": [("/*", "*/")], ".jsx": [("/*", "*/")], ".tsx": [("/*", "*/")],
    ".java": [("/*", "*/")], ".c": [("/*", "*/")], ".cpp": [("/*", "*/")],
    ".h": [("/*", "*/")], ".hpp": [("/*", "*/")],
    ".cs": [("/*", "*/")], ".go": [("/*", "*/")], ".rs": [("/*", "*/")],
    ".php": [("/*", "*/")],
    ".swift": [("/*", "*/")], ".kt": [("/*", "*/")], ".scala": [("/*", "*/")],
    ".sql": [("/*", "*/")],
    ".ps1": [("<#", "#>")],
    ".xml": [("<!--", "-->")],
}



# ===== 新增：排除規則 =====

# 要跳過的資料夾名稱
SKIP_DIRS = {
    # 套件管理
    "node_modules", "bower_components",
    # Python
    "__pycache__", ".venv", "venv", "env", ".env",
    "site-packages", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", "eggs", "*.egg-info",
    # 版本控制
    ".git", ".svn", ".hg",
    # IDE / 編輯器
    ".idea", ".vscode", ".vs",
    # 打包產物
    "dist", "build", "out", "target", "bin", "obj",
    "Release", "Debug", "cmake-build-debug",
    # 前端
    ".next", ".nuxt", ".output", ".cache",
    "coverage", ".nyc_output", "storybook-static",
    # Go
    "vendor",
    # 其他
    ".terraform", ".serverless",
}

# 要跳過的檔案名稱（完全比對）
SKIP_FILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Pipfile.lock",
    "poetry.lock",
    "composer.lock",
    "Gemfile.lock",
    "Cargo.lock",
    "go.sum",
    ".DS_Store",
    "Thumbs.db",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    ".prettierrc",
    ".eslintrc",
    ".browserslistrc",
}

# 要跳過的副檔名
SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd",
    ".so", ".dll", ".dylib", ".exe",
    ".class", ".jar", ".war",
    ".min.js", ".min.css",
    ".map",           # source map
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".zip", ".tar", ".gz", ".rar", ".7z",
    ".mp3", ".mp4", ".wav", ".avi",
    ".db", ".sqlite", ".sqlite3",
    ".log",
    ".env",           # 可能含密碼
    ".lock",
    ".cache",
    ".txt",
    ".csv",
}

# 檔案大小上限（單檔超過此值直接跳過，通常是自動產生的）
MAX_SINGLE_FILE_SIZE = 100_000  # 100 KB

# 每批次字元上限（超過會拆到下一批）
BATCH_CHARS = 20_000


def should_skip_file(filepath):
    """判斷這個檔案是否應該跳過"""
    filename = os.path.basename(filepath)
    ext = os.path.splitext(filename)[1].lower()

    # 1. 檔名完全比對
    if filename in SKIP_FILES:
        return True

    # 2. 副檔名比對
    if ext in SKIP_EXTENSIONS:
        return True

    # 3. 特殊檔名模式
    if filename.endswith(".min.js") or filename.endswith(".min.css"):
        return True
    if filename.startswith("."):  # 隱藏檔（可選，視需求）
        # 但保留 .env.example 之類的
        if ext not in CODE_EXTENSIONS:
            return True

    # 4. 檔案大小
    try:
        if os.path.getsize(filepath) > MAX_SINGLE_FILE_SIZE:
            return True
    except OSError:
        return True

    # 5. 必須是我們認識的程式碼副檔名
    if ext not in CODE_EXTENSIONS:
        return True

    return False


def should_skip_dir(dirname):
    """判斷這個資料夾是否應該跳過"""
    # 完全比對
    if dirname in SKIP_DIRS:
        return True
    # 隱藏資料夾（以 . 開頭）
    if dirname.startswith("."):
        return True
    # 常見模式
    if dirname.endswith(".egg-info"):
        return True
    return False


REVIEW_SYSTEM_PROMPT = """你是一位資深軟體工程師，負責進行完整的程式碼審查。請提供具建設性且可執行的回饋。

## 審查重點

請針對所選的程式碼分析以下項目：

1. **安全性問題**
   - 輸入驗證與清理
   - 驗證與授權
   - 資料外洩風險
   - 注入攻擊漏洞

2. **效能與效率**
   - 演算法複雜度
   - 記憶體使用模式
   - 資料庫查詢最佳化
   - 不必要的運算

3. **程式碼品質**
   - 可讀性與可維護性
   - 適當的命名慣例
   - 函式／類別的大小與職責
   - 重複程式碼

4. **架構與設計**
   - 設計模式的使用
   - 關注點分離
   - 相依性管理
   - 錯誤處理策略

5. **測試與文件**
   - 測試涵蓋率與品質
   - 文件完整性
   - 註解的清楚程度與必要性

## 輸出格式

請以下列格式提供回饋：

**🔴 關鍵問題** - 合併前必須修正
**🟡 建議事項** - 可考慮進一步改善
**✅ 良好實踐** - 做得好的地方

針對每個問題，請提供：
- 具體的行號參考
- 問題的清楚說明
- 包含程式碼範例的建議解決方案
- 修改的原因與理由

重點關注：${input:focus:是否有任何需要特別強調的審查面向？}

請以具建設性且具有教育意義的方式提供回饋。 

請全程使用「繁體中文」（台灣用語習慣）撰寫，不要使用簡體字，也不要虛構程式碼中不存在的問題。"""

INTEGRATION_SYSTEM_PROMPT = """你是一位經驗豐富的技術主管，負責將多份 AI 審查報告整合成一份最終報告。你收到多位審查專家針對「同一份程式碼」各自獨立完成的 Code Review 報告與中間摘要，請將這些材料去蕪存菁，整合出最終審查報告。

## 整合重點

1. **去重合併** — 合併重複或相似的問題，只列一次，並標註共識模型數
2. **交叉驗證** — 過濾明顯錯誤或無根據的意見；模型意見衝突時判斷何者合理並說明原因
3. **重新判定嚴重程度** — 綜合所有審查者判斷，重新決定最終嚴重程度

## 輸出格式

審查摘要
問題清單（依嚴重程度分類，每項附上「共識模型數」）
🔴 嚴重問題

問題 1：【標題】（共識：X/4 個模型提及）

位置
問題
風險
建議

🟡 建議修改
🟢 可選優化
亮點
總結與最終決策

請全程使用「繁體中文」（台灣用語習慣）撰寫，不要使用簡體字，也不要輸出程式碼區塊。"""

INTEGRATION_PARTIAL_PROMPT = """你是一位經驗豐富的技術主管，負責產生 Code Review 的中間摘要。你收到的是一批審查意見（非完整報告），請將這批意見整理成簡潔摘要。

## 任務

整理出每個問題的：
- 簡短描述
- 所在檔案或位置
- 嚴重程度判斷

請勿輸出完整報告格式，只需條列重點。此摘要後續會與其他批次合併。

## 輸出格式

## 第 X 批審查摘要
### 🔴 關鍵問題
- 問題描述（位置）[嚴重程度]

### 🟡 建議事項
...

### 📌 其他觀察
...

請全程使用「繁體中文」（台灣用語習慣）撰寫，不要使用簡體字。"""


def get_installed_models():
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        resp.raise_for_status()
        return sorted(m["name"] for m in resp.json().get("models", []))
    except Exception:
        return []


MODEL_CONTEXT = {
    "ornith:9b": 8192,
    "codellama:7b": 16384,
    "richardyoung/qwythos-9b-abliterated:Q8_0": 32768,
    "gemma4:e4b": 32768,
}

MODEL_TIMEOUT = {
    "ornith:9b": 600,
    "codellama:7b": 600,
    "richardyoung/qwythos-9b-abliterated:Q8_0": 1800,
    "gemma4:e4b": 600,
}


def _get_num_ctx(model):
    return MODEL_CONTEXT.get(model, 16384)


def _get_timeout(model):
    return MODEL_TIMEOUT.get(model, 600)


def stream_chat(model, system_prompt, user_content, on_chunk, stop_event):
    """呼叫 ollama /api/chat streaming，把每個 chunk 丟給 on_chunk callback。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "options": {"num_ctx": _get_num_ctx(model), "num_predict": -1},
        "stream": True,
    }
    with requests.post(f"{OLLAMA_URL}/api/chat", json=payload, stream=True, timeout=_get_timeout(model)) as resp:
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text.strip()}")
        for line in resp.iter_lines():
            if stop_event.is_set():
                break
            if not line:
                continue
            data = json.loads(line)
            if "message" in data and data["message"].get("content"):
                on_chunk(data["message"]["content"])
            if data.get("done"):
                break


class CodeReviewApp:
    def __init__(self, root):
        self.root = root
        self.root.title("本地多模型 Code Review 工具")
        self.root.geometry("1100x750")

        self.selected_files = []
        self.review_results = {m: "" for m in REVIEW_MODELS}
        self.integration_result = ""
        self.worker_thread = None
        self.stop_event = threading.Event()
        self.ui_queue = queue.Queue()

        self._build_ui()
        self._poll_queue()

    # ---------- UI ----------
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Button(top, text="選擇資料夾", command=self.choose_folder).pack(side="left")
        ttk.Button(top, text="選擇檔案", command=self.choose_files).pack(side="left", padx=(6, 0))
        self.path_label = ttk.Label(top, text="尚未選擇", foreground="gray")
        self.path_label.pack(side="left", padx=10)

        mid = ttk.Frame(self.root, padding=(8, 0))
        mid.pack(fill="both", expand=False)

        list_frame = ttk.LabelFrame(mid, text="檔案清單（勾選要審查的檔案）")
        list_frame.pack(side="left", fill="both", expand=True)

        toolbar = ttk.Frame(list_frame)
        toolbar.pack(fill="x", padx=4, pady=(2, 0))
        ttk.Button(toolbar, text="全選", command=lambda: self._toggle_file_list(True)).pack(side="left")
        ttk.Button(toolbar, text="全取消", command=lambda: self._toggle_file_list(False)).pack(side="left", padx=(6, 0))

        self.file_listbox = tk.Listbox(list_frame, selectmode="multiple", height=6)
        self.file_listbox.pack(fill="both", expand=True, padx=4, pady=4)

        opt_frame = ttk.LabelFrame(mid, text="模型選擇")
        opt_frame.pack(side="left", fill="y", padx=(8, 0))

        self.model_vars = {}
        for m in REVIEW_MODELS:
            var = tk.BooleanVar(value=True)
            self.model_vars[m] = var
            ttk.Checkbutton(opt_frame, text=m, variable=var).pack(anchor="w", padx=4)

        ttk.Label(opt_frame, text="整合模型：").pack(anchor="w", padx=4, pady=(10, 0))
        installed = get_installed_models()
        default_integrator = DEFAULT_INTEGRATOR if DEFAULT_INTEGRATOR in installed else (installed[0] if installed else DEFAULT_INTEGRATOR)
        self.integrator_var = tk.StringVar(value=default_integrator)
        self.integrator_combo = ttk.Combobox(
            opt_frame, textvariable=self.integrator_var,
            values=installed or [DEFAULT_INTEGRATOR], width=32, state="readonly",
        )
        self.integrator_combo.pack(anchor="w", padx=4, pady=(0, 6))

        self.auto_integrate_var = tk.BooleanVar(value=True)
        self.auto_integrate_cb = ttk.Checkbutton(
            opt_frame, text="審查完成後自動整合", variable=self.auto_integrate_var,
        )
        self.auto_integrate_cb.pack(anchor="w", padx=4, pady=(0, 4))

        btn_frame = ttk.Frame(self.root, padding=8)
        btn_frame.pack(fill="x")
        self.run_btn = ttk.Button(btn_frame, text="開始審查（跑四個模型）", command=self.start_review)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self.stop_review, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.integrate_btn = ttk.Button(btn_frame, text="整合結果", command=self.start_integration, state="disabled")
        self.integrate_btn.pack(side="left", padx=(6, 0))
        self.save_btn = ttk.Button(btn_frame, text="另存全部結果", command=self.save_results, state="disabled")
        self.save_btn.pack(side="left", padx=(6, 0))
        self.export_csv_btn = ttk.Button(btn_frame, text="匯出 CSV", command=self._export_csv, state="disabled")
        self.export_csv_btn.pack(side="left", padx=(6, 0))

        self.status_label = ttk.Label(btn_frame, text="就緒")
        self.status_label.pack(side="left", padx=12)

        self.progress_bar = ttk.Progressbar(btn_frame, mode="determinate", length=120)
        self.progress_bar.pack(side="left", padx=(0, 8))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.text_widgets = {}
        for m in REVIEW_MODELS:
            frame = ttk.Frame(self.notebook)
            self.notebook.add(frame, text=m)
            txt = tk.Text(frame, wrap="word", font=("Consolas", 10))
            txt.pack(fill="both", expand=True)
            self.text_widgets[m] = txt

        integ_frame = ttk.Frame(self.notebook)
        self.notebook.add(integ_frame, text="★ 整合報告")
        self.integ_text = tk.Text(integ_frame, wrap="word", font=("Consolas", 10))
        self.integ_text.pack(fill="both", expand=True)

        self.issue_frame = ttk.Frame(self.notebook)
        self.issue_tree = ttk.Treeview(
            self.issue_frame,
            columns=("severity", "num", "title", "consensus", "location", "description", "risk", "suggestion"),
            show="headings", height=12,
        )
        col_defs = [
            ("severity", "嚴重程度", 110),
            ("num", "#", 40),
            ("title", "標題", 250),
            ("consensus", "共識", 60),
            ("location", "位置", 200),
            ("description", "問題描述", 250),
            ("risk", "風險", 200),
            ("suggestion", "建議修正", 250),
        ]
        for key, text, width in col_defs:
            self.issue_tree.heading(key, text=text)
            self.issue_tree.column(key, width=width, minwidth=40)
        self.issue_tree.pack(fill="both", expand=True, padx=4, pady=4)
        for col in ("severity", "num", "title", "consensus", "location", "description", "risk", "suggestion"):
            self.issue_tree.heading(
                col,
                command=lambda _c=col: self.treeview_sort_column(self.issue_tree, _c, False),
            )
        self.issue_tree.bind("<<TreeviewSelect>>", self._on_issue_select)

    def _set_files(self, files, label):
        self.selected_files = files
        self.path_label.config(text=label)
        self.file_listbox.delete(0, "end")
        for f in files:
            # 顯示相對路徑 + 檔案大小，方便使用者判斷
            size_kb = os.path.getsize(f) / 1024
            display = f"{f}  ({size_kb:.0f} KB)"
            self.file_listbox.insert("end", display)
        # 預設全選
        for i in range(len(files)):
            self.file_listbox.selection_set(i)

    # ---------- 檔案選擇 ----------
    def choose_folder(self):
        folder = filedialog.askdirectory(title="選擇要審查的資料夾")
        if not folder:
            return
        files = []
        for dirpath, dirnames, filenames in os.walk(folder):
            # ★ 關鍵：原地修改 dirnames，os.walk 就不會進入這些子目錄
            dirnames[:] = [
                d for d in dirnames
                if not should_skip_dir(d)
            ]

            for fn in filenames:
                full_path = os.path.join(dirpath, fn)
                if not should_skip_file(full_path):
                    files.append(full_path)

        # 排序讓結果可預期
        files.sort()
        self._set_files(files, f"{folder}（{len(files)} 個程式檔）")

    def choose_files(self):
        files = filedialog.askopenfilenames(title="選擇要審查的程式檔")
        if not files:
            return
        self._set_files(list(files), f"{len(files)} 個檔案")

    def _get_checked_files(self):
        idxs = self.file_listbox.curselection()
        return [self.selected_files[i] for i in idxs]

    def _toggle_file_list(self, select_all):
        if select_all:
            self.file_listbox.selection_set(0, "end")
        else:
            self.file_listbox.selection_clear(0, "end")

    # ---------- 審查流程 ----------
    def start_review(self):
        files = self._get_checked_files()
        if not files:
            messagebox.showwarning("提醒", "請先選擇並勾選至少一個檔案")
            return
        models = [m for m, v in self.model_vars.items() if v.get()]
        if not models:
            messagebox.showwarning("提醒", "請至少勾選一個模型")
            return
        installed = get_installed_models()
        models = [m for m in models if m in installed]
        if not models:
            messagebox.showwarning("提醒", "你選擇的模型在本機 Ollama 中都不存在，請先拉取模型")
            return
        missing = set(m for m, v in self.model_vars.items() if v.get()) - set(models)
        if missing:
            self.status_label.config(text=f"以下模型不存在，已略過：{', '.join(sorted(missing))}")

        for m in REVIEW_MODELS:
            self.text_widgets[m].delete("1.0", "end")
            self.review_results[m] = ""
        self.integ_text.delete("1.0", "end")
        self.integration_result = ""

        self.stop_event.clear()
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.integrate_btn.config(state="disabled")
        self.save_btn.config(state="disabled")
        self.export_csv_btn.config(state="disabled")

        self.progress_bar["value"] = 0
        self.integrator_combo.config(state="disabled")
        self.auto_integrate_cb.config(state="disabled")

        self.worker_thread = threading.Thread(
            target=self._review_worker, args=(files, models), daemon=True
        )
        self.worker_thread.start()

    @staticmethod
    def _strip_comments_and_docstrings(code, ext):
        """依副檔名移除單行註解與區塊註解/docstring，減少模型輸入噪音"""
        ext = ext.lower()
        line_chars = LINE_COMMENT_CHARS.get(ext, [])
        block_pairs = BLOCK_COMMENT_PAIRS.get(ext, [])

        # 1. 移除區塊註解（含 Python docstring）
        for start, end in block_pairs:
            if start == end:
                # Python docstring：用狀態機避免誤刪賦值語句內的字串
                in_block = False
                out = []
                for line in code.splitlines(True):
                    stripped = line.strip()
                    if not in_block:
                        if stripped.startswith(start):
                            rest = stripped[len(start):]
                            if start in rest:
                                continue
                            in_block = True
                            continue
                        out.append(line)
                    else:
                        if start in stripped:
                            idx = stripped.index(start)
                            rest = stripped[idx + len(start):].strip()
                            in_block = False
                            if rest:
                                out.append(line)
                        continue
                code = ''.join(out)
            else:
                code = re.sub(re.escape(start) + r'(?:.|\n)*?' + re.escape(end), '', code)

        # 2. 移除純註解行（整行都是註解）
        for char in line_chars:
            code = re.sub(r'^\s*' + re.escape(char) + r'.*$', '', code, flags=re.MULTILINE)

        # 3. 合併連續空白行
        code = re.sub(r'\n{3,}', '\n\n', code)
        return code.strip()

    def _prepare_batches(self, files):
        """把檔案按 BATCH_CHARS 分批（不截斷單檔內容），回傳 list[str]"""
        batches = []
        current_blocks = []
        current_size = 0

        for path in files:
            if self.stop_event.is_set():
                break
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception as e:
                content = f"(讀取失敗: {e})"

            ext = os.path.splitext(path)[1]
            content = self._strip_comments_and_docstrings(content, ext)
            block = f"### 檔案: {os.path.basename(path)}\n```\n{content}\n```"
            block_size = len(block)

            if current_size + block_size > BATCH_CHARS and current_blocks:
                batches.append((current_size, "\n\n".join(current_blocks)))
                current_blocks = []
                current_size = 0

            current_blocks.append(block)
            current_size += block_size

        if current_blocks:
            batches.append((current_size, "\n\n".join(current_blocks)))

        return batches

    def _review_worker(self, files, models):
        batches = self._prepare_batches(files)
        total_batches = len(batches)
        total_steps = len(models) * total_batches

        self.ui_queue.put(("setup_progress", total_steps))

        for model in models:
            if self.stop_event.is_set():
                break

            prev_output = ""  # 累積上一批的審查結果，作為下一批的上下文

            for batch_idx, (batch_size, batch_content) in enumerate(batches, 1):
                if self.stop_event.is_set():
                    break

                if batch_idx == 1:
                    user_content = f"進行 Code Review。找出這段程式碼中的所有 Bug、安全漏洞、效能問題、可維護性問題。直接從審查摘要開始，不要重複程式碼，不要打招呼，不要問問題。\n\n程式碼：\n\n{batch_content}"
                else:
                    user_content = f"繼續審查同一份程式的其餘部分。只看新程式碼中的問題，不要撰寫或補完程式碼，不要重複已說過的內容。直接列出新發現的問題。\n\n剩餘程式碼：\n\n{batch_content}"

                self.ui_queue.put(("status", f"{model} - 第 {batch_idx}/{total_batches} 批 ({batch_size} chars)"))

                if batch_idx > 1:
                    self.ui_queue.put(("append", model, f"\n\n--- 第 {batch_idx}/{total_batches} 批 ---\n\n"))

                buf = []

                def on_chunk(chunk, _buf=buf):
                    _buf.append(chunk)
                    self.ui_queue.put(("append", model, chunk))

                try:
                    stream_chat(model, REVIEW_SYSTEM_PROMPT, user_content, on_chunk, self.stop_event)
                except Exception as e:
                    self.ui_queue.put(("append", model, f"\n\n[錯誤] 呼叫模型失敗: {e}"))

                prev_output = "".join(buf)

                self.ui_queue.put(("batch_done",))

            self.ui_queue.put(("model_done", model))

        self.ui_queue.put(("all_done",))

    def stop_review(self):
        self.stop_event.set()
        self.progress_bar["value"] = 0
        self.integrator_combo.config(state="readonly")
        self.auto_integrate_cb.config(state="normal")
        self.export_csv_btn.config(state="disabled")
        self.status_label.config(text="已要求停止...")

    # ---------- 整合 ----------
    def start_integration(self):
        model = self.integrator_var.get()
        reviews = {m: self.review_results[m] for m in REVIEW_MODELS if self.review_results.get(m)}
        if not reviews:
            messagebox.showwarning("提醒", "目前沒有任何審查結果可以整合")
            return

        self.integrate_btn.config(state="disabled")
        self.stop_event.clear()
        self.integ_text.delete("1.0", "end")
        self.integration_result = ""
        self.notebook.select(len(REVIEW_MODELS))
        self.status_label.config(text=f"正在用 {model} 整合結果...")

        # 將各模型報告按 BATCH_CHARS 分組（不分割單份報告）
        parts = [f"【{m} 的審查意見】\n{txt}" for m, txt in reviews.items()]
        batches = []
        current = []
        current_size = 0
        for part in parts:
            if current_size + len(part) > BATCH_CHARS and current:
                batches.append(current)
                current = []
                current_size = 0
            current.append(part)
            current_size += len(part)
        if current:
            batches.append(current)

        total_batches = len(batches)

        self.progress_bar["maximum"] = total_batches
        self.progress_bar["value"] = 0

        def worker():
            partial_summaries = []

            for batch_idx, batch_parts in enumerate(batches):
                if self.stop_event.is_set():
                    break

                is_last = (batch_idx == total_batches - 1)

                if is_last:
                    # 最終批次：合併先前的中間摘要 + 這批原始報告
                    combined_parts = []
                    if partial_summaries:
                        combined_parts.append("【先前批次的中間摘要】\n" + "\n\n".join(partial_summaries))
                    combined_parts.append("【最後一批原始審查意見】\n" + "\n\n".join(batch_parts))
                    user_content = "將以下所有審查意見整合成一份最終報告，直接輸出整合報告，不要打招呼，不要重複原始內容。\n\n" \
                        "請嚴格按照以下格式輸出（這很重要）：\n\n" \
                        "🔴 嚴重問題\n\n" \
                        "問題 1：【問題標題】（共識：X/4 個模型提及）\n\n" \
                        "位置\n問題描述\n風險\n建議\n\n" \
                        "🟡 建議修改\n\n" \
                        "問題 2：【問題標題】（共識：X/4 個模型提及）\n\n" \
                        "位置\n問題描述\n風險\n建議\n\n" \
                        "🟢 可選優化\n\n" \
                        "...（依此類推）\n\n" \
                        "亮點\n\n" \
                        "總結與最終決策\n\n" + "\n\n".join(combined_parts)
                    prompt = INTEGRATION_SYSTEM_PROMPT
                    self.ui_queue.put(("status", "整合 - 最終合併階段"))
                else:
                    user_content = "整理以下審查意見，產生中間摘要。不要打招呼，不要重複原始內容。\n\n" + "\n\n".join(batch_parts)
                    prompt = INTEGRATION_PARTIAL_PROMPT
                    self.ui_queue.put(("status", f"整合 - 第 {batch_idx+1}/{total_batches} 批（中間摘要）"))

                # 收集完整回應
                buf = []

                def on_chunk(chunk, _final=is_last):
                    if _final:
                        self.ui_queue.put(("integ_append", chunk))
                    buf.append(chunk)

                try:
                    stream_chat(model, prompt, user_content, on_chunk, self.stop_event)
                except Exception as e:
                    if is_last:
                        self.ui_queue.put(("integ_append", f"\n\n[錯誤] 整合失敗: {e}"))

                if not is_last:
                    partial_summaries.append("".join(buf))
                elif not self.stop_event.is_set():
                    self.ui_queue.put(("integ_append", ""))

                self.ui_queue.put(("integ_batch_done",))

            self.ui_queue.put(("integ_done",))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 解析 / 匯出 ----------
    def _parse_issues_from_report(self, text):
        """從整合報告文字中解析出結構化問題清單"""
        if not text:
            return []
        severity_map = {
            "🔴 嚴重問題": "🔴 嚴重 (Blocking)",
            "🟡 建議修改": "🟡 建議 (Important)",
            "🟢 可選優化": "🟢 可選 (Nit)",
            "💡 替代方案": "💡 替代方案 (Suggestion)",
            "📚 知識分享": "📚 知識分享／教育性建議 (Learning)",
            "🎉 值得讚賞": "🎉 值得讚賞 (Praise)",
        }
        # 接受 markdown 標題（### 🔴 嚴重問題）以及無格式兩種
        def _match_severity(s):
            raw = s.lstrip("#").strip()
            return severity_map.get(raw)
        section_map = {
            "位置": "location",
            "問題": "description",
            "風險": "risk",
            "建議": "suggestion",
        }
        results = []
        current_severity = None
        current_issue = None
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            sev = _match_severity(stripped)
            if sev:
                current_severity = sev
                continue
            # 問題 N：【標題】（共識：X/Y 個模型提及） — 【】可省略，：或 : 皆可
            m = re.match(r"問題\s*(\d+)\s*[：:]\s*", stripped)
            if m:
                rest = stripped[m.end():]
                cm = re.search(r"（共識：(\d+/\d+)\s*個模型提及）", rest)
                consensus = cm.group(1) if cm else ""
                if cm:
                    rest = rest[:cm.start()].strip()
                # 嘗試取 【】中的標題，失敗則取整段文字
                tm = re.match(r"[【\[]?(.+?)[】\]]?\s*$", rest)
                title = tm.group(1).strip() if tm else rest.strip()
                if title:
                    if current_issue:
                        results.append(current_issue)
                    current_issue = {
                        "severity": current_severity or "",
                        "number": m.group(1),
                        "title": title,
                        "consensus": consensus,
                        "location": "",
                        "description": "",
                        "risk": "",
                        "suggestion": "",
                    }
                continue
            if current_issue is not None:
                if stripped in section_map:
                    current_issue["_sec"] = section_map[stripped]
                    continue
                else:
                    # content line under current section
                    sec = current_issue.get("_sec")
                    if sec:
                        if current_issue[sec]:
                            current_issue[sec] += " "
                        current_issue[sec] += stripped
        if current_issue:
            results.append(current_issue)
        for r in results:
            r.pop("_sec", None)
        return results

    def _export_csv(self):
        rows = self._parse_issues_from_report(self.integration_result)
        if not rows:
            messagebox.showinfo("提示", "整合報告中未解析到結構化問題清單")
            return
        import csv
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile=f"codereview_issues_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=[
                "severity", "number", "title", "consensus",
                "location", "description", "risk", "suggestion",
            ])
            w.writeheader()
            w.writerows(rows)
        messagebox.showinfo("完成", f"已匯出 CSV 至：\n{path}")

    def _populate_issue_table(self):
        """解析整合報告並填入問題清單 Treeview"""
        for item in self.issue_tree.get_children():
            self.issue_tree.delete(item)
        rows = self._parse_issues_from_report(self.integration_result)
        if rows:
            for r in rows:
                self.issue_tree.insert("", "end", values=(
                    r["severity"], r["number"], r["title"], r["consensus"],
                    r["location"], r["description"], r["risk"], r["suggestion"],
                ))
            self.notebook.add(self.issue_frame)
        else:
            try:
                self.notebook.hide(self.issue_frame)
            except tk.TclError:
                pass

    def treeview_sort_column(self, tv, col, reverse):
        l = [(tv.set(k, col), k) for k in tv.get_children("")]
        l.sort(key=lambda x: (x[0] or ""), reverse=reverse)
        for index, (_, k) in enumerate(l):
            tv.move(k, "", index)
        tv.heading(col, command=lambda: self.treeview_sort_column(tv, col, not reverse))

    def _on_issue_select(self, event):
        sel = self.issue_tree.selection()
        if not sel:
            return
        values = self.issue_tree.item(sel[0], "values")
        if len(values) < 2:
            return
        num = values[1]
        tag = f"問題 {num}："
        pos = self.integ_text.search(tag, "1.0", tk.END)
        if pos:
            self.notebook.select(len(REVIEW_MODELS) + 1)  # integ report tab index
            self.integ_text.see(pos)

    # ---------- 儲存 ----------
    def save_results(self):
        folder = filedialog.askdirectory(title="選擇儲存結果的資料夾")
        if not folder:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(folder, f"code_review_{ts}")
        os.makedirs(out_dir, exist_ok=True)
        for m, text in self.review_results.items():
            if not text:
                continue
            fname = m.replace("/", "_").replace(":", "_") + ".txt"
            with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
                f.write(text)
        if self.integration_result:
            with open(os.path.join(out_dir, "整合報告.txt"), "w", encoding="utf-8") as f:
                f.write(self.integration_result)
        messagebox.showinfo("完成", f"已儲存至：\n{out_dir}")

    # ---------- 主執行緒輪詢佇列 ----------
    @staticmethod
    def _is_at_bottom(txt):
        """檢查 Text widget 的捲軸是否在底部"""
        try:
            return txt.yview()[1] >= 0.99
        except Exception:
            return True

    def _poll_queue(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]
                if kind == "status":
                    self.status_label.config(text=item[1])
                elif kind == "append":
                    _, model, chunk = item
                    self.review_results[model] += chunk
                    self.text_widgets[model].insert("end", chunk)
                    if self._is_at_bottom(self.text_widgets[model]):
                        self.text_widgets[model].see("end")
                elif kind == "setup_progress":
                    self.progress_bar["maximum"] = item[1]
                    self.progress_bar["value"] = 0
                elif kind == "batch_done":
                    self.progress_bar.step(1)
                    done = int(self.progress_bar["value"])
                    total = int(self.progress_bar["maximum"])
                    self.status_label.config(text=f"已處理 {done}/{total} 批")
                elif kind == "model_done":
                    _, model = item
                    converted = to_traditional(self.review_results[model])
                    if converted != self.review_results[model]:
                        self.review_results[model] = converted
                        self.text_widgets[model].delete("1.0", "end")
                        self.text_widgets[model].insert("end", converted)
                    idx = REVIEW_MODELS.index(model)
                    self.notebook.tab(idx, text=f"✔ {model}")
                elif kind == "all_done":
                    self.run_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.integrator_combo.config(state="readonly")
                    self.auto_integrate_cb.config(state="normal")
                    self.save_btn.config(state="normal")
                    if self.auto_integrate_var.get():
                        self.status_label.config(text="所有模型審查完成，正在整合...")
                        self.start_integration()
                    else:
                        self.integrate_btn.config(state="normal")
                        self.status_label.config(text="所有模型審查完成，可以按「整合結果」")
                elif kind == "integ_append":
                    self.integration_result += item[1]
                    self.integ_text.insert("end", item[1])
                    if self._is_at_bottom(self.integ_text):
                        self.integ_text.see("end")
                elif kind == "integ_batch_done":
                    self.progress_bar.step(1)
                    done = int(self.progress_bar["value"])
                    total = int(self.progress_bar["maximum"])
                    self.status_label.config(text=f"整合中 {done}/{total} 批")
                elif kind == "integ_done":
                    self.integrator_combo.config(state="readonly")
                    self.auto_integrate_cb.config(state="normal")
                    converted = to_traditional(self.integration_result)
                    if converted != self.integration_result:
                        self.integration_result = converted
                        self.integ_text.delete("1.0", "end")
                        self.integ_text.insert("end", converted)
                    self.integrate_btn.config(state="normal")
                    self.save_btn.config(state="normal")
                    self.export_csv_btn.config(state="normal")
                    self._populate_issue_table()
                    self.status_label.config(text="整合報告完成")
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)


def main():
    root = tk.Tk()
    app = CodeReviewApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
