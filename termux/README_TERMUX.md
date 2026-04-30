# 媒体中心·Termux 部署说明

## 0. 先决条件（在安卓宿主上）
- 安装 **Termux**（F-Droid 版），打开一次。
- 设置→电池→Termux：**允许后台活动、不受限制**。
- 打开 **个人热点**（安卓宿主侧做 AP）；记下网关 IP，通常 `192.168.43.1`。

## 1. 拷贝文件到安卓宿主
把本工程目录放到安卓宿主内部存储 `VerPadProxy/` 下，目录结构：

```
内部存储/VerPadProxy/
├── scripts/
│   ├── redirect_addon.py
│   ├── user_auth.py
│   ├── pdf_page_render.py
│   └── termux/               ← 本目录（setup.sh / start.sh / env.sh / 本 README）
├── payload/
│   ├── PDF/
│   ├── 视频/
│   ├── 音乐/
│   ├── private/
│   └── upl/                  ← 上传会写入这里
└── data/                     ← 首次启动自动生成：mitm_users.json 等
```

> 注意：**scripts 里必须同时有 `redirect_addon.py` 和 `user_auth.py`**，因为前者 `import user_auth`。

## 2. 首次安装（Termux 里一次性执行）

```bash
termux-setup-storage        # 弹框允许存储权限
cd ~/storage/shared/VerPadProxy/scripts/termux
bash setup.sh
```

`setup.sh` 会：
1. 装 `python`、`mitmproxy`、`clang` 等；
2. 尝试装 `PyMuPDF`（可选，只影响 `/pdf.png`）；
3. 建好 `VerPadProxy/payload` 下的分区与 `data/` 目录。

## 3. 日常启动

```bash
cd ~/storage/shared/VerPadProxy/scripts/termux
bash start.sh
```

屏幕上会把**当前网卡 IP**、监听端口、各目录路径都打印出来。

## 4. 客户端平板 连过来
1. 客户端平板 连上安卓宿主热点；
2. 打开 WiFi 设置 → 修改这个网络 → 代理 **手动**；
3. 主机填 **安卓宿主端热点网卡 IP**，端口填 **2345**；
4. 浏览器打开任意 `http://xxx/`（因为我们默认 `MITM_REDIRECT_HOSTS=*`，任何 Host 都会被改写为媒体中心）；
5. 如果需要 HTTPS 场景也走脚本：把 `~/.mitmproxy/mitmproxy-ca-cert.cer` 传给 客户端平板，在 客户端平板 里导入为「VPN 与应用」证书。

## 5. 常用自定义
编辑 `termux/env.sh`：
- 只劫持某个站点：`MITM_REDIRECT_HOSTS="example.com:8080"`；
- 改端口：`MC_LISTEN_PORT=2345`；
- 换媒体目录：`MITM_SHARE_DIR=/sdcard/我的媒体/payload`。

## 6. 首次登录
- 启动后打开任意页面→跳到 `/__login`；
- 账号 **admin**，默认密码 **change-me-please**。强烈建议首次启动前设置 `MITM_BOOTSTRAP_PASSWORD=你的强密码`。

## 7. 保活与自启（可选）
- **保活**：`start.sh` 已调用 `termux-wake-lock`；不要手滑清后台即可。
- **开机自启**：装 **Termux:Boot**，然后建文件
  ```bash
  mkdir -p ~/.termux/boot
  cat > ~/.termux/boot/verpadproxy <<'EOF'
  #!/data/data/com.termux/files/usr/bin/env bash
  bash ~/storage/shared/VerPadProxy/scripts/termux/start.sh
  EOF
  chmod +x ~/.termux/boot/verpadproxy
  ```
  重启安卓宿主即可。

## 8. 故障自检
| 现象 | 排查 |
|------|------|
| 客户端平板 打任何页面都走原站、没进媒体中心 | `MITM_REDIRECT_HOSTS` 是否为 `*` 或包含目标 Host；客户端平板 代理是否生效 |
| HTTPS 页面提示证书不受信 | 未安装 mitmproxy CA，或 客户端平板 开了 VPN/私有 DNS |
| 启动直接报 `ModuleNotFoundError: mitmproxy` | 再跑一次 `setup.sh`；或在 Termux 里 `python -m pip install mitmproxy` |
| PDF 预览正常、翻页时缩略图空白 | `PyMuPDF` 未装；`setup.sh` 里那一步重装 |
| 端口 2345 被占 | `MC_LISTEN_PORT` 换个别的，重启 |
| Termux 被省电杀 | 再次确认「允许后台活动」；用 Termux:Boot + wake-lock |
