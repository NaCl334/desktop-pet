# 构建与验收

建议 Python 3.11 x64，带完整 Tcl/Tk。先安装 `requirements-build.txt`。

本仓库不携带第三方二进制。仅在本地开发和许可允许范围内，将 `requirements-live2d.txt` 安装到 `runtime/live2d`：

```powershell
python -m pip install -r requirements-live2d.txt --target runtime/live2d
```

构建脚本的自动探测优先使用 Conda Tcl/Tk 布局，可用 `-PythonPath` 指定符合脚本布局的 Python。普通 python.org 安装可运行源码，但现有打包脚本未兼容所有 Tcl/Tk 目录布局。

另从 7-Zip 官方 26.05 Extra x64 归档获取 `x64/7za.exe`，放到 `assets/setup/7zip/7za.exe`；不提交此文件。上游来源及 SHA256 见同目录 SOURCE.txt。构建前核对 SHA256；不得用来源未知的解压器替换。

```powershell
.\scripts\build_desktop_pet.ps1 -PythonPath '<带完整Tcl/Tk的python.exe>'
# 验收候选 EXE 后，按脚本约定将其命名为项目根的 我的桌宠.exe
.\scripts\build_release.ps1 -OutputPath 'release/我的桌宠_v0.1.0_安装版' -Zip
```

源码测试入口（无云端请求）：

```powershell
python -m unittest scripts.test_emotion_catalog scripts.test_live2d_resource_scope scripts.test_history_memory scripts.test_lgpl_ffmpeg
```

FFmpeg 下载固定到上游构建与 SHA256。历史 daily release 可能被上游清理；如果不可下载，应更新并复验固定清单，不能跳过校验或静默改用 GPL 构建。

发布前必须：确认 Live2D 发布许可；填写 MIT 版权人；用完整源码的候选 EXE 重新打包；审查配置、外部运行资源与 EXE 内部资源；记录 ZIP SHA256。不得沿用旧 Git 历史。

## 路线 B 的两种本体

```powershell
.\scripts\build_desktop_pet.ps1 -WithoutLive2D -OutputPath '<项目根目录/候选基础版.exe>'
.\scripts\build_release.ps1 -WithoutLive2D -Executable '<候选基础版.exe>' -OutputPath 'release/基础版' -Zip
# 含 Live2D 的本地候选：去掉以上两处 WithoutLive2D
```

基础版构建排除 live2d、glfw、OpenGL，不复制 runtime/live2d；发布脚本检查 EXE 内部归档，若仍含组件则拒绝制作基础版。

新增验收：`python -m unittest scripts.test_signed_character_packages scripts.test_character_package scripts.test_setup_installation scripts.test_lgpl_ffmpeg`。

可无窗口验证已签包：`我的桌宠.exe --verify-character-package <包目录> <安装目录/temp/报告.json>`；报告包含验签结果和本机 Live2D 组件状态。

## 路线 B 的两种本体

```powershell
.\scripts\build_desktop_pet.ps1 -WithoutLive2D -OutputPath '<项目根目录/候选基础版.exe>'
.\scripts\build_release.ps1 -WithoutLive2D -Executable '<候选基础版.exe>' -OutputPath 'release/基础版' -Zip
# 含 Live2D 的本地候选：去掉以上两处 WithoutLive2D
```

基础版构建排除 live2d、glfw、OpenGL，不复制 runtime/live2d；发布脚本检查 EXE 内部归档，若仍含组件则拒绝制作基础版。

新增验收：`python -m unittest scripts.test_signed_character_packages scripts.test_character_package scripts.test_setup_installation scripts.test_lgpl_ffmpeg`。

可无窗口验证已签包：`我的桌宠.exe --verify-character-package <包目录> <安装目录/temp/报告.json>`；报告包含验签结果和本机 Live2D 组件状态。

纯源码不附带 7za.exe。解压验收需按前文准备并校验该工具，或设置 DESKTOP_PET_TEST_7ZA 指向已校验的外部副本；其 SHA256 在安装逻辑中仍会校验。本机角色素材检查在纯源码环境跳过，通用签名和行为测试照常执行。
