import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from playlist_document import PlaylistDocument


class PlaylistEditor(tk.Toplevel):
    def __init__(self, parent, directory, on_saved):
        super().__init__(parent)
        self.title('TXT 歌单管理 · 保存后同步下载任务')
        self.geometry('920x600')
        self.transient(parent)
        self.grab_set()
        self.directory = Path(directory)
        self.on_saved = on_saved
        self.document = None
        self.path_text = tk.StringVar(value='选择现有 TXT，或新建歌单')
        self.query = tk.StringVar()
        self.title_value, self.artist_value = tk.StringVar(), tk.StringVar()
        top = ttk.Frame(self, padding=10)
        top.pack(fill='x')
        ttk.Button(top, text='打开 TXT', command=self.open).pack(side='left', padx=4)
        ttk.Button(top, text='新建 TXT', command=self.new).pack(side='left', padx=4)
        ttk.Button(top, text='保存 TXT 并同步任务', command=self.save).pack(side='left', padx=4)
        ttk.Label(self, textvariable=self.path_text, wraplength=890).pack(anchor='w', padx=14)
        row = ttk.Frame(self, padding=10)
        row.pack(fill='x')
        ttk.Label(row, text='查找歌名 / 歌手：').pack(side='left')
        ttk.Entry(row, textvariable=self.query).pack(side='left', fill='x', expand=True)
        self.query.trace_add('write', lambda *_: self.refresh())
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(frame, columns=('line', 'title', 'artist'), show='headings', selectmode='browse')
        for key, label, width in [('line', 'TXT 行号', 70), ('title', '歌名', 370), ('artist', '歌手', 340)]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width)
        self.tree.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(frame, command=self.tree.yview)
        scrollbar.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        details = ttk.Frame(self, padding=10)
        details.pack(fill='x')
        ttk.Label(details, text='歌名').grid(row=0, column=0)
        ttk.Entry(details, textvariable=self.title_value, width=38).grid(row=0, column=1, padx=5)
        ttk.Label(details, text='歌手').grid(row=0, column=2)
        ttk.Entry(details, textvariable=self.artist_value, width=30).grid(row=0, column=3, padx=5)
        for col, (label, op) in enumerate([('新增歌曲', self.add), ('修改选中', self.update), ('删除选中', self.delete)]):
            ttk.Button(details, text=label, command=op).grid(row=1, column=col, pady=10, padx=4)
        ttk.Label(self, text='增删改先保存在编辑器中；点击保存才写回 TXT。原文件自动备份，歌曲增删同步任务，不删除音乐文件。',
                  wraplength=880).pack(anchor='w', padx=14, pady=(0, 10))
        self.protocol('WM_DELETE_WINDOW', self.close)

    def discard_allowed(self):
        return not self.document or not self.document.dirty or messagebox.askyesno('尚未保存', '放弃当前未保存的修改？', parent=self)

    def load(self, path):
        try:
            self.document = PlaylistDocument(path)
            self.path_text.set(str(self.document.path))
            self.query.set('')
            self.refresh()
        except Exception as exc:
            messagebox.showerror('打开失败', str(exc), parent=self)

    def open(self):
        if self.discard_allowed():
            path = filedialog.askopenfilename(parent=self, initialdir=str(self.directory), filetypes=[('TXT 歌单', '*.txt *.list')])
            if path:
                self.load(path)

    def new(self):
        if self.discard_allowed():
            path = filedialog.asksaveasfilename(parent=self, initialdir=str(self.directory), defaultextension='.txt', filetypes=[('TXT 歌单', '*.txt')])
            if path:
                self.load(path)

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        if self.document:
            query = self.query.get().strip().casefold()
            for track in self.document.tracks():
                if query in (track.title+' '+track.artist).casefold():
                    self.tree.insert('', 'end', iid=str(track.source_line), values=(track.source_line, track.title, track.artist))

    def select(self, _event=None):
        if self.tree.selection():
            values = self.tree.item(self.tree.selection()[0], 'values')
            self.title_value.set(values[1])
            self.artist_value.set(values[2])

    def apply(self, action):
        try:
            if not self.document:
                raise ValueError('请先打开或新建 TXT')
            action()
            self.refresh()
            self.path_text.set(str(self.document.path)+'  [未保存]')
        except Exception as exc:
            messagebox.showerror('无法修改', str(exc), parent=self)

    def selected_line(self):
        if not self.tree.selection():
            raise ValueError('请选择要操作的歌曲')
        return int(self.tree.selection()[0])

    def add(self):
        self.apply(lambda: self.document.add(self.title_value.get(), self.artist_value.get()))

    def update(self):
        self.apply(lambda: self.document.update(self.selected_line(), self.title_value.get(), self.artist_value.get()))

    def delete(self):
        self.apply(lambda: self.document.delete(self.selected_line()))

    def save(self):
        if not self.document:
            return
        try:
            backup = self.document.save()
        except Exception as exc:
            messagebox.showerror('保存失败', str(exc), parent=self)
            return
        self.path_text.set(str(self.document.path)+'  [已保存]')
        try:
            self.on_saved(self.document.path)
            messagebox.showinfo('保存成功', 'TXT 已保存，下载任务已同步。'+ (f'\n备份：{backup.name}' if backup else ''), parent=self)
        except Exception as exc:
            messagebox.showerror('TXT 已保存，但任务同步失败', str(exc), parent=self)

    def close(self):
        if self.discard_allowed():
            self.destroy()
