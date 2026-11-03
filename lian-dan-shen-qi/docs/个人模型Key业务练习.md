# 个人模型 Key：业务实现说明

## 最新实现结果

按你本次要求，ModelCredentialService已由助手完整实现，所有TODO和占位异常已移除。20项业务回归与项目完整393项回归通过；真实Fernet、实际仓储和独立临时MySQL的组合联调也通过。临时库已清理，测试只使用假API Key，没有调用模型。

本次完成业务层，尚未接入主密钥配置加载、服务工厂和HTTP路由，因此不会在/docs里新增接口。

## 上一步已经通过

你已修正保存SQL的参数顺序，以及删除结果的布尔判断。最新仓储17项测试和完整373项回归通过，真实MySQL独立临时库也验证实际仓储的保存、更新、查询、删除、重复删除、用户隔离、错误传播和提交失败回滚。临时库已清理，没有调用模型。

## 业务层的职责

文件app/services/model_credential_service.py中的ModelCredentialService，负责把两个现成对象串起来：

- self.cipher：ApiKeyCipher对象，负责真正的加密和解密。
- self.repository：ModelCredentialRepository对象，负责MySQL存取、提交和回滚。

构造函数把对象保存到self，属于组合和依赖注入，不是继承，也不会自动把仓储方法变成业务类自身的方法。调用形式是self.repository.save_key(...)和self.cipher.encrypt(...)。

这些对象可以复用；每次用户ID、原始Key和密文使用方法局部变量，不保存为self.api_key、self.user_id或全局变量，避免不同用户请求串用数据。

## 四个方法

| 方法 | 参数 | 顺序 | 返回值 |
| --- | --- | --- | --- |
| save_api_key | user_id、原始api_key | 校验 → 加密 → 保存密文 | None |
| is_configured | user_id | 校验 → 查询密文是否存在 | bool |
| delete_api_key | user_id | 校验 → 调用仓储删除 | bool |
| get_api_key | user_id | 校验 → 查询 → 处理未配置 → 解密 | 原始Key字符串，仅后端内部使用 |

公开接口将来只需要显示是否已配置，不返回原文或密文。get_api_key是后端调用模型之前使用的内部方法，不能直接作为公开接口返回值。is_configured只表示有配置记录，不证明供应商接受该Key，也不发起模型请求。

## 已完成的执行顺序

1. 构造函数保存repository和cipher，不连接数据库、不生成主密钥、不缓存某个用户的Key。
2. save_api_key先校验用户ID和Key，等待加密完成，再把密文交给仓储；只有仓储提交完成才正常返回None。加密失败不会进入保存，数据库失败继续抛出。
3. is_configured读取密文后返回encrypted_key is not None，不解密；查询失败继续抛出，不能返回False冒充未配置。
4. delete_api_key返回仓储给出的True/False，不把False当异常；删除或提交失败继续抛出，不返回成功。
5. get_api_key每次重新按用户读取密文，没有配置时抛ModelCredentialNotConfiguredError；有配置时在工作线程解密并返回。只将解密步骤的ValueError转换为ModelCredentialDecryptionError，数据库错误不被误判为未配置或解密失败。

四个方法是async def，因此调用方将来要await它们；内部加密类和仓储方法都是同步函数，使用await asyncio.to_thread(...)执行。业务层不自己建立数据库连接或commit，不调用模型。

例如保存的关键两步：

```python
encrypted_key = await asyncio.to_thread(self.cipher.encrypt, user_id, api_key)
await asyncio.to_thread(self.repository.save_key, user_id, encrypted_key)
```

第一步返回密文，第二步只接收密文，不会把原始api_key直接交给数据库。

## to_thread怎么使用

调用格式：

```python
result = await asyncio.to_thread(
    某个同步函数,
    参数一,
    参数二,
)
```

第一个参数是函数对象本身，例如self.cipher.encrypt，后面才是user_id和api_key。不要先写self.cipher.encrypt(...)再把返回值交给to_thread。先await加密拿到密文，再await数据库保存，两个步骤有依赖，不能一起无序执行。

## 异常分别代表什么

| 异常或结果 | 含义 | 处理 |
| --- | --- | --- |
| ValueError：输入校验失败 | 本次用户ID或原始Key格式错误 | 继续抛出，由未来接口转换 |
| ModelCredentialNotConfiguredError | 用户尚未配置个人Key | 提示先配置，不回退到平台共享Key |
| ModelCredentialDecryptionError | 已存密文无法解密 | 保持服务端错误分类，不当成未配置 |
| MySQL异常 | 查询、保存、删除或提交失败 | 继续抛出，不返回成功、不伪装成None/False |
| delete返回False | 用户存在，但已没有配置 | 正常结果，允许重复删除 |

两个新业务异常在app/errors.py中由助手提供。get_api_key的解密异常转换已经写好：把加密工具抛出的ValueError转换为ModelCredentialDecryptionError，未来接口对外只提示通用重试信息。注意输入校验和数据库读取在这个try外面，不能把这些错误也误认成解密失败。

## 模块和包的作用

| 对象 | 来源 | 作用 |
| --- | --- | --- |
| asyncio.to_thread | Python标准库 | 将同步的加密、数据库方法放到工作线程执行 |
| ModelCredentialRepository | 项目仓储模块 | 校验用户存在并按用户过滤SQL、处理事务 |
| ApiKeyCipher | 项目加密模块 | 加密时绑定用户ID，解密时检查归属和完整性 |
| validate_api_key | 项目加密模块 | 检查原始Key类型、长度和空白，不请求供应商 |
| validate_positive_integer | 项目校验模块 | 检查用户ID格式，不代表登录鉴权 |
| logged_operation | 项目日志模块 | 包住同步/异步函数，记录安全结果和异常，不记录秘密内容 |
| 两个ModelCredential异常类 | 项目异常模块 | 区分未配置和已存密文无法读取 |

业务层不写SQL，也不先查用户再调用仓储。仓储已经检查实际用户是否存在；未来接口从已确认的Token身份取得user_id，普通客户端不能用输入的任意ID冒充用户。

## 如何复查测试

在项目根目录运行：

```powershell
python -m scripts.check_model_credential_service
python -m unittest tests.test_model_credential_service -v
python -m unittest discover -s tests
```

前两条执行同一组20项业务测试，已纳入自动回归；第三条执行项目全部393项测试。业务单元测试使用假Key、模拟加密工具和模拟仓储，不需要正式主密钥或数据库，不调用模型。检查保存顺序、只保存密文、配置状态、重复删除、内部解密、用户隔离、错误传播、同步工作不阻塞事件循环，以及日志不记录原始Key、密文或异常中的秘密文本。

真实数据库组合验证：

```powershell
python -m scripts.verify_model_credential_sql_mysql --service
```

这个命令同时验证SQL、实际仓储和实际业务，使用现有本机MySQL、随机独立临时库、内存临时主密钥与假Key。覆盖加密保存、更新、状态、内部解密、重复删除、用户隔离、错误主密钥、用户密文交换拒绝和数据库错误传播；仓储部分还覆盖提交失败回滚。测试后只清理本次临时库。

下一步是加载服务器保存的加密主密钥、组装服务工厂，再接入已鉴权的保存/状态/删除HTTP接口。内部get_api_key的返回值不会直接做成公开响应。
