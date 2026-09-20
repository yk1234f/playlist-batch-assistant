from __future__ import annotations

import csv
import json
import os
import queue
import sys
import tkinter as tk
import webbrowser
import uuid
import time
from dataclasses import replace
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from urllib.parse import urlencode

from audio_files import MIN_BYTES, detect_directories
from engine import BatchWorker
from browser_runtime import BROWSER_OPTIONS
from playlist_parser import Track, parse_file, dedupe_tracks
from provider import BASE_URL, SOURCES
from source_policy import selected_for
from playlist_document import sync_playlists
from playlist_editor import PlaylistEditor
from duplicate_dialog import DuplicateDialog
from short_audio import mark_deleted_short

APP_NAME = '歌单批量下载助手 v2.4 · 浏览器启动提示'


class PlaylistApp(tk.Tk):
    def __init__(self, state_path=None):
        super().__init__()
        self.title(APP_NAME)
        self.geometry('1220x940')
        self.minsize(1000, 700)
        self.tracks = []
        self.playlist_paths = []
        self.source_vars = {source: tk.BooleanVar(value=True) for source in SOURCES.values()}
        self.retry_vars = {source: tk.BooleanVar(value=False) for source in SOURCES.values()}
        self.by_filename = tk.BooleanVar(value=False)
        self.track_source = tk.StringVar(value='自动（按规则）')
        self.active_source = tk.StringVar(value='当前音源：—')
        self.browser_choice = tk.StringVar(value=next(iter(BROWSER_OPTIONS)))
        self.browser_status = tk.StringVar(value='浏览器：尚未启动（需要已安装 Edge 或 Chrome）')
        self.duplicates = 0
        self.roots = []
        self.current_uid = None
        self.current_page = ''
        self.worker = None
        self.busy = False
        self.paused = False
        self.closing = False
        self.events = queue.Queue()
        self.state_path = state_path or Path(os.getenv('LOCALAPPDATA', str(Path.home()))) / 'PlaylistBatchAssistant' / 'session.json'
        self.destination = tk.StringVar(value=str(Path.home() / 'Music' / 'PlaylistAssistant'))
        self.minimum = tk.StringVar(value=str(MIN_BYTES // 1024))
        self.stats = tk.StringVar(value='请导入 TXT / CSV 歌单')
        self.message = tk.StringVar(value='点击开始后自动打开浏览器，输入、搜索、选歌并点击网页下载。')
        self.step_started = time.monotonic()
        self.step_text = ''
        self.current = tk.StringVar(value='当前：—')
        self.match = tk.StringVar(value='匹配：—')
        self.root_text = tk.StringVar(value='尚未选择本地音乐目录')
        self._build_ui()
        if self.state_path.is_file():
            try:
                self._load(self.state_path)
                self.message.set('已恢复上次会话；开始时将重新扫描本地文件')
            except Exception as exc:
                self.message.set(f'旧会话无法读取：{exc}')
        self.after(100, self.poll)
        self.protocol('WM_DELETE_WINDOW', self.close)

    def _build_ui(self):
        style = ttk.Style()
        style.configure('TButton', padding=(10, 6))
        self.mutable_buttons = []
        top = ttk.Frame(self, padding=12)
        top.pack(fill='x')
        for label, callback in [('导入歌单', self.import_files), ('管理 TXT 歌单', self.edit_playlist),
                                ('从 TXT 重新同步', self.reload_playlists), ('载入会话', self.load_session),
                                ('清空歌单', self.clear), ('重试失败 / 待确认', self.retry)]:
            button = ttk.Button(top, text=label, command=callback)
            button.pack(side='left', padx=3)
            self.mutable_buttons.append(button)
        ttk.Button(top, text='保存会话', command=self.save_session).pack(side='left', padx=3)
        ttk.Button(top, text='导出日志', command=self.export_log).pack(side='left', padx=3)
        settings = ttk.LabelFrame(self, text='本地音乐与下载位置', padding=10)
        settings.pack(fill='x', padx=12)
        bar = ttk.Frame(settings)
        bar.pack(fill='x')
        for label, callback in [('自动识别', self.detect), ('选择目录', self.choose_root),
                                ('清除扫描目录', self.clear_roots), ('重新扫描', lambda: self.start(True)),
                                ('文件去重 / 删除重复', lambda: DuplicateDialog(self))]:
            button = ttk.Button(bar, text=label, command=callback)
            button.pack(side='left', padx=3)
            self.mutable_buttons.append(button)
        ttk.Label(bar, text='注意：去重删除会删除文件', foreground='#a02020').pack(side='left', padx=8)
        short_bar = ttk.Frame(settings)
        short_bar.pack(fill='x', pady=(5,0))
        short_button = ttk.Button(short_bar, text='删除短音频（<90秒）', command=lambda: DuplicateDialog(self, short=True))
        short_button.pack(side='left', padx=3)
        self.mutable_buttons.append(short_button)
        ttk.Label(short_bar, text='会删除文件；不足 90 秒不保存并标记“时长不足”，90 秒整及以上保留。', foreground='#a02020').pack(side='left')
        ttk.Label(settings, textvariable=self.root_text, wraplength=1080).pack(anchor='w', pady=8)
        row = ttk.Frame(settings)
        row.pack(fill='x')
        ttk.Label(row, text='保存到：').pack(side='left')
        entry = ttk.Entry(row, textvariable=self.destination)
        entry.pack(side='left', fill='x', expand=True, padx=4)
        self.mutable_buttons.append(entry)
        choose = ttk.Button(row, text='选择下载目录', command=self.choose_destination)
        choose.pack(side='left')
        self.mutable_buttons.append(choose)
        ttk.Label(row, text='最小文件 KiB：').pack(side='left', padx=(12, 0))
        minimum = ttk.Entry(row, textvariable=self.minimum, width=8)
        minimum.pack(side='left')
        self.mutable_buttons.append(minimum)
        browser_row = ttk.Frame(self, padding=(12, 6))
        browser_row.pack(fill='x')
        ttk.Label(browser_row, text='自动操作浏览器：').pack(side='left')
        browser_combo = ttk.Combobox(browser_row, textvariable=self.browser_choice,
                                     values=list(BROWSER_OPTIONS), state='readonly', width=24)
        browser_combo.pack(side='left')
        browser_combo.bind('<<ComboboxSelected>>', lambda event: self.autosave())
        self.mutable_buttons.append(browser_combo)
        ttk.Label(browser_row, textvariable=self.browser_status).pack(side='left', padx=10)
        policy = ttk.LabelFrame(self, text='搜索音源（从左至右；精确匹配即停止搜索）', padding=6)
        policy.pack(fill='x', padx=12, pady=5)
        ttk.Label(policy, text='启用音源').grid(row=0, column=0, sticky='w')
        ttk.Label(policy, text='查找失败后二次搜索').grid(row=1, column=0, sticky='w')
        for col, (label, source) in enumerate(SOURCES.items(), 1):
            for row, variables in ((0, self.source_vars), (1, self.retry_vars)):
                check = ttk.Checkbutton(policy, text=label, variable=variables[source], command=self.policy_changed)
                check.grid(row=row, column=col, sticky='w', padx=12)
                self.mutable_buttons.append(check)
        infer = ttk.Checkbutton(policy, text='按 TXT 文件名识别音源（识别到时优先使用；未识别按上方勾选）',
                                variable=self.by_filename, command=self.policy_changed)
        infer.grid(row=2, column=0, columnspan=6, sticky='w')
        self.mutable_buttons.append(infer)
        ttk.Label(policy, text='下载返回错误内容时，当前歌曲自动重试 2 次（3 秒 / 6 秒）；与“二次搜索”独立，不重搜其他音源。').grid(row=3, column=0, columnspan=6, sticky='w')
        ttk.Label(self, textvariable=self.stats, font=('Microsoft YaHei UI', 11, 'bold'), wraplength=1120).pack(anchor='w', padx=16, pady=12)
        ttk.Label(self, textvariable=self.current, wraplength=1100).pack(anchor='w', padx=16)
        ttk.Label(self, textvariable=self.active_source, font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w', padx=16)
        ttk.Label(self, textvariable=self.match, wraplength=1100).pack(anchor='w', padx=16, pady=6)
        actions = ttk.Frame(self, padding=(12, 5))
        actions.pack(fill='x')
        self.start_button = ttk.Button(actions, text='开始网页自动下载', command=self.start)
        self.start_button.pack(side='left', padx=3)
        self.pause_button = ttk.Button(actions, text='暂停', command=self.pause)
        self.pause_button.pack(side='left', padx=3)
        ttk.Button(actions, text='跳过', command=self.skip).pack(side='left', padx=3)
        ttk.Button(actions, text='停止', command=self.stop).pack(side='left', padx=3)
        ttk.Button(actions, text='显示自动浏览器', command=self.open_page).pack(side='left', padx=3)
        ttk.Label(actions, text='自动操作浏览器；无需手动搜索或点下载').pack(side='right')
        frame = ttk.Frame(self, padding=12)
        source_row = ttk.Frame(self, padding=(12, 4))
        source_row.pack(fill='x')
        ttk.Label(source_row, text='所选歌曲音源：').pack(side='left')
        self.source_combo = ttk.Combobox(source_row, textvariable=self.track_source, state='readonly',
                                        values=['自动（按规则）', *SOURCES], width=20)
        self.source_combo.pack(side='left')
        self.source_combo.bind('<<ComboboxSelected>>', self.set_track_source)
        self.mutable_buttons.append(self.source_combo)
        ttk.Label(source_row, text='选中下方歌曲即可修改；单首手动指定优先于文件名与全局设置').pack(side='left', padx=10)
        frame.pack(fill='both', expand=True)
        columns = ('status', 'source', 'title', 'artist', 'note')
        self.tree = ttk.Treeview(frame, columns=columns, show='headings')
        for key, label, width in [('status', '状态', 80), ('source', '音源 / 策略', 180), ('title', '歌名', 240),
                                  ('artist', '歌手', 200), ('note', '文件 / 详情', 520)]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=70)
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        xscroll = ttk.Scrollbar(frame, orient='horizontal', command=self.tree.xview)
        xscroll.grid(row=1, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=xscroll.set)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.tree.bind('<<TreeviewSelect>>', self.selection_changed)
        self.log_box = ScrolledText(self, height=6, wrap='word', state='disabled', font=('Microsoft YaHei UI', 9))
        self.log_box.pack(fill='x', padx=12, pady=(0, 6))
        ttk.Label(self, textvariable=self.message, wraplength=1120).pack(anchor='w', padx=16, pady=(0, 12))

    def log(self, message):
        self.step_started = time.monotonic()
        self.step_text = message
        self.message.set(message)
        line = f'[{time.strftime("%H:%M:%S")}] {message}\n'
        self.log_box.configure(state='normal')
        self.log_box.insert('end', line)
        if int(self.log_box.index('end-1c').split('.')[0]) > 500:
            self.log_box.delete('1.0', '100.0')
        self.log_box.see('end')
        self.log_box.configure(state='disabled')
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            logfile = self.state_path.parent / 'runtime.log'
            if logfile.exists() and logfile.stat().st_size > 5 * 1024 * 1024:
                os.replace(logfile, logfile.with_suffix('.previous.log'))
            with logfile.open('a', encoding='utf-8') as stream:
                stream.write(line)
        except OSError:
            pass

    def selection_changed(self, _event=None):
        if not self.busy and self.tree.selection():
            self.current_uid = self.tree.selection()[0]
            self.current_page = ''
            track = self.by_uid(self.current_uid)
            if track:
                self.track_source.set(next((label for label, source in SOURCES.items() if source == track.music_source), '自动（按规则）'))
                self.current.set(f'当前：{track.title} - {track.artist}')
                self.match.set('匹配：尚未搜索')

    def by_uid(self, uid):
        return next((t for t in self.tracks if t.uid == uid), None)

    def source_label(self, track):
        selected = [s for s, variable in self.source_vars.items() if variable.get()]
        resolved = selected_for(track, selected, self.by_filename.get())
        names = {s: label for label, s in SOURCES.items()}
        return ('指定：' if track.music_source else '') + (' → '.join(names[s] for s in resolved) or '未选择')

    def row_values(self, track):
        return (track.status, self.source_label(track), track.title, track.artist, track.note)

    def policy_changed(self):
        if not self.busy:
            self.refresh()
            self.autosave()

    def set_track_source(self, _event=None):
        if self.busy or not self.tree.selection():
            return
        track = self.by_uid(self.tree.selection()[0])
        if track:
            track.music_source = SOURCES.get(self.track_source.get(), '')
            if track.status in ('失败', '待确认', '时长不足'):
                track.status, track.note = '待处理', ''
            self.tree.item(track.uid, values=self.row_values(track))
            self.update_stats()
            self.autosave()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for t in self.tracks:
            self.tree.insert('', 'end', iid=t.uid, values=self.row_values(t))
        self.update_stats()
        self.root_text.set('扫描：' + '；'.join(str(p) for p in self.roots) if self.roots else '尚未选择本地音乐目录（下载目录始终扫描）')

    def update_stats(self):
        counts = {}
        for t in self.tracks:
            counts[t.status] = counts.get(t.status, 0) + 1
        pending = sum(counts.get(s, 0) for s in ('待处理', '搜索中', '下载中'))
        self.stats.set(f'歌单歌曲：{len(self.tracks) + self.duplicates}    歌单内部重复：{self.duplicates}    '
                       f'本地已有：{counts.get("本地已有", 0)}    待下载：{pending}    '
                       f'下载成功：{counts.get("成功", 0)}    失败：{counts.get("失败", 0)}    '
                       f'待确认：{counts.get("待确认", 0)}    时长不足：{counts.get("时长不足", 0)}    跳过：{counts.get("跳过", 0)}')

    def import_files(self):
        if self.busy:
            return
        paths = filedialog.askopenfilenames(filetypes=[('歌单', '*.txt *.csv *.list')])
        if not paths:
            return
        self.playlist_paths = list(dict.fromkeys(self.playlist_paths + [str(Path(p).resolve()) for p in paths]))
        self.reload_playlists()

    def playlist_directory(self):
        base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
        for directory in (base / 'playlists', base.parent / 'playlists'):
            if directory.is_dir():
                return directory
        return base / 'playlists'

    def edit_playlist(self):
        if not self.busy:
            PlaylistEditor(self, self.playlist_directory(), self.playlist_saved)

    def playlist_saved(self, path):
        if self.busy:
            raise RuntimeError('下载正在运行，请停止后重新同步')
        self.playlist_paths = list(dict.fromkeys(self.playlist_paths + [str(Path(path).resolve())]))
        self.reload_playlists(raise_errors=True)

    def reload_playlists(self, raise_errors=False):
        if self.busy:
            return
        if not self.playlist_paths:
            self.message.set('请先导入或打开 TXT 歌单')
            return
        try:
            tracks, duplicates = sync_playlists(self.playlist_paths, self.tracks)
        except Exception as exc:
            self.message.set(f'同步失败，原任务保持不变：{exc}')
            if raise_errors:
                raise
            return
        self.tracks, self.duplicates = tracks, duplicates
        mark_deleted_short(self.tracks, self.state_path.parent)
        self.current_uid = None
        self.refresh()
        self.autosave()
        self.message.set(f'已从 {len(self.playlist_paths)} 个歌单同步 {len(tracks)} 个任务；重复 {duplicates} 首')
        self.start(True)

    def clear(self):
        if self.busy or not messagebox.askyesno('清空歌单', '清空处理列表？本地音乐文件不会删除。'):
            return
        self.tracks, self.duplicates = [], 0
        self.playlist_paths = []
        self.current_uid = None
        self.refresh()
        self.autosave()

    def detect(self):
        if self.busy:
            return
        self.roots = list(dict.fromkeys(self.roots + detect_directories()))
        self.refresh()
        self.message.set('已识别系统音乐/下载目录；其他磁盘的音乐文件夹请用“选择目录”添加')
        self.autosave()

    def choose_root(self):
        if self.busy:
            return
        path = filedialog.askdirectory(title='添加本地音乐目录（可多次添加）')
        if path:
            self.roots = list(dict.fromkeys(self.roots + [Path(path)]))
            self.refresh()
            self.autosave()

    def clear_roots(self):
        if not self.busy:
            self.roots = []
            self.refresh()
            self.autosave()

    def choose_destination(self):
        if self.busy:
            return
        path = filedialog.askdirectory(title='选择下载目录')
        if path:
            self.destination.set(path)
            self.autosave()

    def start(self, scan_only=False):
        if self.busy:
            return
        if self.worker and self.worker.is_alive():
            self.worker.stop()
            self.worker.join(2)
            if self.worker.is_alive():
                self.message.set('正在关闭上轮浏览器，请稍后再点击开始')
                return
        if not self.tracks and not scan_only:
            self.message.set('请先导入歌单')
            return
        try:
            minimum = int(self.minimum.get()) * 1024
            if minimum < 1024 or minimum > 100 * 1024 * 1024:
                raise ValueError()
            if not self.destination.get().strip():
                raise ValueError()
        except ValueError:
            self.message.set('请填写下载目录和 1–102400 KiB 的最小文件大小')
            return
        selected_sources = [source for source, variable in self.source_vars.items() if variable.get()]
        if not scan_only and not any(selected_for(t, selected_sources, self.by_filename.get()) for t in self.tracks):
            self.message.set('请至少选择一个音源，或为歌曲指定音源')
            return
        if not self.roots:
            self.roots = detect_directories()
            self.refresh()
        self.busy, self.paused = True, False
        self.start_button.state(['disabled'])
        for button in self.mutable_buttons:
            button.state(['disabled'])
        self.worker = BatchWorker([replace(t) for t in self.tracks], list(self.roots),
                                  Path(self.destination.get()).expanduser(), self.events,
                                  min_bytes=minimum, scan_only=scan_only, sources=selected_sources,
                                  retry_sources=[s for s, variable in self.retry_vars.items() if variable.get()],
                                  sources_by_filename=self.by_filename.get(),
                                  browser_channel=BROWSER_OPTIONS[self.browser_choice.get()])
        self.worker.start()
        self.log('开始扫描本地文件' if scan_only else '开始网页自动流程：扫描 → 自动打开浏览器 → 搜索 → 点击下载 → 校验')

    def pause(self):
        if not self.busy or not self.worker:
            return
        self.paused = not self.paused
        self.worker.pause() if self.paused else self.worker.resume()
        self.pause_button.configure(text='继续' if self.paused else '暂停')
        self.message.set('暂停已请求；正在等待的网络请求最迟超时后响应' if self.paused else '继续运行')

    def skip(self):
        if self.busy and self.worker:
            self.worker.skip()
            return
        track = self.by_uid(self.current_uid)
        if track and track.status not in ('成功', '本地已有'):
            track.status = '跳过'
            self.refresh()
            self.autosave()

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop()
            self.message.set('正在停止，请等待当前网络请求结束……')

    def retry(self):
        if self.busy:
            return
        for t in self.tracks:
            if t.status in ('失败', '待确认', '跳过'):
                t.status, t.note = '待处理', ''
        self.refresh()
        self.autosave()

    def open_page(self):
        if self.worker and self.worker.is_alive():
            self.worker.focus_browser()
            self.message.set('已请求显示程序正在操作的浏览器窗口')
        else:
            self.message.set('点击“开始网页自动下载”后会自动打开浏览器，无需手动打开网页')

    def poll(self):
        dirty = False
        try:
            while True:
                evt = self.events.get_nowait()
                kind = evt['kind']
                if kind == 'message':
                    self.log(evt['text'])
                elif kind == 'browser':
                    self.browser_status.set('已启动浏览器：' + evt['name'])
                elif kind == 'scan':
                    self.message.set(f'本地有效音频 {evt["count"]} 个；扫描警告 {len(evt["warnings"])} 条')
                    try:
                        self.state_path.parent.mkdir(parents=True, exist_ok=True)
                        (self.state_path.parent / 'scan_warnings.txt').write_text('\n'.join(evt['warnings']), encoding='utf-8')
                    except OSError as exc:
                        self.message.set(f'无法写入扫描日志：{exc}')
                elif kind == 'current':
                    self.current_uid = evt['uid']
                    self.current_page = ''
                    self.active_source.set('当前音源：等待搜索')
                    track = self.by_uid(self.current_uid)
                    if track:
                        self.track_source.set(next((label for label, source in SOURCES.items() if source == track.music_source), '自动（按规则）'))
                        self.current.set(f'当前：{track.title} - {track.artist}')
                        self.match.set('匹配：正在搜索……')
                        self.tree.selection_set(track.uid)
                        self.tree.see(track.uid)
                elif kind == 'source':
                    name = next((label for label, source in SOURCES.items() if source == evt['source']), evt['source'])
                    self.active_source.set(f'当前音源：{name} · 第 {evt["round"]} 次搜索')
                elif kind == 'match':
                    self.current_page = evt['page']
                    self.match.set(f'歌名 {evt["title"]:.0%}  歌手 {evt["artist"]:.0%}  版本 {"一致 ✓" if evt["version"] else "不同 ×"}   候选：{evt["candidate"]}')
                elif kind == 'status':
                    track = self.by_uid(evt['uid'])
                    if track:
                        track.status, track.note = evt['status'], evt['note']
                        self.tree.item(track.uid, values=self.row_values(track))
                        if track.status in ('失败', '待确认', '成功', '时长不足'):
                            self.log(f'{track.title} - {track.artist}：{track.status}；{track.note}')
                        dirty = True
                elif kind == 'done':
                    self.busy = False
                    self.paused = False
                    self.start_button.state(['!disabled'])
                    self.pause_button.configure(text='暂停')
                    for button in self.mutable_buttons:
                        button.state(['!disabled'])
                    if evt.get('interruption'):
                        self.active_source.set('当前音源：已中断')
                        self.log('本轮已中断：' + evt['interruption'])
                    else:
                        self.log('本轮已结束；请查看上方日志。浏览器仍开启时可供核对，停止或关闭程序时自动关闭。')
                    dirty = True
        except queue.Empty:
            pass
        if dirty:
            self.update_stats()
            self.autosave()
        if self.busy and not self.paused and self.step_text:
            elapsed = int(time.monotonic() - self.step_started)
            if elapsed >= 5:
                self.message.set(f'{self.step_text}（已等待 {elapsed} 秒）')
        if self.closing and not self.busy:
            if self.worker and self.worker.is_alive():
                self.worker.join(.2)
                if self.worker.is_alive():
                    self.after(100, self.poll)
                    return
            self.autosave()
            self.destroy()
            return
        self.after(100, self.poll)

    def _data(self):
        return dict(browser_channel=BROWSER_OPTIONS[self.browser_choice.get()], version=3, duplicates=self.duplicates, playlist_paths=self.playlist_paths,
                    sources=[s for s,v in self.source_vars.items() if v.get()],
                    retry_sources=[s for s,v in self.retry_vars.items() if v.get()],
                    sources_by_filename=self.by_filename.get(), roots=[str(p) for p in self.roots],
                    download_dir=self.destination.get(), minimum_kib=self.minimum.get(),
                    tracks=[t.to_dict() for t in self.tracks])

    def _save(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.json.tmp')
        temp.write_text(json.dumps(self._data(), ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, path)

    def autosave(self):
        try:
            self._save(self.state_path)
        except OSError as exc:
            self.message.set(f'会话保存失败：{exc}')

    def save_session(self):
        path = filedialog.asksaveasfilename(defaultextension='.json', filetypes=[('会话', '*.json')])
        if path:
            try:
                self._save(Path(path))
            except OSError as exc:
                messagebox.showerror('保存失败', str(exc))

    def _load(self, path):
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        tracks = [Track(**item) for item in data.get('tracks', [])]
        unique, duplicates = dedupe_tracks(tracks)
        for i, track in enumerate(unique):
            track.uid = f'session-{i}'
            if track.status in ('搜索中', '下载中'):
                track.status = '待处理'
            if track.status == '失败' and 'Target page, context or browser has been closed' in track.note:
                track.status = '待处理'
                track.note = '上次浏览器关闭导致下载中断，已恢复待处理；重新开始即可重试'
        self.tracks = unique
        self.playlist_paths = list(data.get('playlist_paths', []))
        for track in unique:
            inferred = Path(track.source_path) if track.source_path else self.playlist_directory() / track.source_file
            if track.source_file and inferred.is_file() and str(inferred.resolve()) not in self.playlist_paths:
                self.playlist_paths.append(str(inferred.resolve()))
        for source, variable in self.source_vars.items():
            variable.set(source in data.get('sources', list(SOURCES.values())))
        for source, variable in self.retry_vars.items():
            variable.set(source in data.get('retry_sources', []))
        self.by_filename.set(bool(data.get('sources_by_filename', False)))
        self.browser_choice.set(next((label for label, code in BROWSER_OPTIONS.items()
                                      if code == data.get('browser_channel')), next(iter(BROWSER_OPTIONS))))
        self.duplicates = max(0, int(data.get('duplicates', 0))) + len(duplicates)
        self.roots = [Path(p) for p in data.get('roots', [])]
        self.destination.set(data.get('download_dir', self.destination.get()))
        self.minimum.set(str(data.get('minimum_kib', MIN_BYTES // 1024)))
        mark_deleted_short(self.tracks, self.state_path.parent)
        self.refresh()

    def load_session(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(filetypes=[('会话', '*.json')])
        if path:
            try:
                self._load(Path(path))
                self.autosave()
            except Exception as exc:
                messagebox.showerror('载入失败', str(exc))

    def export_log(self):
        path = filedialog.asksaveasfilename(defaultextension='.csv', filetypes=[('日志', '*.csv')])
        if path:
            try:
                with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow(['状态', '歌名', '歌手', '详情'])
                    for t in self.tracks:
                        # Keep untrusted song names from becoming spreadsheet formulas.
                        writer.writerow(["'" + v if v.startswith(('=', '+', '-', '@')) else v
                                         for v in (t.status, t.title, t.artist, t.note)])
            except OSError as exc:
                messagebox.showerror('导出失败', str(exc))

    def close(self):
        self.closing = True
        if self.worker:
            self.worker.stop()
        if self.busy:
            self.stop()
        else:
            self.autosave()
            if self.worker and self.worker.is_alive():
                self.message.set('正在关闭自动浏览器……')
            else:
                self.destroy()


if __name__ == '__main__':
    if '--browser-self-test' in sys.argv:
        from browser_selftest import main
        raise SystemExit(main())
    if '--self-test' in sys.argv:
        from selftest import main
        raise SystemExit(main())
    PlaylistApp().mainloop()
