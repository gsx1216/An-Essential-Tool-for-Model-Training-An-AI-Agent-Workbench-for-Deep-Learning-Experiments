# 个人模型Key：主密钥配置与服务工厂

## 最新检查结果

你已完成5个TODO，工厂逻辑正确；master_password变量名前后一致，正常传入加密对象，没有功能错误。工厂7项（原6项加1项HTTP依赖失败检查）及完整409项回归通过，真实临时库也验证了实际工厂、配置读取、业务、加密与仓储的完整链路。临时主密钥只用于测试，真实.env没有改动。下一步见[个人模型Key接口接入练习](个人模型Key接口接入练习.md)，下文保留原练习步骤供回顾。

## 本次任务

个人Key的加密、仓储和业务层已经通过检查。下一步给后续FastAPI路由提供完整的ModelCredentialService对象。

安全配置读取由助手完成，你只需要填写app/api/dependencies.py末尾的get_model_credential_service工厂，共5个TODO。现在不需要正式主密钥、不修改真实.env；工厂检查使用临时主密钥和模拟数据库密码。

## 两种配置分别是什么

| 配置 | 谁设置 | 用途 |
| --- | --- | --- |
| MYSQL_PASSWORD | 部署者 | 连接后端MySQL |
| MODEL_CREDENTIAL_ENCRYPTION_KEY | 部署者 | 加解密所有用户保存的个人API Key |
| 用户个人DeepSeek API Key | 每位已登录用户 | 后续通过已鉴权接口提交，加密存入该用户的数据库记录 |

主密钥不是DeepSeek API Key，也不是登录密码。它由服务器保管，不经过普通用户接口、不与数据库密文存放在一起。正式主密钥的生成和配置留到这一步检查通过后；测试用的是内存临时值，不能拿测试生成的随机值替换已存凭据所用的主密钥。

## 已完成的配置读取

app/config.py新增get_model_encryption_key：

1. load_dotenv读取本地config/.env，override=False保留服务器环境变量优先级，interpolate=False禁止把其他变量展开成主密钥。
2. 读取MODEL_CREDENTIAL_ENCRYPTION_KEY，检查存在、长度与前后空白，使用Fernet确认密钥编码可用。
3. 配置有效时返回字符串；缺失或错误时抛ModelCredentialConfigurationError，安全日志不记录密钥原文。
4. 不自动生成、不覆盖配置。已经保存的密文需要同一把主密钥才能读取，读取配置时随机生成新值会破坏这个条件。

get_mysql_password保留原逻辑。旧的全局模型Key读取函数没有接入个人Key服务；这个工厂只需要数据库密码和加密主密钥。

## 你要填写的5个TODO

打开app/api/dependencies.py，找到最末尾get_model_credential_service：

1. 调用get_mysql_password，用password接收。
2. 调用get_model_encryption_key，用master_key接收。
3. 创建ModelCredentialRepository(password)，用repository接收。
4. 创建ApiKeyCipher(master_key)，用cipher接收。
5. 返回ModelCredentialService(repository=repository, cipher=cipher)。

写完后删除NotImplementedError，保留已有try/except和@lru_cache。这里不调用任何用户业务方法，不传入user_id，不查询数据库。

对象关系：读取两项后端配置 → 创建仓储与加密对象 → 把两个对象注入业务对象 → 返回组装好的业务对象。返回对象和调用业务是两件事，后续接口拿到对象后才根据当前登录用户执行save_api_key等方法。

## 为什么这个函数有异常转换

FastAPI先解析Depends再执行路由函数。如果工厂在依赖解析时失败，路由函数内部的try/except还没有执行，所以工厂里已经提供配置错误处理：安全日志记录具体失败位置，HTTP层统一503“请稍后重试。”，不把配置名字、密码或主密钥返回客户端。

ModelCredentialConfigurationError在app/errors.py，表示后端主密钥配置问题；数据库密码缺失或加密对象构造失败的ValueError也在这里统一转换。未知故障继续交给已有全局错误处理。

## lru_cache复用什么

@lru_cache(maxsize=1)复用本进程构建成功的服务对象，以及其中的仓储和加密对象，不保存某个用户的Key、Token、身份或查询结果。仓储每次操作仍单独创建连接。工厂抛出异常时不会缓存这个失败，修正缺失配置后仍能重试。

已经成功创建的对象会继续使用原配置；部署配置变更通常需要重启进程。多个进程或副本必须配置一致的固定主密钥，不能每次启动随机生成。换主密钥需要专门的数据迁移，不能只改环境变量后期待旧密文自动可读。

## 本次包与模块

| 对象 | 来源 | 作用 |
| --- | --- | --- |
| os.getenv | Python标准库 | 从进程环境读取后端配置 |
| load_dotenv | 第三方python-dotenv | 加载本地.env，保留服务器配置优先级 |
| Fernet | 第三方cryptography | 校验主密钥能否用于加解密 |
| lru_cache | Python标准库functools | 按进程复用构建成功的对象 |
| logging、log_failure | 标准库及项目日志模块 | 记录安全的配置错误，不记录秘密内容 |
| HTTPException | 第三方FastAPI | 在依赖解析阶段返回通用503 |
| ModelCredentialRepository | 项目仓储 | 提供按用户存取密文的方法 |
| ApiKeyCipher | 项目加密工具 | 使用传入的固定主密钥加解密 |
| ModelCredentialService | 项目业务 | 串联加密与仓储，保存和读取个人Key |

## 填完后测试

```powershell
python -m scripts.check_model_credential_factory
```

6项工厂检查使用模拟密码、临时随机主密钥和真实对象构造，不连接数据库，不调用模型。覆盖对象组装、缓存复用、主密钥缺失、数据库密码缺失、加密对象构造错误、失败后恢复，以及不需要全局DeepSeek Key。未填写时因NotImplementedError失败属于预期。

已经完成的主密钥配置读取另有9项自动回归：

```powershell
python -m unittest tests.test_model_credential_config -v
```

这一小步完成后先检查工厂，再通过实际工厂与真实临时库联调，之后生成正式服务器主密钥并接保存、状态、删除接口。当前尚未新增HTTP路由。
