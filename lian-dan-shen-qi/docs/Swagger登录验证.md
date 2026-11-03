# 在 /docs 中验证注册、登录、当前用户和退出

## 启动与打开

在已激活的 langchain 环境中进入项目根目录：

```powershell
cd E:\code\LangChian_New
python -m app.database --login-session-schema
python -m uvicorn app.main:app --reload
```

建表命令只确认登录记录表存在，不清空用户和聊天记录。MySQL 连接密码由后端环境配置读取。

如果 Uvicorn 已在运行，刷新 `/docs`；若接口仍显示旧结构，重新启动后端。不要在相同端口重复启动第二个服务。

打开 http://127.0.0.1:8000/docs 。

## 先准备一个已注册账号

已有账号可直接登录。没有账号时，展开 `POST /auth/register`，点击 **Try it out**，填写测试账号：

```json
{
  "username": "demo_user_1022",
  "password": "Test1234"
}
```

点击 **Execute**。查看 **Server response** 的实际结果，不能把 **Example Value** 当成已经调用成功。

- `201`：注册成功，保存此次使用的用户名和密码。
- `409`：用户名已存在，可换一个测试名称；如果知道该账号密码，也可以直接登录。
- `400`：用户名或密码不符合要求；注册密码为 8～32 个字符。

上面的账号和密码是手工测试示例，代码不会自动创建这个账号。

## 验证登录

展开 `POST /auth/login`，点击 **Try it out**，填写刚才同样的用户名和密码，然后点击 **Execute**。

成功时实际状态码应为 `200`，返回结构如下，Token 与用户 ID 以实际响应为准：

```json
{
  "access_token": "服务端生成的随机凭证",
  "token_type": "bearer",
  "expires_in": 259200,
  "user": {
    "user_id": 1,
    "username": "demo_user_1022"
  }
}
```

`expires_in` 的单位为秒，259200 秒即 3 天。原始 Token 返回给当前客户端，数据库只保存它的哈希。每次成功登录都会新增一条登录记录。

响应头 `Cache-Control: no-store` 表示含凭证的响应不应被缓存；`X-Request-ID` 用于在服务端日志中定位本次请求。

## 顺手测试异常情况

| 操作 | 预期实际状态码 |
|---|---|
| 已注册用户名、正确密码 | 200，返回 Token 和用户信息 |
| 相同用户名、错误密码 | 401，用户名或密码错误 |
| 未注册用户名 | 401，与错误密码使用同一提示 |
| 删除请求 JSON 中的 password 字段 | 422，提示字段缺失 |
| password 改为对象，例如 `{}` | 422，提示字段格式不正确 |
| 同一个账号成功登录两次 | 两次均为 200，但返回的 Token 不同 |

正常密码错误时不能新增登录记录。遇到 500 或 503 时，复制响应头 `X-Request-ID` 到 `logs/` 中查找，不需要把密码或 Token 发给别人。

## 使用 Authorize 验证当前用户

1. 从 `/auth/login` 实际响应中复制 access_token。
2. 点击页面上的 **Authorize**，只粘贴 Token 本身，不添加 `Bearer `。
3. 确认授权后，展开 `GET /auth/me`，点击 **Try it out → Execute**。
4. 实际响应应为 200，返回刚才登录账号的 user_id 和 username。

Authorize 只是让 Swagger 在后续受保护请求中带上请求头，凭证是否有效仍由服务器检查。输入一个编造的 Token，应返回 401。

在授权弹窗中点击 Logout 会清除 Swagger 的凭证输入，之后不携带 Token 请求 `/auth/me` 应返回 401。这个按钮不会撤销服务器保存的 Token。

## 验证服务器退出登录

1. 登录并在 Authorize 中填入实际 Token，先确认 `/auth/me` 返回 200。
2. 保留授权输入，执行 `POST /auth/logout`，应返回 204，Response body 为空。
3. 保留同一 Token，再执行 `/auth/me`，应返回 401。
4. 再执行 `/auth/logout`，也应返回 401。
5. 重新登录取得新 Token，更新 Authorize 后，`/auth/me` 应重新返回 200。

退出只影响调用时携带的 Token；同一账号另外一次登录和其他账号均不受影响。HTTP 204 是没有响应正文的成功响应，不需要看到一段 JSON。

## 这一步验证到了哪里

200 且返回 Token 表示“密码验证 → 创建凭证 → 保存登录记录 → HTTP 返回”这条链路执行成功。错误密码返回 401 则验证了拒绝分支。

`/auth/me` 已接入对客户端 `Authorization: Bearer ...` 的校验，包括到期与撤销检查。其他业务接口仍需按各自需求接入身份和归属校验。过期与撤销场景使用独立临时 MySQL 库验证，不清空现有业务数据。
