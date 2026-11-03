# 字体清晰度与显示缩放

启动时先设置 Windows DPI 感知，再创建 Tk 窗口，避免由 Windows 将低分辨率整窗位图放大。优先使用每显示器 DPI 感知 v2；较旧系统依次回退。若 Python 宿主或程序清单已经指定 DPI 模式，保留已有模式，不更改系统显示设置、兼容性设置或 ClearType。

字体按实际已安装字体选择：Windows 优先 Microsoft YaHei UI，macOS 优先 PingFang SC，Linux 优先 Noto Sans CJK SC，缺少时使用可用字体或 Tk 原生默认字体。代码预览另选等宽字体。字体大小使用 point，不重复乘显示缩放；行高、间距等像素尺寸使用同一显示比例。

Windows 的 150% 缩放对应 144 DPI，Tk 的换算单位是每 point 的像素数，因此应设置为 144/72 = 2.0，而非 1.5。macOS 和 Linux 保留 Tk 原生缩放值。

## Windows 验证清单

本次修复已运行模拟 Windows API 与字体回退的自动测试，但开发环境不是 Windows，尚未完成原生 Windows 像素清晰度与跨显示器测试。

1. 完整退出旧程序，用 `py -3 main.py --demo` 重启新版本。
2. 分别在系统显示缩放 100%、125%、150%、200% 下检查中文、英文、菜单、表格行、输入框与对话框是否清晰且不裁切。
3. 在不同缩放比例的显示器之间移动窗口，并检查弹出对话框。Tk 8.6 不保证已创建控件动态调整尺寸；若跨屏后大小不合适，请在目标显示器重启程序。此版本不宣称完整支持动态跨屏缩放。
4. 若仍模糊，记录 Windows 缩放比例，以及 `py -3 -m tkinter` 测试窗口中显示的 Tcl/Tk 版本，提供原尺寸截图。截图被聊天工具或图片查看器缩放时也可能显得模糊。

未来若打包 `.exe`，应在程序清单中声明 DPI 感知，而非依赖源代码启动器。

## 依据

- [Microsoft：SetProcessDpiAwarenessContext](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setprocessdpiawarenesscontext)
- [Microsoft：GetDpiForWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getdpiforwindow)
- [Tk：scaling 单位与动态缩放限制](https://www.tcl-lang.org/man/tcl8.6/TkCmd/tk.htm)
