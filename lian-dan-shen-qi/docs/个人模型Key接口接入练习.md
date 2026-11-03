# 个人模型Key接口接入练习

## 最新检查结果

两个数据模型和三个路由已填写正确。模型7项、接口16项，以及此前功能合计432项自动回归通过；独立挂载真实路由、真实登录、真实工厂、Fernet和临时MySQL的HTTP联调也通过，临时库已清理。测试只用假Key，没有调用模型。

保存接口return后还留着一行NotImplementedError，不会执行，删除即可。main.py尚未挂载新路由，正式主密钥也没有由助手生成或写入；这两项属于下一步接入。具体修改位置、模块说明和/docs测试顺序见[个人模型Key接口检查与验收.md](个人模型Key接口检查与验收.md)。以下保留原练习供回看。

## 工厂检查结论

你填写的app/api/dependencies.py中get_model_credential_service逻辑正确，没有发现功能错误。读取数据库密码、读取加密主密钥、创建仓储和加密对象、返回业务对象，顺序和参数均正确。

master_password是你自定义的局部变量名，前后保持一致就能正常使用；它实际存的是加密主密钥，不是MySQL密码。若以后改名为master_key只是可读性调整，不属于必须修复的错误。

原6项工厂检查通过。新增依赖失败时不执行路由函数、返回通用503的HTTP测试后，工厂7项已纳入自动回归，项目完整409项通过。真实临时MySQL验证也通过实际工厂、真实主密钥读取、业务、加密和仓储链路。临时主密钥只注入测试进程，真实.env没有修改，临时库已清理，没有调用模型。

## 工厂的执行逻辑

get_mysql_password从后端配置取MySQL密码；get_model_encryption_key读取并验证固定主密钥。ModelCredentialRepository只加载SQL，构造时不连接数据库；ApiKeyCipher创建使用该主密钥的加密对象。ModelCredentialService把二者保存为协作者，之后按每次传入的用户ID执行操作。

@lru_cache(maxsize=1)复用本进程组装成功的服务对象，不缓存用户身份、Token或个人Key。构建失败不缓存，配置修好后能够重试。配置错误由log_failure记录安全日志，再由HTTPException转换成503“请稍后重试。”；依赖解析失败时，路由函数还没有执行。

## 已完成的模型与路由练习

### 先写数据模型

打开app/api/schemas.py末尾，共3个TODO：

1. ModelCredentialRequest：api_key字段，类型SecretStr，必填。
2. ModelCredentialStatusResponse：provider字段，类型str，默认"deepseek"。
3. 同一个响应模型：configured字段，类型bool，必填。

请求模型已经提供extra="forbid"，拒绝正文里的user_id、master_key等额外字段。SecretStr只遮蔽显示，不加密；get_secret_value()显式取得原始字符串，真正的加密由业务层完成。

填写后可先执行：

```powershell
python -m scripts.check_model_credential_api_models
```

这7项检查不连接数据库。未填字段时失败属于预期，不是前面工厂出错。

### 再写路由核心

文件app/api/model_credential_routes.py，已经提供装饰器、依赖、异常转换、日志和提示，你填写7个TODO：

| 方法和地址 | 你要填写的核心 | 成功响应 |
| --- | --- | --- |
| PUT /me/model-credentials/deepseek | get_secret_value取得字符串，再await service.save_api_key | 204，空响应 |
| GET /me/model-credentials/deepseek | await service.is_configured，再创建状态响应模型 | 200，provider和configured |
| DELETE /me/model-credentials/deepseek | await service.delete_api_key | 204，空响应 |

返回204的Response要显式传status_code=204，并设置Cache-Control:no-store、Pragma:no-cache。删除配置已经不存在时也返回204，表示删除后的目标状态已达到，不把业务返回False当故障。

填写对应部分后删除NotImplementedError。业务方法是async，直接await，不再包to_thread。服务工厂是同步组装函数，交给Depends调用，不在这里手动创建或await工厂。

## 身份、依赖和数据从哪里来

current_user由Depends(require_current_user)提供，Token验证成功后才能继续；user_id一律从current_user.user_id取。request来自Pydantic解析后的请求正文；service来自Depends(get_model_credential_service)。这些是对象注入，不是继承。

保存时：取原始Key字符串 → 交给已认证用户对应的业务调用 → 业务加密 → 仓储按该用户写入MySQL → 返回空204，不回传原文或密文。

查看时：查询本人的配置是否存在 → 返回{"provider":"deepseek","configured":true或false}。公开接口只调用is_configured，不能调用内部get_api_key返回秘密。

删除时：仓储仅删除本人DeepSeek配置 → 业务返回True/False → 接口统一空204，不删除用户、聊天或登录记录。

输入错误400；缺少/无效Token401；正文结构错误422；数据库故障或主密钥配置错误503，客户端只看到通用提示；未知异常由现有全局中间件转换为通用500。失败日志不包含提交的Key。

## 本次包和模块的作用

| 对象 | 来源 | 用途 |
| --- | --- | --- |
| logging | Python标准库 | 获取模块日志对象、WARNING级别 |
| lru_cache | Python标准库functools | 工厂复用协作者对象 |
| BaseModel、ConfigDict、SecretStr | 第三方Pydantic | 声明字段、拒绝额外输入、遮蔽秘密显示 |
| APIRouter | 第三方FastAPI | 组合统一路径和三种HTTP方法 |
| Depends | 第三方FastAPI | 提供已认证身份和已组装业务对象 |
| Response、HTTPException | 第三方FastAPI | 返回空204、设置响应头、转换已知错误 |
| MySQLError | 第三方MySQL驱动 | 识别数据库错误，转换成通用503 |
| require_current_user | 项目身份依赖 | 验证登录Token并取得真实用户身份 |
| get_model_credential_service | 项目服务工厂 | 提供协作者组装好的业务对象 |
| ModelCredentialService | 项目业务 | 校验并调度加密和仓储 |
| 两个新数据模型、UserResponse | 项目schemas | 声明请求、公开状态和已确认身份的结构 |
| log_failure | 项目日志模块 | 保存安全的失败位置和异常分类 |

## 写完后的验收用例

| 场景 | 预期 |
| --- | --- |
| 保存合法格式的假Key | 204，无响应正文 |
| 再次保存 | 更新本人的旧配置，仍是204 |
| 查询有配置/无配置 | 200，configured分别为true/false |
| 删除和重复删除 | 都为204 |
| 无Token或无效Token | 401，业务不执行 |
| 正文额外填写user_id或服务器主密钥 | 422 |
| Key为空、含空白或超长 | 业务400 |
| 额外URL参数user_id | 不改变已认证身份 |
| 数据库故障、主密钥配置缺失 | 503，统一“请稍后重试。” |
| 意外异常 | 500，统一提示并记录日志 |
| 两个用户 | 各自只操作自己的配置 |

## 当前状态和后续接入

新路由没有在main.py挂载，暂时不会出现在/docs。模型与路由的检查、HTTP模拟测试、真实临时库联调已经通过；下一步由你挂载路由、准备固定服务端主密钥并在/docs验收。432项自动回归包含本次新模型和接口；真实HTTP联调明确使用独立验证应用，不代表生产main已经挂载或Uvicorn已经启动。

本次没有生成正式主密钥或修改真实.env。以后真实环境访问这些接口前，需要后端已配置固定主密钥并创建凭据表；测试阶段继续使用假Key，不调用供应商。
