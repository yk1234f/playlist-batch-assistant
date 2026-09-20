import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from datetime import datetime
import uuid
from library_duplicates import find_duplicates, delete_duplicates
from short_audio import find_short_audio, delete_short_audio, mark_deleted_short


class DuplicateDialog(tk.Toplevel):
    def __init__(self, parent, short=False):
        super().__init__(parent)
        self.parent = parent
        self.short = short
        self.title('删除不足 90 秒音频（会删除文件）' if short else '文件去重 / 删除重复（会删除文件）')
        self.geometry('1060x550')
        self.transient(parent)
        self.grab_set()
        self.plan, self.busy, self.changed = [], False, False
        self.events = queue.Queue()
        self.directory = tk.StringVar(value=parent.destination.get())
        self.status = tk.StringVar(value='先扫描；只删除能可靠解析时长且不足 90 秒的音频。' if short else '先扫描；只处理文件内容完全一致的音频副本。')
        row = ttk.Frame(self,padding=10)
        row.pack(fill='x')
        ttk.Label(row,text='音乐目录：').pack(side='left')
        self.entry=ttk.Entry(row,textvariable=self.directory)
        self.entry.pack(side='left',fill='x',expand=True)
        self.choose=ttk.Button(row,text='选择目录',command=self.select_directory)
        self.choose.pack(side='left')
        self.scan_button=ttk.Button(row,text='扫描短音频' if short else '扫描重复',command=self.scan)
        self.scan_button.pack(side='left')
        notice = '注意：会永久删除下方不足 90 秒的文件，不进入回收站；90 秒整及以上、无法可靠读取时长的文件保留。删除后任务标记“时长不足”。' if short else '注意：删除会永久移除下方“将删除”列的文件，不进入回收站；每组保留一份。不同内容/音质/版本不会按同名删除。'
        ttk.Label(self,text=notice,wraplength=1020,foreground='#a02020').pack(padx=12,pady=8,anchor='w')
        frame=ttk.Frame(self,padding=10)
        frame.pack(fill='both',expand=True)
        self.tree=ttk.Treeview(frame,columns=('keep','delete','size'),show='headings')
        for key,label,width in [('keep','实测时长' if short else '保留文件',450),('delete','将删除（确认后）',450),('size','大小 MiB',100)]:
            self.tree.heading(key,text=label);self.tree.column(key,width=width)
        self.tree.grid(row=0,column=0,sticky='nsew')
        sy=ttk.Scrollbar(frame,command=self.tree.yview);sy.grid(row=0,column=1,sticky='ns')
        sx=ttk.Scrollbar(frame,orient='horizontal',command=self.tree.xview);sx.grid(row=1,column=0,sticky='ew')
        self.tree.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
        frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
        ttk.Label(self,textvariable=self.status,wraplength=1020).pack(padx=12,anchor='w')
        self.delete_button=ttk.Button(self,text='删除清单中的短音频' if short else '删除清单中的重复文件',command=self.delete,state='disabled')
        self.delete_button.pack(pady=12)
        self.directory.trace_add('write', self.directory_changed)
        self.protocol('WM_DELETE_WINDOW',self.close)
        self.timer=self.after(100,self.poll)

    def directory_changed(self, *_):
        self.plan=[]
        self.tree.delete(*self.tree.get_children())
        self.delete_button.state(['disabled'])

    def select_directory(self):
        value=filedialog.askdirectory(parent=self,initialdir=self.directory.get())
        if value:self.directory.set(value);self.plan=[];self.delete_button.state(['disabled'])

    def run(self,operation):
        self.busy=True
        for widget in (self.entry,self.choose,self.scan_button,self.delete_button):widget.state(['disabled'])
        def task():
            try:self.events.put(operation())
            except Exception as exc:self.events.put(('error',str(exc)))
        threading.Thread(target=task,daemon=True).start()

    def scan(self):
        directory=self.directory.get()
        self.status.set('正在读取音频时长……' if self.short else '正在按文件大小、SHA-256 和音频有效性检查重复……')
        self.run(lambda:('scan',(find_short_audio if self.short else find_duplicates)(directory)))

    def delete(self):
        if not self.plan or self.busy:return
        label = '不足 90 秒的音频' if self.short else '重复副本'
        if not messagebox.askyesno('确认永久删除',f'将永久删除 {len(self.plan)} 个{label}，不进入回收站。\n删除前会再次核验文件，详情见清单。\n确认删除？',parent=self):return
        report=self.parent.state_path.parent / (('short-audio-' if self.short else 'duplicates-')+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.jsonl')
        plan=list(self.plan)
        self.status.set('正在复核并删除重复文件……')
        self.run(lambda:('delete',(delete_short_audio if self.short else delete_duplicates)(plan,report),str(report)))

    def poll(self):
        try:
            event=self.events.get_nowait()
            self.busy=False
            for widget in (self.entry,self.choose,self.scan_button):widget.state(['!disabled'])
            self.tree.delete(*self.tree.get_children())
            if event[0]=='scan':
                self.plan,errors=event[1]
                for item in self.plan:self.tree.insert('', 'end',values=(item['keep'],item['delete'],f'{item["size"]/1024**2:.2f}'))
                label='不足 90 秒的音频' if self.short else '内容完全一致的副本'
                self.status.set(f'发现 {len(self.plan)} 个{label}；扫描警告 {len(errors)} 条。'+(' '+errors[0] if errors else ''))
                if self.plan:self.delete_button.state(['!disabled'])
            elif event[0]=='delete':
                deleted,errors=event[1]
                if self.short:
                    mark_deleted_short(self.parent.tracks,self.parent.state_path.parent)
                    self.parent.refresh();self.parent.autosave()
                self.plan=[];self.changed=bool(deleted) or self.changed
                self.status.set(f'已删除 {len(deleted)} 个文件；未删除/异常 {len(errors)} 个。清单：{event[2]}'+(' '+errors[0] if errors else ''))
            else:self.plan=[];self.status.set(event[1])
        except queue.Empty:pass
        self.timer=self.after(100,self.poll)

    def close(self):
        if self.busy:return
        self.after_cancel(self.timer)
        self.destroy()
        if self.changed:self.parent.start(True)
