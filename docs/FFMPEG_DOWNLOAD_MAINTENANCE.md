# FFmpeg 下载版本维护

安装器固定使用官方 LGPL 月末构建，不跟随可变的 latest。
当前版本：autobuild-2026-10-31-12-59，win64-lgpl。

上游当前保留规则：最近14份日构建；每个月最后一份构建保留两年。
因此月末构建仍需要维护，并非永久下载地址。计划在2028-09-30前复核更新。

更新时应核对上游保留策略和对应月末资产，取得官方digest后实际下载，验证ZIP、
许可证和构建标志，重新记录整包与ffmpeg/ffprobe的SHA256，完成回归及打包安装验证。
配置位于core/lgpl_ffmpeg.py。不可只更换URL而保留旧哈希，也不可跳过哈希校验。

官方API与直链共用续传缓存。网络不可达时应检查网络或代理再重试；
文件被删除时需要维护者更换已验证版本，切换网络不能解决失效链接。
本源码和常规发布ZIP不附FFmpeg二进制，也不托管第三方下载镜像。

官方规则：https://github.com/FFmpeg-Builds/FFmpeg-Builds#release-retention-policy
固定发布：https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/tag/autobuild-2026-10-31-12-59