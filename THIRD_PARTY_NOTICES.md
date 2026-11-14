# 第三方组件声明

MIT 仅覆盖本项目原创代码。第三方软件、模型、素材均保留原许可。本仓库不提供角色素材和角色语音模型。

| 组件 | 许可与范围 |
| --- | --- |
| GPT-SoVITS | MIT，Copyright (c) 2024 RVC-Boss；上游环境由使用者选择下载，完整许可见 licenses/GPT-SoVITS-MIT.txt。 |
| BigVGAN | MIT，Copyright (c) 2024 NVIDIA CORPORATION，见 licenses/BigVGAN-MIT.txt。 |
| FFmpeg-Builds 的 LGPL 构建 | LGPLv3，见 licenses/LGPL-3.0.txt 与 licenses/GPL-3.0.txt；使用独立进程。安装时从上游下载并校验，不在源码或桌宠 ZIP 中附带 FFmpeg 二进制。 |
| live2d-py 1.0.0 | Python 封装 MIT，Copyright (c) 2024 Arkueid，见 licenses/live2d-py-MIT.txt；内含 Cubism 原生部分不由此改为 MIT。 |
| Live2D Cubism SDK 5-r.5 | 专有 SDK 条款及适用的 Open Software License；本源码仓库不携带 SDK 原生库。官方发行版限制为签名角色包；这一方案的应用分类及 SDK 分发条件仍需 Live2D 官方确认。 |
| PyOpenGL / GLFW / freeglut / GLE | 各自许可分别保留；freeglut 与 GLE 声明附在 licenses/。运行时二进制不进入源码仓库。 |
| Python / Tcl / Tk | 各自 PSF、Tcl/Tk 条款，不能统一视为同一个许可证。 |
| Pillow / NumPy | 各自开源许可，二进制发布随依赖保留对应声明。 |
| cryptography | Apache-2.0 / BSD；Ed25519 签名校验，见 licenses/cryptography/。 |
| PyInstaller | GPL 加 bootloader 例外；不把本程序整体改为 GPL，也不是 PSF 许可。 |
| 7-Zip Extra 26.05 | LGPL 及相应 BSD 条款；原始声明和来源位于 assets/setup/7zip/，二进制仅供本地构建/发布包。 |

上游入口：
- GPT-SoVITS: https://github.com/RVC-Boss/GPT-SoVITS
- BigVGAN: https://github.com/NVIDIA/BigVGAN
- FFmpeg: https://ffmpeg.org/legal.html
- FFmpeg-Builds（原 BtbN/FFmpeg-Builds，现有地址会重定向）: https://github.com/FFmpeg-Builds/FFmpeg-Builds
- live2d-py: https://github.com/Arkueid/live2d-py
- Cubism SDK: https://www.live2d.com/en/sdk/license/
- 可扩展应用: https://www.live2d.com/en/sdk/license/expandable/
- PyInstaller: https://pyinstaller.org/en/stable/license.html

上游整合包可能包含更多依赖和基础模型，其各自许可应保留，不能用本文件替代上游全部声明。商用或再次分发模型前需另行确认模型和素材许可。
