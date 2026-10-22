"""tkinter 置顶悬浮双语字幕窗（暗色半透明，可拖动，Esc 退出）。"""
from __future__ import annotations

import os
import queue as queue_mod
import tkinter as tk

from .transcriber import MeetingTranscriber

BG = "#1f2328"
FG_ORIG = "#c9d1d9"
FG_TRANS = "#f0f6fc"
FG_PARTIAL = "#8b949e"
FG_ACCENT = "#58a6ff"


class SubtitleWindow:
    def __init__(self, cfg, transcriber: MeetingTranscriber):
        self.cfg = cfg
        self.t = transcriber
        self._partial = ""
        self._drag = {"x": 0, "y": 0}
        self._anchors: dict[int, str] = {}
        self._cleared = 0
        self._started_once = False     # 是否已经开过一场（决定中央提示是否还显示）

        self.root = tk.Tk()
        self.root.title("会议实时字幕")
        self.root.geometry(f"{cfg.ui.width}x{cfg.ui.height}+80+120")
        self.root.attributes("-topmost", bool(cfg.ui.always_on_top))
        self.root.attributes("-alpha", float(cfg.ui.opacity))
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Escape>", lambda e: self._on_close())
        self._build()
        # 不自动开始：等用户点「开始」才采集系统音频
        if getattr(cfg.ui, "autostart", False):
            self.root.after(150, self._toggle)
        self.root.after(80, self._poll)

    # ---------------- 界面构建 ----------------

    def _build(self) -> None:
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", side="top")
        self.status = tk.Label(bar, text="● 待开始", fg="#8b949e", bg=BG,
                               font=("Microsoft YaHei", 10))
        self.status.pack(side="left", padx=10)
        self.lang_lbl = tk.Label(
            bar,
            text=f"{self.cfg.asr.source_lang.upper()} → {self.cfg.translate.target_lang.upper()}",
            fg=FG_PARTIAL, bg=BG, font=("Consolas", 10))
        self.lang_lbl.pack(side="left", padx=6)
        # 翻译后端状态徽标（预检结果在启动后异步到达，这里先显示占位）
        tr_ok = self.t.translator.enabled
        self.tr_lbl = tk.Label(
            bar,
            text=("翻译已就绪" if tr_ok else "仅转写"),
            fg=("#3fb950" if tr_ok else "#d29922"), bg=BG, font=("Microsoft YaHei", 9))
        self.tr_lbl.pack(side="left", padx=2)
        self.btn = tk.Button(bar, text="▶ 开始", command=self._toggle, bg="#238636",
                             fg="#ffffff", relief="flat",
                             font=("Microsoft YaHei", 10), padx=14, cursor="hand2")
        self.btn.pack(side="right", padx=8, pady=4)
        # 清屏：只清除界面显示，不影响已落盘的 MD 文档
        self.clear_btn = tk.Button(bar, text="清屏", command=self._clear_screen, bg="#30363d",
                                   fg="#c9d1d9", relief="flat",
                                   font=("Microsoft YaHei", 9), cursor="hand2")
        self.clear_btn.pack(side="right", padx=4, pady=4)
        self.dir_btn = tk.Button(bar, text="打开目录", command=self._open_dir, bg="#30363d",
                                 fg="#c9d1d9", relief="flat",
                                 font=("Microsoft YaHei", 9), cursor="hand2")
        self.dir_btn.pack(side="right", padx=4, pady=4)
        for w in (bar, self.status, self.lang_lbl, self.tr_lbl):
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<B1-Motion>", self._motion)

        self.partial_lbl = tk.Label(self.root, text="", fg=FG_PARTIAL, bg=BG,
                                    font=("Microsoft YaHei", self.cfg.ui.font_size - 1, "italic"),
                                    anchor="w", padx=10)
        self.partial_lbl.pack(fill="x", side="top")

        self.text = tk.Text(self.root, bg=BG, fg=FG_ORIG, bd=0, relief="flat",
                            font=("Microsoft YaHei", self.cfg.ui.font_size),
                            wrap="word", state="disabled", padx=10, pady=4,
                            spacing1=2, spacing3=3)
        self.text.pack(fill="both", expand=True, side="top")
        self.text.tag_config("orig", foreground=FG_ORIG)
        self.text.tag_config("trans", foreground=FG_TRANS, font=("Microsoft YaHei", self.cfg.ui.font_size, "bold"))
        self.text.tag_config("err", foreground="#f85149")
        self.text.tag_config("note", foreground="#d29922")

        # 中央「开始」引导层：未点击开始前盖在字幕区上方，明确告知需手动启动
        self.start_overlay = tk.Frame(self.root, bg=BG)
        self.start_overlay.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(self.start_overlay, text="点击「开始」后开始监听系统音频",
                 fg="#8b949e", bg=BG, font=("Microsoft YaHei", 10)).pack(pady=(0, 8))
        tk.Button(self.start_overlay, text="▶  开始", command=self._toggle, bg="#238636",
                  fg="#ffffff", relief="flat", activebackground="#2ea043",
                  font=("Microsoft YaHei", 14, "bold"), padx=30, pady=10,
                  cursor="hand2").pack()

    # ---------------- 交互 ----------------

    def _press(self, e) -> None:
        self._drag = {"x": e.x, "y": e.y}

    def _motion(self, e) -> None:
        x = self.root.winfo_x() + e.x - self._drag["x"]
        y = self.root.winfo_y() + e.y - self._drag["y"]
        self.root.geometry(f"+{x}+{y}")

    def _toggle(self) -> None:
        """开始 / 停止 采集。

        只有用户显式点击才会启动采集——程序启动后不碰系统音频设备。
        """
        if self.t.running:
            self.t.stop()
            self._set_state("stopped")
        else:
            self.t.start()
            self._started_once = True
            self._hide_start_overlay()
            self._set_state("running")

    def _hide_start_overlay(self) -> None:
        try:
            self.start_overlay.place_forget()
        except Exception:  # noqa: BLE001
            pass

    def _set_state(self, state: str) -> None:
        """统一刷新状态标签与按钮文案。"""
        if state == "running":
            self.status.config(text="● 采集中", fg="#3fb950")
            self.btn.config(text="■ 停止", bg="#da3633")
        elif state == "summarizing":
            self.status.config(text="● 总结中…", fg="#d29922")
            self.btn.config(text="▶ 开始", bg="#238636")
        else:
            self.status.config(text="● 已停止", fg="#f85149")
            self.btn.config(text="▶ 开始", bg="#238636")

    def _clear_screen(self) -> None:
        """清屏：只清除字幕窗内的显示内容。

        已落盘的 transcript.md / summary.md / meeting.wav 完全不受影响——
        文字记录仍完整保存在磁盘上。
        """
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")
        self.partial_lbl.config(text="")
        self._partial = ""
        # 锚点保留：已识别句的译文若稍后到达，仍能定位（找不到时退化为独立行显示）
        self._cleared += 1

    def _open_dir(self) -> None:
        if self.t.session_dir and self.t.session_dir.exists():
            os.startfile(str(self.t.session_dir))  # noqa: S606

    def _on_close(self) -> None:
        """退出：必须等总结落盘后再销毁窗口。

        总结跑在守护线程里，若先 destroy 再退出进程，线程会被中途杀掉、
        summary 丢失。退出前同步等待（stop(wait_summary=True)）。
        另处理"已点停止但总结仍在跑"的情况。
        """
        if self.t.running:
            self.status.config(text="● 总结中…", fg="#d29922")
            self.root.update_idletasks()
            self.root.update()
            self.t.stop(wait_summary=True)
        else:
            # 已停止但总结线程可能仍在跑（守护线程会随进程被杀）
            if not self.t.wait_for_summary():
                self.status.config(text="● 等待总结…", fg="#d29922")
                self.root.update_idletasks()
                self.root.update()
                self.t.wait_for_summary()
        self.root.destroy()

    # ---------------- 总结预览窗口 ----------------
    def _show_summary(self, md: str) -> None:
        win = tk.Toplevel(self.root)
        win.title("会议总结")
        win.geometry("760x620+140+80")
        win.configure(bg=BG)
        win.transient(self.root)

        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", side="top", padx=10, pady=(8, 4))
        tk.Label(head, text="会议总结（已写入 transcript.md）", fg=FG_ACCENT,
                 bg=BG, font=("Microsoft YaHei", 11, "bold")).pack(side="left")
        tk.Button(head, text="复制全文", command=lambda: self._copy(md, win),
                  bg="#30363d", fg="#c9d1d9", relief="flat",
                  font=("Microsoft YaHei", 9), cursor="hand2").pack(side="right", padx=3)
        tk.Button(head, text="打开目录", command=self._open_dir, bg="#30363d",
                  fg="#c9d1d9", relief="flat",
                  font=("Microsoft YaHei", 9), cursor="hand2").pack(side="right", padx=3)

        box = tk.Text(win, bg=BG, fg=FG_ORIG, bd=0, relief="flat",
                      font=("Microsoft YaHei", 11), wrap="word", padx=12, pady=8,
                      spacing1=3, spacing3=3)
        sb = tk.Scrollbar(win, orient="vertical", command=box.yview)
        box.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        box.pack(fill="both", expand=True, side="top")
        box.insert("1.0", md)
        box.config(state="disabled")

    def _copy(self, text: str, win: tk.Toplevel) -> None:
        win.clipboard_clear()
        win.clipboard_append(text)
        self._append("（总结已复制到剪贴板）", "err")

    # ---------------- 事件轮询 ----------------

    def _append(self, line: str, tag: str) -> None:
        self.text.config(state="normal")
        self.text.insert("end", line + "\n", tag)
        if float(self.text.index("end-1c").split(".")[0]) > 220:
            self.text.delete("1.0", "30.0")
        self.text.see("end")
        self.text.config(state="disabled")

    def _find_anchor(self, anchor_line: str) -> str:
        """定位原文行；清屏后返回空串。"""
        return self.text.search(anchor_line, "1.0", stopindex="end")

    def _insert_after(self, anchor_line: str, line: str, tag: str) -> None:
        """把译文插入到对应原文句的正下方，保持原文—译文配对可见。"""
        self.text.config(state="normal")
        idx = self._find_anchor(anchor_line)
        if not idx:
            self.text.insert("end", line + "\n", tag)
        else:
            end_of_line = f"{idx}+1line"
            self.text.insert(end_of_line, line + "\n", tag)
        if float(self.text.index("end-1c").split(".")[0]) > 220:
            self.text.delete("1.0", "30.0")
        self.text.see("end")
        self.text.config(state="disabled")

    def _poll(self) -> None:
        try:
            while True:
                ev = self.t.events.get_nowait()
                kind = ev[0]
                if kind == "status":
                    self._set_state(ev[1])
                elif kind == "final":
                    _, ts, orig, seq = ev
                    self._partial = ""
                    self.partial_lbl.config(text="")
                    self._anchors[seq] = f"[{ts}] {orig}"
                    self._append(f"[{ts}] {orig}", "orig")
                elif kind == "trans":
                    _, ts, trans, seq = ev
                    anchor = self._anchors.get(seq)
                    if anchor and self._find_anchor(anchor):
                        self._insert_after(anchor, f"      → {trans}", "trans")
                    else:
                        # 原文已被清屏移除，译文独立成行显示
                        self._append(f"      → {trans}", "trans")
                elif kind == "summary":
                    md = ev[1]
                    self._set_state("stopped")
                    self._show_summary(md)
                elif kind == "partial":
                    self._partial = ev[1]
                    self.partial_lbl.config(text="… " + self._partial[-70:])
                elif kind == "error":
                    self._append(ev[1], "err")
                elif kind == "note":
                    self._append(ev[1], "note")
                elif kind == "preflight":
                    # 后台预检完成 → 刷新翻译徽标
                    _, ok, msg = ev
                    if ok:
                        self.tr_lbl.config(text="翻译已就绪", fg="#3fb950")
                    else:
                        self.tr_lbl.config(text="仅转写", fg="#d29922")
                        self._append(f"翻译后端未就绪：{msg}", "note")
        except queue_mod.Empty:
            pass
        self.root.after(80, self._poll)

    def run(self) -> None:
        self.root.mainloop()
