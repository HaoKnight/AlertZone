# 🚨 AlertZone

<div align="center">
    <img src="./AlertZone Server/icon/icon.png" width="128" height="128" alt="AlertZone 图标" />
</div>

<div align="center">
    <p style="font-size: 30px; font-weight: 700; margin: 10px 0 0;">
        AlertZone
    </p>
    <p>本地人体检测 · 局域网监控 · 桌面后台告警</p>
</div>

## 🔍 项目概述

AlertZone 由 AlertZone Server（服务端）和 AlertZone Desktop（客户端）两个
相互配合的项目组成。Server 负责连接摄像头、运行人体检测并提供局域网接口；
Desktop 通过这些接口显示独立原生仪表板，并提供系统托盘、后台报警小窗和
桌面提示音。

推荐先启动 AlertZone Server，再使用浏览器或 AlertZone Desktop 连接 Server
显示的局域网地址。

## 📂 项目入口

### [🖥️ AlertZone Server →](./AlertZone%20Server/README.md)

本地人体检测与局域网服务端。负责摄像头采集、YOLO 人体检测、ByteTrack 临时
跟踪、范围识别、事件截图、实时预览和局域网 HTTP 接口。

适合运行在连接监控摄像头的电脑上。

### [🔔 AlertZone Desktop →](./AlertZone%20Desktop/README.md)

AlertZone 的独立局域网桌面客户端。负责原生监测仪表板、实时预览、系统
托盘后台运行、报警小窗、弹窗位置与大小记忆和自定义提示音。

适合运行在同一局域网中需要接收报警的另一台电脑上。

## 🔗 工作方式

```text
摄像头
  │
  ▼
AlertZone Server
  ├── YOLO 人体检测与 ByteTrack 跟踪
  ├── 区域监控、报警事件和截图
  └── 局域网 Web/API 服务
                  │
                  ▼
         AlertZone Desktop
          ├── 原生监测仪表板
          ├── 后台报警小窗
          └── 桌面提示音
```

## 🚀 快速开始

1. 打开 [AlertZone Server README](./AlertZone%20Server/README.md)，完成安装并
   启动 Server。
2. 在 Server 中启用“局域网连接”，记录显示的局域网地址。
3. 打开 [AlertZone Desktop README](./AlertZone%20Desktop/README.md)，完成安装
   并启动 Desktop。
4. 在 Desktop 中输入 Server 的局域网地址。

## 📁 目录结构

```text
AlertZone/
├── AlertZone Server/       # 摄像头检测与局域网服务
│   └── README.md
├── AlertZone Desktop/      # 局域网桌面前端与后台报警
│   └── README.md
└── README.md               # 双项目总览
```
