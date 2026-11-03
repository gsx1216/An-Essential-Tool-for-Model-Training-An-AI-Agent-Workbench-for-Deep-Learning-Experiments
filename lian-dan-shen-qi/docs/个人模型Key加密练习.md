# 个人模型 Key：先完成加解密工具

## 当前结果（2026-10-23）

按你的要求，加解密工具已由助手完整实现，并修正你写到一半的代码。19项真实 Fernet 加解密测试通过，项目完整回归共347项通过。测试使用临时随机主密钥和假 API Key，没有连接数据库、请求模型或生成正式部署密钥。

加密代码是 app/security/api_key_cipher.py；正式自动回归用例是 tests/test_api_key_cipher.py；原来的 scripts/check_api_key_cipher.py 保留为运行入口。下面介绍已经完成的实现，不再需要填写 TODO。

## 你原来哪里有问题

这些原始行号指修改前的 app/security/api_key_cipher.py；原文件已备份到本次检查目录。

| 原位置 | 类型 | 原因及结果 | 本次修改 |
| --- | --- | --- | --- |
| __init__ 原第39行 | 功能错误 | 初始化之后仍执行 raise NotImplementedError，合法主密钥也无法创建对象 | 删除占位异常，保存好 Fernet 对象后正常结束构造 |
| encrypt 原第57行 | 功能错误 | Fernet.encrypt 的参数名是 data，不接受 payload_bytes=，调用会抛 TypeError | 使用 self._fernet.encrypt(payload_bytes) 的位置参数调用 |
| encrypt 原第60行 | 返回类型错误 | 原来直接返回 bytes，与方法声明以及 decrypt 接受的 str 不一致 | decode("ascii") 后返回密文字符串 |
| decrypt 原第76行 | 尚未完成 | 保留了 TODO 占位异常，还没有执行解密和 JSON 还原 | 补齐解密、UTF-8解码和json.loads，保留原有归属检查 |

你原来的 payload = payload.encode("utf-8") 可以正常转换类型，不是功能错误；改名为 payload_bytes 是为了区分字符串与字节。self.fernet 这个属性名本身也能使用；这次统一为 self._fernet 表示内部工具对象。未使用的 base64 导入已经清理。

## 本阶段的目标

下一项功能是：登录用户配置自己的 DeepSeek API Key，后端加密保存到 MySQL；发送消息时，后端只取当前用户的配置来调用模型。

分成四小步：①加解密工具（已完成）；②个人凭据表与仓储；③已登录用户的保存、配置状态及删除接口；④发送消息接口取个人 Key。下一步是第二小步。

整体流程：登录用户提交个人 API Key → 后端确认用户身份 → 校验输入并加密 → 密文入库。调用模型时：确认身份和会话归属 → 取本人的密文 → 在后端解密 → 传给 ModelGateway。公开接口只返回是否已配置，不返回原文或密文。没有配置时拒绝调用，不回退到共享 Key。

## 两种密钥不要混淆

| 名称 | 谁提供 | 用途 | 保存位置 |
| --- | --- | --- | --- |
| 用户 API Key | 每个用户自己提供 | 调用该用户的模型账号 | 以后加密存入 MySQL |
| 加密主密钥 master_key | 系统部署者生成并保管 | 加解密用户 API Key | 服务器环境变量；本地开发可用未提交的 config/.env |

主密钥不通过普通用户接口输入，不与数据库密文放在一起。之后接配置加载时再生成和配置正式主密钥；本次测试只在内存生成临时主密钥，你现在不用改 .env。

登录密码用慢哈希验证即可；API Key 在后端调用模型时需要还原，所以采用可逆加密。使用 cryptography 提供的 Fernet，其 API 包含 encrypt 和 decrypt，并在解密时校验密文完整性。参考 [Fernet 官方文档](https://cryptography.io/en/latest/fernet/)。不自己拼装加密算法。

## 依赖

在已有 langchain 环境和项目根目录执行：

```powershell
python -m pip install cryptography==50.0.2
```

requirements.txt 已记录本次使用的版本，你当前 langchain 环境中已安装 cryptography 50.0.2。以后在新环境中安装项目依赖时仍需要它。

## 当前代码按什么顺序执行

app/security/api_key_cipher.py 中的三个方法已经完成：

1. __init__：把传入的 master_key 转成 ASCII 字节，构造 Fernet 对象并保存到 self._fernet。
2. encrypt：将已有的 payload JSON 字符串转成 UTF-8 字节。
3. 调用 self._fernet.encrypt，得到密文字节。
4. 将密文字节转成 ASCII 字符串并返回，便于以后存库。
5. decrypt：将密文转成 ASCII 字节，调用 self._fernet.decrypt 取得原文字节。
6. 原文字节按 UTF-8 解码，再用 json.loads 还原字典 payload；之后交给已经写好的身份和格式校验。

加密的完整顺序是：检查正整数用户 ID → 校验 API Key → 把版本、供应商、用户 ID 和 API Key 组成字典 → JSON 字符串 → UTF-8字节 → Fernet加密 → ASCII密文字符串。

解密的完整顺序是：检查用户 ID 和密文类型/长度 → 密文字节 → Fernet验证并解密 → UTF-8字符串 → json.loads还原字典 → 检查版本、供应商和用户 ID → 校验并返回 API Key。只有后端调用模型的内部流程将来使用这个返回值，公开接口不直接返回它。

构造主密钥错误时抛出固定提示的 ValueError；密文被篡改、主密钥不匹配、解码失败或JSON错误时，统一抛出不含秘密内容的 ValueError。元数据或用户ID不符也拒绝继续。logged_operation 负责将失败写入安全日志，然后继续向调用方抛出异常。

三个方法都是同步函数，本次不使用 async/await；以后异步业务层可通过 asyncio.to_thread 调用它们。当前只验证传入ID的格式和密文内部归属，不查询登录记录，也不代替接口的Token鉴权或仓储的SQL归属过滤。

主密钥只在部署配置时生成，不在每次加密或启动构造函数里随机生成，否则重启后旧数据无法解密。

## 为什么加密内容里有 user_id

加密前的字典包含 version、provider、user_id、api_key。解密后除了验证密文，还确认内部的 user_id 等于当前用户，避免把一个用户的整段密文挪到另一个用户记录后仍能使用。将来 SQL 也必须按当前登录用户过滤，两种检查保护不同边界。

## 包和方法的作用

| 对象 | 来源 | 作用 |
| --- | --- | --- |
| json.dumps | Python 标准库 | 字典变成 JSON 字符串 |
| json.loads | Python 标准库 | JSON 字符串变成字典 |
| str.encode | Python 内置类型 | 字符串变成 bytes 字节 |
| bytes.decode | Python 内置类型 | 字节变成字符串，不是解密 |
| Fernet | 第三方 cryptography | 创建加解密对象，执行 encrypt/decrypt |
| InvalidToken | 第三方 cryptography | 密文格式、签名等验证失败；与登录 Token 异常不同 |
| logged_operation | 项目日志模块 | 记录操作结果和安全异常，不记录参数或返回正文 |
| validate_positive_integer | 项目校验模块 | 确认 user_id 是范围内的正整数，排除布尔值 |
| validate_api_key | 本文件提供 | 输入类型、长度、空白检查，不请求供应商验证 Key |

Fernet 需要 bytes；数据库字段准备保存字符串，所以两边要 encode/decode。把字符串变成 bytes 本身不提供加密保护，真正的加密在 encrypt，真正的解密在 decrypt。

## 如何复查测试

```powershell
python -m scripts.check_api_key_cipher
python -m unittest tests.test_api_key_cipher -v
python -m unittest discover -s tests
```

前两条都运行同一组19项真实加解密本地测试；第三条运行项目全部347项回归。加解密测试只使用临时随机主密钥和假 API Key，不连接数据库、不调用模型、不打印真实秘密。其他回归中的数据库操作由模拟对象替代，本次没有实际数据库联调。

覆盖原文往返、同一内容随机密文、同主密钥重建实例可读取、错误主密钥拒绝、密文篡改拒绝、用户之间不可交换密文、空白与超长输入、错误元数据和用户ID；还验证错误JSON、无效UTF-8、最大用户ID、ASCII标点、字段缺失，以及持久化日志不泄露主密钥、Key或密文。

这一小步已经通过。下一步由助手准备个人凭据表与仓储需要的SQL，你继续手写业务层。当前尚未接入凭据数据库、主密钥配置加载或公开接口，因此并不代表用户配置Key的整条功能已经完成。
