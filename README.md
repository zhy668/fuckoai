# fuckoai Linux

Linux 容器版注册控制面板。

## 功能

- Web 控制面板：`/ui`
- 邮箱列表导入与 Gmail IMAP 收件
- 本地 Mock 注册任务目标，默认指向 `http://127.0.0.1:8088`，不会访问真实注册站点- 购买组可视化配置，保存到本地 `data/purchase_config.json`
- Linux 图形浏览器自动注册入口，通过 Xvfb、x11vnc、noVNC 查看

## 文件结构

```text
server.py                  # 本地 API 和 Web 控制面板服务
control_panel.html         # Linux Web 控制面板
uc_signup.py               # Linux 浏览器自动注册脚本
config.example.json        # 应用配置模板
config.json                # 本地应用配置，不进入 git
Dockerfile                 # Linux 容器镜像
docker-compose.yml         # fuckoai 服务
scripts/start_linux_vnc.sh # Xvfb/VNC/noVNC + server 启动脚本
```

运行数据放在 `data/`，`.env`、`config.json` 和 `data/` 不进入 git，也不进入 Docker build context。

## 配置

`.env` 只放管理员密码：

```env
ADMIN_PASSWORD=你的控制面板管理员密码
```

`ADMIN_PASSWORD` 可选；设置后访问 `/ui` 需要登录。

其他设置写在本地 `config.json`，也可以在控制面板“设置”页保存。首次部署可以从模板创建：

```bash
cp config.example.json config.json
```

模板已包含 HeroSMS 接口地址、注册资料默认值、浏览器参数和 Gmail IMAP 非敏感配置；接口密钥、CPA 等用户配置默认为空。Gmail 应用专用密码不写入 `config.json`，只从环境变量 `GMAIL_APP_PASSWORD` 读取。

Gmail 示例配置：

```env
ADMIN_PASSWORD=你的控制面板管理员密码
GMAIL_APP_PASSWORD=abcdefghijklmnop
```

并在控制面板“应用配置”中确认：

```text
SIGNUP_TARGET_URL=http://127.0.0.1:8088/auth/login?intent=signup
SIGNUP_MOCK_MODE=true
```

Mock 模式只进行本地目标配置和 Gmail 邮件轮询，不购买号码、不访问真实外部注册站点。


## 购买配置

购买参数统一维护在控制面板“设置”页，保存后写入 `data/purchase_config.json`。该文件位于 `data/`，不会进入 git。

默认仓库不提供具体国家、运营商、价格等购买组。首次使用前需要在控制面板新增购买组。

服务端会按已启用购买组顺序尝试买号，失败时自动试下一组。

## 启动

```bash
docker compose up -d --build fuckoai
```

访问：

```text
http://127.0.0.1:3030/ui
```

查看容器：

```bash
docker compose ps
docker logs --tail 80 fuckoai
```

## Linux 本地运行

```bash
python3 server.py
```

如果需要浏览器画面：

```bash
./scripts/start_linux_vnc.sh
```

## 邮箱列表导入与 Gmail 收件

控制面板支持导入 TXT/CSV 邮箱列表和 Gmail IMAP 收件。旧 Temp Mail 配置入口已移除；`@icloud.com` 地址可以作为已有邮箱直接导入，系统不会创建 iCloud 邮箱，也不会替你配置 iCloud 转发。

在 iCloud 中把这些地址的邮件转发到同一个 Gmail 后，服务端可以通过 Gmail IMAP 查询最新转发邮件：

1. 在 iCloud 侧完成并验证转发规则。
2. Gmail 账号开启两步验证，创建一个仅用于本服务的应用专用密码。
3. 在服务端环境变量设置 `GMAIL_APP_PASSWORD`，在控制面板填写 `GMAIL_USERNAME`，保存后点击“测试 Gmail IMAP”。
4. 在邮箱列表中选择/填写目标 iCloud 地址，点击“从 Gmail 查邮件”。接口会在最近邮件的头部和正文中匹配该地址；如果转发后的邮件没有保留原始 iCloud 地址，无法可靠区分多个地址，此时应使用 Gmail 标签/文件夹或其他明确的转发标记。

应用专用密码只适用于开启两步验证的账号；它不是普通 Gmail 密码，也不是 iCloud 密码。Google 更推荐 OAuth；Google Workspace 还可能受管理员策略限制。不要把应用专用密码提交到 Git 或写进前端页面。


## API

基础地址：

```text
http://127.0.0.1:3030/api
```

常用接口：

- `GET /api/health`
- `POST /api/purchase`
- `GET /api/purchase-settings`
- `POST /api/purchase-settings`
- `GET /api/email-queue`
- `POST /api/email-queue`
- `POST /api/email-queue/generate`
- `POST /api/gmail/test`
- `GET /api/gmail/mail/latest?address=someone@icloud.com`
- `GET /api/uc-signup/status`
- `POST /api/uc-signup/start`
- `POST /api/uc-signup/stop`
- `GET /api/uc-signup/logs`

## 致谢

感谢 linux.do 社区提供的交流、经验和启发。
