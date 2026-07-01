"""
gui_code_review.py
多模型交叉 Code Review 工具（含圖形介面）
需求：pip install requests
"""

import os
import re
import json
import queue
import statistics
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import requests


# ============================================================
# 設定
# ============================================================

OLLAMA_URL = "http://localhost:11434/api/generate"

MODELS = [
    "ornith:9b",
    "codellama:7b",
    "richardyoung/qwythos-9b-abliterated:Q8_0",
    "gemma4:e4b",
]

JUDGE_MODEL = "gemma4:e4b"
TIMEOUT = 300


# ============================================================
# 資料結構
# ============================================================

@dataclass
class ReviewResult:
    model: str
    raw_text: str
    scores: dict = field(default_factory=dict)
    advantages: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)
    parse_ok: bool = False


@dataclass
class FileReviewData:
    file_path: str
    code: str
    results: list = field(default_factory=list)      # list[ReviewResult]
    score_summary: dict = field(default_factory=dict)
    common_issues: list = field(default_factory=list)
    final_report: str = ""
    error: Optional[str] = None


# ============================================================
# 核心邏輯（呼叫 Ollama / 解析 / 統計）
# ============================================================

def call_ollama(model: str, prompt: str) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2},
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()["response"]


def build_review_prompt(code: str) -> str:
    return f"""你是一位資深軟體工程師，負責 Code Review。

請閱讀以下程式碼，並「只回傳」一個 JSON，不要有其他文字、不要用 Markdown 包裹。

JSON 格式如下：

{{
  "bug_score": 0-10,
  "security_score": 0-10,
  "performance_score": 0-10,
  "readability_score": 0-10,
  "maintainability_score": 0-10,
  "overall_score": 0-10,
  "advantages": ["優點1", "優點2"],
  "issues": ["問題1", "問題2"],
  "suggestions": ["建議1", "建議2"]
}}

請針對以下面向評估：Bug／邏輯錯誤、資訊安全、效能、可讀性、可維護性。

程式碼如下：

{code}
"""

def extract_json(text: str) -> Optional[dict]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    json_str = match.group(0)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        fixed = re.sub(r",\s*}", "}", json_str)
        fixed = re.sub(r",\s*]", "]", fixed)
        try:
            return json.loads(fixed)
        except Exception:
            return None


def review_with_model(model: str, code: str) -> ReviewResult:
    prompt = build_review_prompt(code)
    try:
        raw = call_ollama(model, prompt)
    except Exception as e:
        return ReviewResult(model=model, raw_text=f"[ERROR] {e}")

    result = ReviewResult(model=model, raw_text=raw)
    data = extract_json(raw)
    if data is None:
        return result

    try:
        result.scores = {
            "bug": float(data.get("bug_score", 0)),
            "security": float(data.get("security_score", 0)),
            "performance": float(data.get("performance_score", 0)),
            "readability": float(data.get("readability_score", 0)),
            "maintainability": float(data.get("maintainability_score", 0)),
            "overall": float(data.get("overall_score", 0)),
        }
        result.advantages = data.get("advantages", []) or []
        result.issues = data.get("issues", []) or []
        result.suggestions = data.get("suggestions", []) or []
        result.parse_ok = True
    except Exception:
        result.parse_ok = False

    return result


def analyze_scores(results: list) -> dict:
    valid = [r for r in results if r.parse_ok]
    if not valid:
        return {}

    dims = ["bug", "security", "performance",
            "readability", "maintainability", "overall"]
    summary = {}
    for dim in dims:
        values = [r.scores[dim] for r in valid]
        summary[dim] = {
            "avg": round(statistics.mean(values), 2),
            "min": min(values),
            "max": max(values),
            "stdev": round(statistics.pstdev(values), 2) if len(values) > 1 else 0,
            "detail": {r.model: r.scores[dim] for r in valid},
        }
    return summary


def find_common_issues(results: list, min_count: int = 2) -> list:
    freq = {}
    for r in results:
        for issue in r.issues:
            key = issue.strip()
            freq[key] = freq.get(key, 0) + 1
    return [k for k, v in freq.items() if v >= min_count]


def build_consensus_prompt(code: str, results: list, score_summary: dict) -> str:
    reviews_text = ""
    for r in results:
        reviews_text += f"\n----- 模型：{r.model} -----\n"
        if r.parse_ok:
            reviews_text += f"分數：{r.scores}\n"
            reviews_text += f"優點：{r.advantages}\n"
            reviews_text += f"問題：{r.issues}\n"
            reviews_text += f"建議：{r.suggestions}\n"
        else:
            reviews_text += f"(JSON 解析失敗，原始回覆)\n{r.raw_text}\n"

    score_text = json.dumps(score_summary, ensure_ascii=False, indent=2)

    return f"""你是資深技術主管，收到四位工程師針對同一份程式碼的 Code Review 結果。

請「全部使用繁體中文」完成：

1. 統整共同意見
2. 指出分歧較大之處與可能原因
3. 依分數統計評估整體品質
4. 給出具體可執行的改進建議，適當附上修改後程式碼片段
5. 最後用條列式整理：

   ### 優點
   ### 主要問題
   ### 改進建議（依優先順序）
   ### 综合結論與建議分數（0-10）

【原始程式碼】

【四位審查者意見】
{reviews_text}

【分數統計摘要】
{score_text}
"""


def generate_final_report(code: str, results: list) -> str:
    score_summary = analyze_scores(results)
    prompt = build_consensus_prompt(code, results, score_summary)
    return call_ollama(JUDGE_MODEL, prompt)


def review_single_file(file_path: str, log_fn) -> FileReviewData:
    """審查單一檔案，log_fn(msg) 用來回報進度"""
    data = FileReviewData(file_path=file_path, code="")

    try:
        code = Path(file_path).read_text(encoding="utf-8", errors="ignore")
        data.code = code
    except Exception as e:
        data.error = f"讀取檔案失敗：{e}"
        return data

    results = []
    for model in MODELS:
        log_fn(f"  ▶ [{Path(file_path).name}] 使用 {model} 審查中...")
        r = review_with_model(model, code)
        status = "成功" if r.parse_ok else "解析失敗(將以原文呈現)"
        log_fn(f"    - {model} 完成（{status}）")
        results.append(r)

    data.results = results
    data.score_summary = analyze_scores(results)
    data.common_issues = find_common_issues(results)

    log_fn(f"  ▶ [{Path(file_path).name}] 交由 {JUDGE_MODEL} 統整最終報告...")
    try:
        data.final_report = generate_final_report(code, results)
    except Exception as e:
        data.final_report = f"[統整失敗] {e}"

    log_fn(f"  ✅ [{Path(file_path).name}] 審查完成")
    return data


# ============================================================
# GUI 主程式
# ============================================================

class CodeReviewGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("多模型交叉 Code Review 工具")
        self.root.geometry("1200x750")

        self.folder_path = tk.StringVar(value="尚未選擇資料夾")
        self.include_sub = tk.BooleanVar(value=False)

        self.msg_queue = queue.Queue()
        self.file_data = {}          # file_path -> FileReviewData
        self.tree_item_map = {}      # tree item id -> file_path
        self.is_running = False

        self._build_ui()
        self._poll_queue()

    # --------------------------------------------------
    # UI 建構
    # --------------------------------------------------

    def _build_ui(self):
        # 上方：資料夾選擇
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Button(top, text="選擇資料夾", command=self.select_folder).pack(side="left")
        ttk.Label(top, textvariable=self.folder_path).pack(side="left", padx=8)
        ttk.Checkbutton(top, text="包含子資料夾", variable=self.include_sub,
                         command=self.rescan_files).pack(side="left", padx=8)

        # 中間：左右分割
        main = ttk.PanedWindow(self.root, orient="horizontal")
        main.pack(fill="both", expand=True, padx=8, pady=8)

        # 左側：檔案清單
        left = ttk.Frame(main, width=300)
        main.add(left, weight=1)

        ttk.Label(left, text="Python 檔案清單（可多選）").pack(anchor="w")

        list_frame = ttk.Frame(left)
        list_frame.pack(fill="both", expand=True)

        self.file_listbox = tk.Listbox(list_frame, selectmode="extended")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical",
                                   command=self.file_listbox.yview)
        self.file_listbox.configure(yscrollcommand=scrollbar.set)
        self.file_listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        btn_frame = ttk.Frame(left)
        btn_frame.pack(fill="x", pady=4)
        ttk.Button(btn_frame, text="全選", command=self.select_all_files).pack(side="left")
        ttk.Button(btn_frame, text="取消全選", command=self.deselect_all_files).pack(side="left", padx=4)

        self.start_btn = ttk.Button(left, text="開始審查", command=self.start_review)
        self.start_btn.pack(fill="x", pady=4)

        self.progress = ttk.Progressbar(left, mode="indeterminate")
        self.progress.pack(fill="x", pady=2)

        ttk.Label(left, text="審查結果一覽：").pack(anchor="w", pady=(10, 0))

        self.tree = ttk.Treeview(
            left, columns=("overall", "status"), show="headings", height=10
        )
        self.tree.heading("overall", text="平均分")
        self.tree.heading("status", text="狀態")
        self.tree.column("overall", width=70, anchor="center")
        self.tree.column("status", width=80, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        ttk.Button(left, text="匯出全部報告 (Markdown)",
                   command=self.export_reports).pack(fill="x", pady=4)

        # 右側：結果 Notebook
        right = ttk.Frame(main)
        main.add(right, weight=3)

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True)

        # Tab1: 進度 Log
        self.log_text = scrolledtext.ScrolledText(self.notebook, wrap="word")
        self.notebook.add(self.log_text, text="進度 Log")

        # Tab2: 分數明細
        score_frame = ttk.Frame(self.notebook)
        self.notebook.add(score_frame, text="分數明細")

        self.score_tree = ttk.Treeview(
            score_frame,
            columns=("model", "bug", "security", "performance",
                     "readability", "maintainability", "overall"),
            show="headings", height=8,
        )
        headers = {
            "model": "模型", "bug": "Bug", "security": "安全性",
            "performance": "效能", "readability": "可讀性",
            "maintainability": "可維護性", "overall": "整體",
        }
        for col, text in headers.items():
            self.score_tree.heading(col, text=text)
            self.score_tree.column(col, width=100, anchor="center")
        self.score_tree.pack(fill="x", padx=4, pady=4)

        self.score_summary_text = scrolledtext.ScrolledText(score_frame, height=10, wrap="word")
        self.score_summary_text.pack(fill="both", expand=True, padx=4, pady=4)

        # Tab3: 最終中文報告
        self.report_text = scrolledtext.ScrolledText(self.notebook, wrap="word")
        self.notebook.add(self.report_text, text="最終改進報告（繁中）")

        # Tab4: 各模型原始回覆
        self.raw_text = scrolledtext.ScrolledText(self.notebook, wrap="word")
        self.notebook.add(self.raw_text, text="各模型原始回覆")

    # --------------------------------------------------
    # 檔案選擇 / 掃描
    # --------------------------------------------------

    def select_folder(self):
        folder = filedialog.askdirectory()
        if not folder:
            return
        self.folder_path.set(folder)
        self.rescan_files()

    def rescan_files(self):
        folder = self.folder_path.get()
        if not os.path.isdir(folder):
            return

        self.file_listbox.delete(0, "end")
        pattern = "**/*.py" if self.include_sub.get() else "*.py"

        files = sorted(Path(folder).glob(pattern))
        self.current_files = [str(f) for f in files]

        for f in self.current_files:
            display = os.path.relpath(f, folder)
            self.file_listbox.insert("end", display)

        self._log(f"掃描到 {len(self.current_files)} 個 .py 檔案")

    def select_all_files(self):
        self.file_listbox.select_set(0, "end")

    def deselect_all_files(self):
        self.file_listbox.selection_clear(0, "end")

    def get_selected_files(self):
        indices = self.file_listbox.curselection()
        return [self.current_files[i] for i in indices]

    # --------------------------------------------------
    # 開始審查（背景執行緒）
    # --------------------------------------------------

    def start_review(self):
        if self.is_running:
            messagebox.showinfo("提示", "目前已有審查工作在執行中")
            return

        selected = self.get_selected_files()
        if not selected:
            messagebox.showwarning("提示", "請至少選擇一個檔案")
            return

        self.is_running = True
        self.start_btn.config(state="disabled")
        self.progress.start(10)
        self.log_text.delete("1.0", "end")
        self.tree.delete(*self.tree.get_children())
        self.tree_item_map.clear()
        self.file_data.clear()

        self._log(f"開始審查 {len(selected)} 個檔案，使用模型：{', '.join(MODELS)}\n")

        thread = threading.Thread(
            target=self._run_batch, args=(selected,), daemon=True
        )
        thread.start()

    def _run_batch(self, files):
        for f in files:
            self.msg_queue.put(("log", f"開始審查：{f}"))
            try:
                data = review_single_file(f, log_fn=lambda m: self.msg_queue.put(("log", m)))
            except Exception as e:
                data = FileReviewData(file_path=f, code="", error=str(e))
                self.msg_queue.put(("log", f"❌ 發生例外：{e}"))

            self.msg_queue.put(("file_done", data))

        self.msg_queue.put(("all_done", None))

    # --------------------------------------------------
    # Queue 輪詢，安全更新 UI
    # --------------------------------------------------

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()

                if kind == "log":
                    self._log(payload)

                elif kind == "file_done":
                    self._on_file_done(payload)

                elif kind == "all_done":
                    self._on_all_done()

        except queue.Empty:
            pass

        self.root.after(100, self._poll_queue)

    def _log(self, msg: str):
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def _on_file_done(self, data: FileReviewData):
        self.file_data[data.file_path] = data

        overall = "-"
        status = "失敗" if data.error else "完成"
        if data.score_summary:
            overall = data.score_summary.get("overall", {}).get("avg", "-")

        display_name = os.path.relpath(
            data.file_path, self.folder_path.get()
        )

        item_id = self.tree.insert("", "end", text=display_name,
                                    values=(overall, status))
        self.tree.set(item_id, "overall", overall)
        self.tree.item(item_id, text=display_name)
        self.tree_item_map[item_id] = data.file_path

        # Treeview 第一欄用 #0 顯示檔名
        self.tree.heading("#0", text="檔案")

    def _on_all_done(self):
        self.is_running = False
        self.start_btn.config(state="normal")
        self.progress.stop()
        self._log("\n🎉 全部審查完成！點選左側清單可查看各檔案詳細結果。")

    # --------------------------------------------------
    # 顯示選取檔案的結果
    # --------------------------------------------------

    def on_tree_select(self, event):
        selection = self.tree.selection()
        if not selection:
            return

        item_id = selection[0]
        file_path = self.tree_item_map.get(item_id)
        data = self.file_data.get(file_path)
        if not data:
            return

        self._show_score_detail(data)
        self._show_final_report(data)
        self._show_raw_text(data)

    def _show_score_detail(self, data: FileReviewData):
        self.score_tree.delete(*self.score_tree.get_children())

        for r in data.results:
            if r.parse_ok:
                self.score_tree.insert("", "end", values=(
                    r.model,
                    r.scores.get("bug", "-"),
                    r.scores.get("security", "-"),
                    r.scores.get("performance", "-"),
                    r.scores.get("readability", "-"),
                    r.scores.get("maintainability", "-"),
                    r.scores.get("overall", "-"),
                ))
            else:
                self.score_tree.insert("", "end", values=(
                    r.model, "解析失敗", "-", "-", "-", "-", "-"
                ))

        self.score_summary_text.delete("1.0", "end")

        if data.score_summary:
            self.score_summary_text.insert("end", "=== 各面向統計（平均 / 最小 / 最大 / 標準差）===\n\n")
            for dim, stat in data.score_summary.items():
                self.score_summary_text.insert(
                    "end",
                    f"{dim:16s} 平均:{stat['avg']:<6} 最小:{stat['min']:<6} "
                    f"最大:{stat['max']:<6} 標準差:{stat['stdev']:<6}\n"
                )

        if data.common_issues:
            self.score_summary_text.insert("end", "\n=== 多位模型共同提出的問題 ===\n")
            for issue in data.common_issues:
                self.score_summary_text.insert("end", f" - {issue}\n")

        if data.error:
            self.score_summary_text.insert("end", f"\n[錯誤] {data.error}\n")

    def _show_final_report(self, data: FileReviewData):
        self.report_text.delete("1.0", "end")
        if data.final_report:
            self.report_text.insert("end", data.final_report)
        else:
            self.report_text.insert("end", "(尚無最終報告，可能發生錯誤)")

    def _show_raw_text(self, data: FileReviewData):
        self.raw_text.delete("1.0", "end")
        for r in data.results:
            self.raw_text.insert("end", f"\n===== 模型：{r.model} =====\n")
            self.raw_text.insert("end", r.raw_text + "\n")

    # --------------------------------------------------
    # 匯出報告
    # --------------------------------------------------

    def export_reports(self):
        if not self.file_data:
            messagebox.showinfo("提示", "目前沒有可匯出的審查結果")
            return

        out_dir = filedialog.askdirectory(title="選擇輸出資料夾")
        if not out_dir:
            return

        for file_path, data in self.file_data.items():
            name = Path(file_path).stem
            out_path = Path(out_dir) / f"{name}_review.md"

            with open(out_path, "w", encoding="utf-8") as f:
                f.write(f"# Code Review 報告\n\n檔案：`{file_path}`\n\n")

                if data.error:
                    f.write(f"⚠️ 錯誤：{data.error}\n\n")

                f.write("## 分數統計\n\n")
                f.write("| 面向 | 平均 | 最小 | 最大 | 標準差 |\n")
                f.write("|------|------|------|------|--------|\n")
                for dim, stat in data.score_summary.items():
                    f.write(f"| {dim} | {stat['avg']} | {stat['min']} | "
                            f"{stat['max']} | {stat['stdev']} |\n")

                f.write("\n## 最終統整報告（繁體中文）\n\n")
                f.write(data.final_report or "(無)")

                f.write("\n\n## 各模型原始回覆\n\n")
                for r in data.results:
                    f.write(f"### {r.model}\n\n```\n{r.raw_text}\n```\n\n")

        messagebox.showinfo("完成", f"已匯出 {len(self.file_data)} 份報告至：\n{out_dir}")


# ============================================================
# 程式入口
# ============================================================

def main():
    root = tk.Tk()
    app = CodeReviewGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
