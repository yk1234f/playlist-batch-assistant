# 歌单批量下载助手 v2

Windows 桌面程序：导入 TXT / CSV 歌单，扫描本地音乐，双重去重，匹配歌名、歌手与版本，自动从煎饼搜页面使用的公开接口下载并校验音频。

## 使用

从 GitHub Releases 下载 `PlaylistBatchAssistant-Windows.zip`，**解压整个文件夹**，双击 `PlaylistBatchAssistant.exe`。不需要安装 Python 或浏览器插件。

1. **导入歌单**：支持多个 TXT / CSV；TXT 格式是 `歌名 - 歌手`，PlaylistOut 格式为 `歌名 - 歌手 - 专辑`；CSV 至少有 `title,artist` 或 `歌名,歌手` 两列。
2. **自动识别**：识别 Windows 的音乐、下载目录（包括系统重定向目录）。其他磁盘或自定义文件夹，用 **选择目录** 多次添加。不会自动遍历所有硬盘。
3. **选择下载目录**，点 **重新扫描**。下载目录始终参与去重；本地文件优先读取音频标签，无标签时识别 `歌名 - 歌手` 和 `歌手 - 歌名` 文件名。
4. 点 **开始自动下载**。顺序尝试网易、QQ、酷狗、酷我、咪咕。每个音源先搜歌名加歌手，再尝试只搜歌名。
5. 可以 **暂停 / 继续**、**跳过**、**停止**，或 **打开当前页面**核对。失败和不确定项保留原因；点击 **重试失败 / 待确认** 会把失败、待确认和手动跳过项重新排队。

原项目的 `playlists` 文件夹保留在本地，可直接从中导入；仓库与发行包不包含个人歌单、歌曲或登录凭据。

## 匹配与去重

- 歌单内统一大小写、全角半角、标点和多歌手顺序后去重。
- Live/现场、Remix/混音、伴奏、Acoustic、不插电、Demo、Cover、Remaster 和速度/音调标记保持区别。不同演出副标题和不同 Remix 名称保留。
- 歌名与歌手的相似度分别展示。**自动下载要求归一化后的歌名、完整歌手集合与版本一致**；近似但不一致的候选进入“待确认”，避免同名翻唱、Live/录音室版本混淆。
- 本地已有文件须通过音频检查才跳过。成功下载会立刻更新本轮索引；下次运行重新扫描，不盲信上次的“成功”状态。
- 无法从标签或文件名可靠识别的文件不会被武断判为已存在；特殊歌手别名、繁简差异和没有清晰分隔符的文件名可能需要手动核对。

## 文件成功的判定

默认最小 **256 KiB**（界面可调）、时长至少 **10 秒**。先下载到随机 `.part` 文件，然后检查：

- HTTP 状态、大小上限 512 MiB、声明与实际长度是否一致；
- MIME 与文件内容是否是 HTML、JSON 或其他文本错误页；
- 真实文件头、对应音频扩展名，以及 Mutagen 能否解析有效音频时长；
- WAV 声明长度是否完整。

校验通过后才将临时文件原子发布为正式文件并记录“成功”。扩展名由内容识别，网站误写 `.mp3` 的 FLAC 会保存为 `.flac`。同名文件不会覆盖；失败/取消清理本次临时文件。

文件校验不能证明音源标注一定正确，也不是逐帧解码检测。网站没有可靠总时长时，无法仅凭文件大小证明不是超过 10 秒的试听片段。

## 队列与保存

每次状态变化自动保存到 `%LOCALAPPDATA%\PlaylistBatchAssistant\session.json`。扫描异常在同目录 `scan_warnings.txt`，也可手动保存会话或导出 CSV 日志。兼容原 v1 会话的歌曲字段；运行中的项目恢复为待处理。

扫描和网络请求在工作线程执行，界面保持响应。暂停/停止/跳过在请求前后与下载分块之间生效；正在阻塞的网络请求最多等待 25 秒超时。每次请求间隔至少 2 秒，单首下载最多 180 秒。不会无上限重试。

## 真实网站验证边界（2026-09-19）

已读取首页和 `static/js/music.js?v=1.7.9`，核对搜索使用 `POST /`，字段为 `input/filter/type/page`，结果字段为 `code/data/name/artist/url`。下载使用页面提供的普通 HTTP/HTTPS 链接。

本次在线检查中，“寓言”“寓言 张韶涵”“周杰伦”（QQ）能返回 10 条候选，“雪落下的声音 林俊杰”和“不要说话”的已测音源返回接口业务码 `404`、没有相关信息。《寓言》歌名与歌手精确匹配，但下载地址多次返回 HTTP 403 或非音频内容；正常系统网络环境下也未通过音频校验。因此本次以 **预发布版** 交付，**未完成真实站点歌曲下载成功验证**。这不等于证明所有用户或所有时刻都会失败；网站音源异常、失效链接、限制或接口变化会被记录为失败，不能保证每首都能下载。

程序不解密 DRM、不处理验证码、不绕过登录/付费限制；仅处理网站正常返回的下载地址。请用于你有权保存的音频。

## 源码运行 / 测试 / 打包

Python **3.12+**（本次验证使用 3.13）:

```powershell
python -m pip install -r requirements.txt
python app.py
python -m unittest selftest -v
```

双击 `build_windows.bat` 或运行：

```powershell
python -m pip install pyinstaller
python build.py
```

EXE 内置相同离线回归测试（自动生成 12 秒静音 WAV，启动本地测试 HTTP 服务）：

```powershell
$env:PLAYLIST_TEST_REPORT = "$pwd\packaged-test.txt"
$p = Start-Process .\dist\PlaylistBatchAssistant\PlaylistBatchAssistant.exe -ArgumentList '--self-test' -PassThru -Wait -WindowStyle Hidden
Get-Content $env:PLAYLIST_TEST_REPORT
$p.ExitCode
```

仓库包含 Windows CI，执行源码测试、打包和 EXE 回归并上传发行文件。测试具体证据见 [VALIDATION.md](VALIDATION.md)。

## 模块

`playlist_parser.py` 歌单解析；`matching.py` 匹配；`audio_files.py` 本地扫描和校验；`provider.py` 网站接口；`downloader.py` 下载；`engine.py` 队列；`app.py` 桌面界面；`selftest.py` 离线回归。
