# 我的桌宠

面向 Windows 的桌面角色陪伴应用。支持静态立绘角色、对话驱动的情绪与动作、独立聊天记录、任务提醒，以及可选的本地语音合成。

当前版本：**0.1.0 公开测试版**

## 下载

**[下载 v0.1.0 基础版](https://github.com/NaCl334/desktop-pet/releases/latest)**

- 系统要求：Windows 10 / 11 x64
- 解压即用，双击 `我的桌宠.exe`，按向导选择安装位置和运行方式
- 本版为**基础版**，不含 Live2D 组件；Live2D 渲染支持开发中



## 功能

- 静态立绘角色：自由新建、导入，支持多服装、多动作与情绪切换
- 对话驱动：接入兼容 OpenAI Chat Completions 协议的接口，由回复内容驱动表情和动作
- 独立聊天记录：每个角色一份持续对话，重启后继续
- 任务提醒
- 可选本地语音：GPT-SoVITS v2Pro，在本机合成
- 可调整快捷键、置顶、截图等操作

## 开始使用

1. 解压下载的 ZIP，双击 `我的桌宠.exe`
2. 向导中选择安装位置与运行方式：
   - **仅使用桌宠**：不下载任何模型，先跑起来
   - **桌宠与本地语音**：额外下载 GPT-SoVITS 整合包（约 8.2–8.9 GB）
3. 进入后添加角色，再在设置中配置模型接口

## 角色素材

本仓库和安装包**不提供任何游戏角色、立绘、Live2D 模型、声音模型、参考音频或演示角色包**。角色需自行准备并导入。

静态角色建议使用透明背景全身 PNG。同一角色各图保持相同画布尺寸、比例和人物位置；推荐 2:3。局部显示直接从全身图截取，无需另备半身图。

```text
角色文件夹/待机.png                         # 单服装
角色文件夹/服装1/待机.png                   # 多服装
角色文件夹/服装1/动作1/待机.png             # 同服装多动作
```

图片按界面所列情绪命名；先查看常用情绪，再添加所需项目。

Live2D 仅加载通过内置公钥校验的官方签名角色包，基础版不包含该组件。详见 [角色包与签名](docs/CHARACTER_PACKAGES.md)。

## 模型接口

在设置 → 模型配置 → 对话模型（接口）中填写兼容 OpenAI Chat Completions 协议的地址、模型名和密钥，测试连接后保存。

- 接口费用由服务商收取，本项目不提供接口或密钥
- 对话和视觉请求会发送到你配置的服务商
- 语音合成在本机进行，不经过外部服务

## 语音与语言

角色包可包含已配置好的 GPT-SoVITS 权重和参考音频，导入后按包内配置恢复；不包含整套语音运行环境。

自行添加声音时，需要训练或寻找有权使用的模型文件及对应参考音频、参考文本。先确认模型支持中文、日文或中日双语；不支持的语言效果可能不理想。缺少中文参考音频时不能切换到中文。

## 运行环境

- 目标环境：Windows 10/11 x64；其他系统不在本次发布验收范围内
- 基础桌宠不需要下载整套语音环境
- 可选 GPT-SoVITS v2Pro 整合包约 8.2–8.9 GB，安装需至少 32 GiB 可用空间；下载与解压耗时取决于网络和磁盘
- 有可用 CUDA 时使用 GPU，否则使用 CPU；CPU 合成可能较慢
- 不会安装显卡驱动或修改系统 Python

## 从源码运行

使用带 Tcl/Tk 的 Python 3.11 x64：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config/app_config.example.json config/app_config.json
.venv\Scripts\python.exe main.py
```

可选 Live2D 依赖与构建说明见 [docs/BUILD.md](docs/BUILD.md)。角色、运行数据和本机配置不会提交到仓库。

## 隐私与许可

不要把密钥、聊天记录、截图或角色声音提交到 Issue 或仓库。备份角色与本地资料后再卸载。

原创程序代码使用 [MIT](LICENSE) 许可证。第三方组件不因此改为 MIT，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

本项目使用 GPT-SoVITS（MIT License, Copyright (c) 2024 RVC-Boss）。

Live2D Cubism SDK 归 Live2D Inc. 所有，受其专有条款约束。在相关许可确认前，本仓库不包含 SDK 文件，公开发布的二进制也不包含 Live2D 组件。
