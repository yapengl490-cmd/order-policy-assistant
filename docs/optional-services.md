# 可选服务：逐项开启，默认关闭

免费演示只读合成数据库、关键词政策与固定计算。模型聊天和真实嵌入不是免费演示的隐藏步骤；飞书不会随网页启动。

## 1. DeepSeek 模型

先在自己的 DeepSeek 账号创建密钥并确认余额、费用与数据发送范围。将 `DEEPSEEK_API_KEY=...` 写入本机 `local/demo.env`（默认已被忽略）；还可配置 `MODEL_NAME` 和 `MAX_DAILY_TASKS`。不要上传此文件或把密钥发到聊天。

显式使用第二份覆盖配置：

```sh
docker compose --env-file local/demo.env -f compose.verify.yaml -f compose.model.example.yaml up -d app
```

这时聊天会把用户问题、授权工具的结果、政策条款发送到 DeepSeek。固定计算仍可不调用模型。关闭模型时执行：

```sh
python3 demo.py up
```

默认配置会把 `MODEL_ENABLED` 重新设置为 `0`。停用后已保留任务仍可查看。模型最多4次请求、工具最多6次尝试、总时限90秒是当前默认；它们限制执行量，不保证按人民币硬停止。服务商费用可能受实际输入、输出、缓存和失败计费影响。

## 2. 可选真实嵌入

Embedding（嵌入）把政策条款或问题变成向量，用于相关性比较。默认使用关键词，不偷偷调用嵌入服务。

复制 `knowledge.env.example` 到 `local/knowledge.env`，仅在本机填写硅基流动密钥；不要把它提交。当前固定服务为硅基流动 `BAAI/bge-m3`，1024维。开启会发送演示政策文本、后续检索问题；默认没有重排序模型。

```sh
cp knowledge.env.example local/knowledge.env
# 用编辑器在本机填写，然后明确建立索引（这一步可能计费）：
docker compose --env-file local/demo.env -f compose.verify.yaml -f compose.knowledge.example.yaml run --rm bootstrap python demo_index.py --allow-external-embedding
# 同时启用模型与检索配置：
docker compose --env-file local/demo.env -f compose.verify.yaml -f compose.model.example.yaml -f compose.knowledge.example.yaml up -d app
```

该可选覆盖会只读挂载 `local/knowledge.env`。容器用户为 UID（用户编号）10001；Linux 上宿主文件权限可能使其无法读取，须先核对文件所有者与容器读取权限，不能把密钥文件直接改成所有用户可读来绕过。该可选挂载的跨平台可用性尚未验收；默认免费演示不依赖此文件。

只有 `bootstrap` 服务具备这套独立演示库的管理员配置；日常应用仍只读订单。索引失败会明确停止，保留旧数据，可按相同资料安全重试；不是“能聊天就代表索引成功”。索引保存在数据库卷，保留卷即可保留。此可选入口的配置和离线边界可测试；是否实际调用服务成功，以本次公开验收记录为准。

## 3. 可选飞书

仓库提供接收器代码，但不会创建飞书应用、申请平台权限、替你绑定身份或公开本机端口。需要你自己的测试应用权限、长连接配置，以及服务器预登记的私聊发送者和会话白名单。程序将该身份映射为指定演示账号；聊天里自称店主不生效。

此接收器目前是原生 Python 入口，固定连接 `http://127.0.0.1:18080`，并读取宿主 `local/access.json`。它不能直接使用默认容器的18180端口和卷内凭据；需要另行配置原生后端与相同授权账号，或后续实现容器接收器适配。该适配未在本次发布实现，不能声称新克隆开箱即有飞书收发。

原生配置放 `local/app.env` 或进程环境变量；在本机显式设置 `FEISHU_SECRETS_FILE`、`FEISHU_ALLOWED_FILE`；格式以 `feishu_bridge.py` 的配置加载校验和仓库空模板为准。只在拥有这两个文件对应的账号、私聊和数据授权时启动。接收器需要访问运行中的本地后端，它没有随默认容器部署启动；公开版本不继承作者的私人账号绑定。

收到消息、任务已执行、回复已送达是不同状态。已有状态用于去重和重试判断，不能把连接建立等同真实收发成功。新克隆不包含作者的运行回执。

## 4. 短程序和快照查询

Programmatic tool use（程序化工具使用）允许模型生成受限短程序，再由独立容器执行。它只可通过受控通道调用只读业务工具；限制语言结构、时间、返回量、总工具预算，禁止任意联网和读取宿主文件。

该路径依赖宿主 Mac 的 Docker（容器引擎）执行能力和固定执行镜像。**默认网页容器没有 Docker 管理接口挂载，不能直接使用这个模式**，界面/接口应明确拒绝并说明原因。不要为了让按钮可点而随手挂载整个引擎接口。

Snapshot SQL（快照查询）只在授权小型数据快照上执行受限只读查询，与固定结果作对照；不是让模型连接正式 PostgreSQL（关系数据库）自由查询。两种模式都需独立环境与权限验证，不能用普通订单查询通过替代。
