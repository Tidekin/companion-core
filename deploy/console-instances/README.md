# 两套主人控制台的只读总览

在主实例打开 `/instances/`，用各实例原有的主人会话查看状态。两台控制台须使用同一站点下的两个 HTTPS 子域，以沿用 host-only、SameSite=Strict 的登录 Cookie。操作链接打开该实例原控制台，返回总览会重新读取状态。

1. 在两台 TLS 入口的私有静态目录（示例 `/srv/companion-instances`）安装本目录的文件。该目录只按下列 location 对外映射，只有 `assets/` 可以整目录公开。按站点现有策略配置 `headers.conf` 的 HSTS。
2. 将模板里的 `https://hub.example.test` 和 `https://peer.example.test` 替换为主、次实例的精确 HTTPS 源，并将示例路径替换为实际目录。实例名和入口链接在 `index.html`。
3. 在两个已有的控制台 TLS server 中 include `card.nginx.conf`，仅主实例再 include `hub.nginx.conf`。已有的 `/v1/console/` 必须指向各自的 Core。使用带 http_auth_request_module 的 Nginx，通过配置检查后热重载。移除新 include 并热重载即可回退。
4. 用无登录窗口访问入口应提示登录；两边各自登录后应显示两份状态。退出其中一个实例，其余实例的数据仍按各自主人的会话读取。每张卡只从自己的源获取 `GET /v1/console/overview`。

主入口复用现有 overview 的主人校验。只有两张只读卡片允许被主入口嵌入，完整控制台沿用已有的防嵌入策略。[Nginx 子请求授权](https://nginx.org/en/docs/http/ngx_http_auth_request_module.html) · [CSP frame-ancestors](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/frame-ancestors)
