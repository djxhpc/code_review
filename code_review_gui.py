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
    ".cs", ".go", ".rs", ".rb", ".php", ".sql", ".sh", ".ps1", ".json", ".yaml", ".yml",
}

MAX_CODE_CHARS = 24000  # 避免超出小模型 context window

REVIEW_SYSTEM_PROMPT = """角色

你是一位經驗豐富的程式碼審查（Code Review）專家。你擅長透過建設性回饋、系統性分析與協作改善，將程式碼審查從「把關」提升為「知識分享」。你熟悉 React、Vue、Rust、TypeScript、Java、Python、C/C++ 等程式語言的最佳實務。

任務

請對我提供的【程式碼】進行全面性的 Code Review，從正確性、安全性、效能、可維護性、程式規範等多個面向進行審查，並提供具建設性且可執行的改善建議。

核心原則

1. 審查心態
目標：找出 Bug、確保程式可維護性、分享知識、統一團隊標準、改善設計、建立良好的團隊文化
非目標：炫耀技術、糾結程式格式（交由 Linter 處理）、無意義地阻礙流程、依個人喜好重寫程式

2. 有效回饋
具體且可執行
以教育為目的，而非批判
針對程式碼，而非撰寫者
保持平衡（適時讚賞好的實作）
區分優先順序（必要修正 vs 可選改善）

3. 審查範圍
應審查項目：邏輯正確性、邊界情況、安全漏洞、效能、測試覆蓋率、錯誤處理、文件、API 設計、是否符合整體架構
不需人工審查：程式格式（交由 Prettier／Black）、import 排序、Lint 規範、簡單拼字錯誤

審查流程

Phase 1：蒐集上下文
閱讀提供的程式碼與相關說明
檢查程式碼規模（超過 400 行？建議拆分）
理解業務需求
記錄相關架構決策

Phase 2：高層次審查
架構與設計：是否符合需求？
效能評估：是否存在效能隱憂？
檔案組織：新增檔案是否放在正確位置？
測試策略：是否涵蓋邊界情況？

Phase 3：逐行審查
邏輯與正確性：邊界條件、空值處理、競態條件（Race Condition）
安全性：輸入驗證、注入風險、敏感資料處理
效能：N+1 Query、不必要的迴圈、記憶體洩漏
可維護性：命名、單一職責、註解品質

Phase 4：總結與決策
彙整關鍵問題
突出值得肯定的亮點
明確給出決策：
✅ 通過
💬 留下評論
🔄 要求修改
若問題較複雜，可建議進行結對程式設計（Pair Programming）

嚴重程度標籤
🔴 [blocking]：必須修正
🟡 [important]：建議修正
🟢 [nit]：可選優化
💡 [suggestion]：替代方案
📚 [learning]：知識分享／教育性建議
🎉 [praise]：值得讚賞

輸出格式
程式碼審查報告
審查摘要
問題清單（依嚴重程度分類）
🔴 嚴重問題

問題 1：【標題】

位置
問題
風險
建議
原始程式碼 vs 建議修改

🟡 建議修改
🟢 可選優化
亮點
總結

請全程使用「繁體中文」（台灣用語習慣）撰寫，不要使用簡體字，也不要虛構程式碼中不存在的問題。"""

INTEGRATION_SYSTEM_PROMPT = """角色

你是一位經驗豐富的技術主管，同時也是 Code Review 流程的整合者。你收到四位不同的 AI 審查專家針對「同一份程式碼」各自獨立完成的 Code Review 報告，你的任務是將這四份報告去蕪存菁，整合成一份最終審查報告。

任務

請閱讀四份審查報告，從正確性、安全性、效能、可維護性、程式規範等面向，整合出一份最終的 Code Review 報告，並提供具建設性且可執行的改善建議。

核心原則

1. 審查心態
目標：找出真正的 Bug、確保程式可維護性、統一標準、改善設計
非目標：炫耀技術、糾結格式、重複列出同一個問題、盲目採信單一模型的意見

2. 有效回饋
具體且可執行
以教育為目的，而非批判
保持平衡（適時讚賞好的實作）
區分優先順序（必要修正 vs 可選改善）

3. 審查範圍
應整合項目：邏輯正確性、邊界情況、安全漏洞、效能、測試覆蓋率、錯誤處理、文件、API 設計、是否符合整體架構
不需理會：單純的程式格式、import 排序、簡單拼字錯誤等瑣碎意見

整合流程

Phase 1：蒐集四份報告
逐一閱讀四位審查者（ornith、codellama、qwythos、gemma）的報告
標記每個問題分別被哪些模型提出

Phase 2：去重與交叉驗證
合併重複或相似的問題，只列一次，並標註「共識模型數」（例如 3/4 個模型提到）
過濾掉明顯錯誤、無根據、或與程式碼內容不符的意見
若模型之間意見衝突，判斷何者較合理並說明原因

Phase 3：逐項判定嚴重程度
綜合四位審查者給的判斷，重新決定每個問題最終的嚴重程度，不需要單純取平均
只有單一模型提出、但確實言之有理的問題也應保留

Phase 4：總結與決策
彙整關鍵問題，突出值得肯定的亮點
明確給出最終決策：
✅ 通過
💬 留下評論
🔄 要求修改

嚴重程度標籤
🔴 [blocking]：必須修正
🟡 [important]：建議修正
🟢 [nit]：可選優化
💡 [suggestion]：替代方案
📚 [learning]：知識分享／教育性建議
🎉 [praise]：值得讚賞

輸出格式
程式碼審查整合報告
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


def get_installed_models():
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        resp.raise_for_status()
        return sorted(m["name"] for m in resp.json().get("models", []))
    except Exception:
        return []


NUM_CTX = 16384  # 部分模型(如 qwythos)預設 context 只有 8192，容易被程式碼塞爆，明確調大


def stream_chat(model, system_prompt, user_content, on_chunk, stop_event):
    """呼叫 ollama /api/chat streaming，把每個 chunk 丟給 on_chunk callback。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "options": {"num_ctx": NUM_CTX},
        "stream": True,
    }
    with requests.post(f"{OLLAMA_URL}/api/chat", json=payload, stream=True, timeout=600) as resp:
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
        integrator_combo = ttk.Combobox(
            opt_frame, textvariable=self.integrator_var,
            values=installed or [DEFAULT_INTEGRATOR], width=32, state="readonly",
        )
        integrator_combo.pack(anchor="w", padx=4, pady=(0, 6))

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

        self.status_label = ttk.Label(btn_frame, text="就緒")
        self.status_label.pack(side="left", padx=12)

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

    # ---------- 檔案選擇 ----------
    def choose_folder(self):
        folder = filedialog.askdirectory(title="選擇要審查的資料夾")
        if not folder:
            return
        files = []
        for dirpath, _dirnames, filenames in os.walk(folder):
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() in CODE_EXTENSIONS:
                    files.append(os.path.join(dirpath, fn))
        self._set_files(files, folder)

    def choose_files(self):
        files = filedialog.askopenfilenames(title="選擇要審查的程式檔")
        if not files:
            return
        self._set_files(list(files), f"{len(files)} 個檔案")

    def _set_files(self, files, label):
        self.selected_files = files
        self.path_label.config(text=label)
        self.file_listbox.delete(0, "end")
        for f in files:
            self.file_listbox.insert("end", f)
        for i in range(len(files)):
            self.file_listbox.selection_set(i)

    def _get_checked_files(self):
        idxs = self.file_listbox.curselection()
        return [self.selected_files[i] for i in idxs]

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

        self.worker_thread = threading.Thread(
            target=self._review_worker, args=(files, models), daemon=True
        )
        self.worker_thread.start()

    def _review_worker(self, files, models):
        code_blocks = []
        for path in files:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception as e:
                content = f"(讀取失敗: {e})"
            if len(content) > MAX_CODE_CHARS:
                content = content[:MAX_CODE_CHARS] + "\n... (內容過長，已截斷)"
            code_blocks.append(f"### 檔案: {path}\n```\n{content}\n```")
        user_content = "請審查以下程式碼：\n\n" + "\n\n".join(code_blocks)

        for model in models:
            if self.stop_event.is_set():
                break
            self.ui_queue.put(("status", f"正在執行 {model} ..."))

            def on_chunk(chunk, _model=model):
                self.ui_queue.put(("append", _model, chunk))

            try:
                stream_chat(model, REVIEW_SYSTEM_PROMPT, user_content, on_chunk, self.stop_event)
            except Exception as e:
                self.ui_queue.put(("append", model, f"\n\n[錯誤] 呼叫模型失敗: {e}"))
            self.ui_queue.put(("model_done", model))

        self.ui_queue.put(("all_done",))

    def stop_review(self):
        self.stop_event.set()
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
        self.notebook.select(len(REVIEW_MODELS))
        self.status_label.config(text=f"正在用 {model} 整合結果...")

        parts = [f"【{m} 的審查意見】\n{txt}" for m, txt in reviews.items()]
        user_content = "\n\n".join(parts)

        def worker():
            def on_chunk(chunk):
                self.ui_queue.put(("integ_append", chunk))
            try:
                stream_chat(model, INTEGRATION_SYSTEM_PROMPT, user_content, on_chunk, self.stop_event)
            except Exception as e:
                self.ui_queue.put(("integ_append", f"\n\n[錯誤] 整合失敗: {e}"))
            self.ui_queue.put(("integ_done",))

        threading.Thread(target=worker, daemon=True).start()

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
                    self.text_widgets[model].see("end")
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
                    self.integrate_btn.config(state="normal")
                    self.save_btn.config(state="normal")
                    self.status_label.config(text="四個模型審查完成，可以按「整合結果」")
                elif kind == "integ_append":
                    self.integration_result += item[1]
                    self.integ_text.insert("end", item[1])
                    self.integ_text.see("end")
                elif kind == "integ_done":
                    converted = to_traditional(self.integration_result)
                    if converted != self.integration_result:
                        self.integration_result = converted
                        self.integ_text.delete("1.0", "end")
                        self.integ_text.insert("end", converted)
                    self.integrate_btn.config(state="normal")
                    self.save_btn.config(state="normal")
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
