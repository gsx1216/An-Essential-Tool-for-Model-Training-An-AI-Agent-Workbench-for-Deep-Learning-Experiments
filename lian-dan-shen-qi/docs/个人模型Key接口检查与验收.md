# 个人模型Key接口检查与验收

## 本次检查结论

你在 `app/api/schemas.py` 中填写的两个模型、`app/api/model_credential_routes.py` 中填写的三个接口，核心逻辑正确，没有发现阻止执行的功能错误。本次只补充了测试、独立验证脚本和说明文档，没有修改你的模型、路由或主应用。

唯一需要清理的位置是 `app/api/model_credential_routes.py` 原第51行：

```python
raise NotImplementedError("TODO 3：返回不含Key的204响应。")
```

它紧跟在保存接口的 `return Response(...)` 后面，函数已经返回，因此这行不会执行。删除这一行即可。这是未清理的练习占位代码，不是当前会触发500的错误。DELETE最后的headers缩进建议和status_code对齐，括号内现有写法合法，不影响功能。

`app/main.py` 还没有挂载这个路由，所以主应用暂时没有这些地址。这是下一步接入工作，不能把独立路由测试通过理解成已经能在主应用的 `/docs` 中看到它。

## 请求如何走完这些模块

```text
HTTP请求
  → 主应用main注册的路由
  → FastAPI解析请求，并解析Depends声明的依赖
  → require_current_user验证Bearer Token，取得真实用户身份
  → get_model_credential_service提供业务对象
  → 路由调用对应async业务方法
  → 业务把同步加密/数据库操作交给asyncio.to_thread
  → 仓储执行参数化SQL
  → 业务结果返回路由
  → 路由返回公开状态或空204
```

身份依赖失败时，不会执行路由函数里的业务调用。用户身份始终来自Token验证结果，不来自请求正文或URL中自行填写的user_id。

| 文件 | 负责什么 | 不同操作的作用 |
| --- | --- | --- |
| `app/main.py` | 创建FastAPI应用，挂载路由、中间件和异常处理 | 决定哪些接口在主应用中可访问 |
| `app/api/schemas.py` | 声明请求和响应字段 | 请求只收SecretStr类型api_key；响应只含provider、configured |
| `app/api/auth_dependencies.py` | 提取并验证登录Token | 成功提供UserResponse；无效或缺失时401 |
| `app/services/current_user_service.py` | 校验Token格式、哈希和数据库登录状态 | 检查有效期、撤销状态，并确定用户身份 |
| `app/api/dependencies.py` | 读取服务端配置，组装仓储、加密器和业务对象 | 组装失败返回通用503；lru_cache复用成功创建的对象 |
| `app/api/model_credential_routes.py` | 接收HTTP请求、调业务、转换HTTP结果 | 不执行SQL、不向客户端返回Key |
| `app/services/model_credential_service.py` | 校验用户ID和Key，协调加密与仓储 | 保存、判断是否存在、删除；内部取Key方法仅供后端使用 |
| `app/security/api_key_cipher.py` | 校验Key格式，使用Fernet加解密 | 密文内部绑定用户ID，防止把其他用户的密文误用于当前用户 |
| `app/repositories/model_credential_repository.py` | 同步执行数据库操作 | 校验用户存在；按user_id和固定provider查询、保存、删除 |
| `sql/*model_credential*.sql`、`sql/create_model_credentials.sql` | 固定SQL及建表定义 | 参数绑定；同一用户同一provider只有一条配置 |
| `app/config.py` | 读取后端环境变量或config/.env | 数据库密码、固定加密主密钥由部署者配置 |
| `app/logging_config.py` | 记录操作位置、异常分类和请求标识 | 不记录Key、密码、原始Token或密文 |
| `app/api/request_logging.py` | 记录请求，处理未预期异常 | 添加请求标识；未知错误返回通用500 |
| `app/api/error_handlers.py` | 处理请求结构校验错误 | 422响应不回显原始输入 |

### PUT：保存或更新自己的Key

`request.api_key` 是SecretStr对象。`get_secret_value()` 显式取出里面的原始字符串，不是加密或解密。随后：

```python
await service.save_api_key(
    user_id=current_user.user_id,
    api_key=api_key,
)
```

业务先校验格式，再加密，再交给仓储保存。仓储按当前用户与deepseek进行新增或更新，并提交事务；失败回滚。响应是空204，不返回原始Key或密文。

`SecretStr` 主要避免对象显示或序列化时意外显示秘密，不代替Fernet加密。格式检查只保证本地可接受的字符和长度，不保证这个Key真的能访问供应商。

### GET：只查看是否已配置

调用 `await service.is_configured(user_id=current_user.user_id)`。仓储查询该用户是否有对应配置，业务返回bool；路由构建：

```python
ModelCredentialStatusResponse(configured=configured)
```

provider自动使用deepseek默认值。返回200，例如 `{"provider":"deepseek","configured":true}`。这个接口不解密、不验证供应商Key是否可用，也不调用模型。

### DELETE：删除自己的配置

调用 `await service.delete_api_key(current_user.user_id)`。仓储只删除该用户的deepseek配置，不删除用户、聊天或登录记录。已不存在时业务返回False，路由仍返回204，因为删除后的目标状态已达到。这称为幂等：重复执行仍得到“配置不存在”的状态。

### 异常和响应头

| 场景 | HTTP响应 | 处理位置 |
| --- | --- | --- |
| 成功保存、更新、删除或重复删除 | 204，空正文 | 路由 |
| 成功查询状态 | 200，两个公开字段 | 路由及响应模型 |
| Key为空、含空白或超出长度 | 400，请求参数不符合要求。 | 业务校验抛ValueError，路由转换 |
| Token缺失、失效或不合法 | 401 | 身份依赖 |
| 缺少字段、字段类型错误、正文额外传user_id等 | 422 | 请求模型及安全校验异常处理器 |
| 数据库故障 | 503，请稍后重试。 | 数据库异常向上传递，由身份依赖或路由转换 |
| 服务端主密钥缺失或不合法 | 503，请稍后重试。 | 工厂依赖 |
| 未预期错误 | 500，请稍后重试。 | 请求中间件 |

成功响应设置 `Cache-Control: no-store` 和 `Pragma: no-cache`，要求缓存不保存这些响应。204表示请求成功但没有响应正文，不能再附带JSON消息。

## 这次引用的包和对象

| 来源 | 对象 | 为什么使用 |
| --- | --- | --- |
| Python标准库logging | getLogger、WARNING | 创建当前模块日志对象；标记校验拒绝的日志级别 |
| Python标准库functools | lru_cache | 缓存组装成功的服务对象，每个进程各自缓存；不缓存用户身份或个人Key |
| Python标准库asyncio | to_thread | 在业务层把同步加密与MySQL操作放到线程中，减少阻塞事件循环 |
| 第三方FastAPI | APIRouter | 声明公共路径前缀和接口分组 |
| 第三方FastAPI | Depends | 根据声明调用依赖，把返回对象注入函数参数；不是继承 |
| 第三方FastAPI | HTTPException | 结束请求并交给框架生成指定状态码的错误响应 |
| 第三方FastAPI | Response | 构造空204，或为GET写入响应头 |
| 第三方Pydantic | BaseModel | 根据字段声明解析、校验和序列化数据 |
| 第三方Pydantic | ConfigDict(extra="forbid") | 拒绝模型未声明的请求字段 |
| 第三方Pydantic | SecretStr | 遮蔽秘密的默认显示；get_secret_value显式取得原文 |
| 第三方mysql.connector | Error，别名MySQLError | 统一捕获MySQL驱动错误，响应不暴露数据库细节 |
| 第三方cryptography | Fernet | 使用固定主密钥完成认证加密及解密验证 |
| 第三方python-dotenv | load_dotenv | 本地开发从.env加载后端配置，已有环境变量优先 |
| 本项目 | require_current_user、get_model_credential_service | 分别提供已认证身份和业务对象 |
| 本项目 | UserResponse、两个凭据模型 | 明确依赖、请求、响应的数据结构 |
| 本项目 | ModelCredentialService、log_failure | 业务能力与不记录秘密的失败日志 |

`@router.put/get/delete` 在模块导入时向router登记接口；只有主应用执行 `app.include_router(router)` 后，这组接口才被主应用接入。

## 已完成的验证

1. 7项模型测试：必填字段、SecretStr类型与遮蔽、额外字段拒绝、公开响应字段。
2. 16项HTTP测试：真实路由和身份依赖，模拟异步身份业务与凭据业务；验证await、参数归属、状态码、空204、响应头、OpenAPI及错误响应。这些测试不连接数据库。
3. 整个自动回归共432项通过，包含本次23项和此前409项。
4. `scripts/verify_model_credential_api_mysql.py` 使用独立FastAPI应用挂载真实路由，真实注册登录、Token验证、用户填写的工厂、真实Fernet、真实仓储与临时MySQL；验证保存、覆盖更新、状态、删除、重复删除、用户隔离、400/401/422/503/500及日志不含测试秘密。
5. 集成验证使用随机临时库，结束后已清理；没有修改业务库、正式.env或main，没有启动Uvicorn，没有调用模型。真实主应用的/docs验收仍需要下面的接入步骤。

在项目根目录可自行重跑普通测试：

```powershell
python -m scripts.check_model_credential_api_models
python -m unittest tests.test_model_credential_api -v
python -m unittest discover -s tests
```

需要本机MySQL和可创建临时库的账号时，才运行独立真实联调：

```powershell
python -m scripts.verify_model_credential_api_mysql
```

## 下一步：你先接入主应用并在/docs验收

这一步先不接模型聊天，确保个人Key管理接口完整可用。

### 1. 清理占位代码并挂载路由

删除保存接口return后的那行NotImplementedError。在 `app/main.py` 的其他路由导入旁增加：

```python
from app.api.model_credential_routes import router as model_credential_router
```

在已有 `app.include_router(...)` 旁增加：

```python
app.include_router(model_credential_router)
```

导入让Python创建router和它登记的接口；include_router让主应用接入这一组接口。仅仅创建文件或导入业务类，不会自动挂载。

### 2. 确认凭据表存在

在项目根目录、已激活langchain环境的终端执行：

```powershell
python -m app.database --model-credential-schema
```

该命令仅创建尚不存在的个人模型凭据表，已有表与记录不清空。MYSQL_PASSWORD仍使用原先后端配置，不通过公共接口提交。

### 3. 配置固定的服务端加密主密钥

如果后端已经配置 `MODEL_CREDENTIAL_ENCRYPTION_KEY`，继续使用原值，不重复生成。没有配置时，由你在本机终端生成一次：

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode('ascii'))"
```

将你本机输出的完整值写入现有 `config/.env` 中，保留里面的其他配置：

```dotenv
MODEL_CREDENTIAL_ENCRYPTION_KEY=这里替换为本机生成的完整值
```

示例中文不是有效密钥，需要替换成实际生成值。这个主密钥属于后端部署配置，不是用户DeepSeek API Key，也不是MySQL密码。保存好同一个值；重启后仍使用它，否则已有密文无法解密。无需把输出发到聊天中。部署时也可以由服务端环境变量提供同名值，现有读取逻辑优先使用环境变量。

### 4. 启动后在/docs测试

```powershell
python -m uvicorn app.main:app --reload
```

打开 `http://127.0.0.1:8000/docs`，应看到“个人模型配置”分组及PUT、GET、DELETE三个接口。

先通过原有注册/登录取得access_token，点击Authorize，在BearerAuth框中只粘贴token本身并确认；不要额外输入Bearer前缀。所有接口路径均为 `/me/model-credentials/deepseek`。

| 顺序 | 你在/docs做什么 | 预期 |
| --- | --- | --- |
| 1 | GET查看状态 | 新用户configured为false；已有配置可为true |
| 2 | PUT发送下方假Key正文 | 204，没有响应正文 |
| 3 | GET查看状态 | 200，provider是deepseek、configured是true，没有Key字段 |
| 4 | PUT替换为另一个假Key | 204 |
| 5 | DELETE删除 | 204 |
| 6 | GET查看状态 | configured为false |
| 7 | 再次DELETE | 仍为204 |
| 8 | PUT正文额外增加user_id | 422，不允许指定用户归属 |
| 9 | PUT正文为{"api_key":""} | 400 |
| 10 | Authorize中Logout后尝试这三个接口 | 401 |

PUT的假数据：

```json
{"api_key":"sk-TEST_ONLY_FAKE_KEY"}
```

这三个接口只管理配置，不联系DeepSeek，假Key即可完成当前验收。configured为true只代表本人的配置记录存在。

如果看到503，客户端只应看到“请稍后重试。”；根据响应X-Request-ID到服务端日志定位配置或数据库故障，不需要向客户端暴露错误细节。

## 本步验收后的任务

再做“发送消息时使用当前用户自己的Key”：已认证身份 → 服务端校验会话归属 → 内部解密该用户Key → 构建请求所需模型调用 → 从数据库读取上下文并发送 → 持久化消息与运行状态。保留现有失败问题可重试的机制；没有个人Key时给出配置提示，不使用其他用户或共享Key。

届时先给你业务和接口骨架，继续由你填写核心逻辑；测试仍使用假模型，不发生真实供应商费用。
