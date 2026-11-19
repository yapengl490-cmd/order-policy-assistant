# 来源与第三方许可

本项目代码按根目录 [MIT 许可证](LICENSE) 提供。它不改变第三方依赖、数据及基础镜像各自的许可。本文记录主要直接依赖与资料来源，不是对全部代码原创性、全部传递依赖或法律合规的完整审计。

## 主要依赖

应用通过依赖清单安装以下软件，未将这些项目的完整源码复制到本仓库。版本见 [`requirements.lock.txt`](requirements.lock.txt)；许可按对应安装包元数据及包内许可文件核对。重分发依赖或构建镜像时应保留各包原有版权与许可文件。

| 依赖 | 用途 | 许可 | 官方来源 |
|---|---|---|---|
| SQLAlchemy | 数据库访问与对象映射 | MIT | [sqlalchemy/sqlalchemy](https://github.com/sqlalchemy/sqlalchemy) |
| Psycopg / psycopg-binary | PostgreSQL 驱动 | LGPL-3.0-only | [psycopg/psycopg](https://github.com/psycopg/psycopg) |
| python-dotenv | 读取本机环境配置 | BSD-3-Clause | [theskumar/python-dotenv](https://github.com/theskumar/python-dotenv) |
| FastAPI | 应用接口 | MIT | [fastapi/fastapi](https://github.com/fastapi/fastapi) |
| Uvicorn | 应用服务器 | BSD-3-Clause | [Kludex/uvicorn](https://github.com/Kludex/uvicorn) |
| Pydantic | 输入与输出结构校验 | MIT | [pydantic/pydantic](https://github.com/pydantic/pydantic) |
| HTTPX | HTTP 客户端 | BSD-3-Clause | [encode/httpx](https://github.com/encode/httpx) |
| Lark OpenAPI SDK (`lark-oapi`) | 可选飞书接收与回复 | MIT | [larksuite/oapi-sdk-python](https://github.com/larksuite/oapi-sdk-python) |
| MCP Python SDK (`mcp`) | 独立工具协议客户端及服务端 | MIT | [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) |

Python 运行时、PostgreSQL 服务及容器基础镜像也有各自的许可与组件清单；本仓库提供构建配置，不将它们重新授权为 MIT。上表没有穷尽锁文件中的传递依赖，完整许可应查看安装分发包内的 `licenses` / `LICENSE` 文件及上游来源。

## UCI 数据格式导入器与公开样例

可选导入器 [`prepare_public_sample.py`](prepare_public_sample.py) 支持用户显式在本机处理以下公开资料：

- Chen, D. (2015). **Online Retail** [Dataset]. UCI Machine Learning Repository.
- 来源：[UCI 数据页](https://archive.ics.uci.edu/dataset/352/online+retail)，[DOI 10.24432/C5BW33](https://doi.org/10.24432/C5BW33)。
- 数据许可：[Creative Commons Attribution 4.0 International（CC BY 4.0，署名许可）](https://creativecommons.org/licenses/by/4.0/)。使用或改编对应资料时应保留署名、许可链接并说明修改。

**当前发布包不包含 UCI 真实发票行、客户编号或真实来源映射。** [`samples/uci/`](samples/uci/README.txt) 中的三笔订单、买家编号、金额、日期、退款和渠道均为独立合成；目录名及 `S_UCI_DEMO` 只是保留的兼容标识，不能把这些合成样例声称为真实 UCI 数据验证。

若用户自行运行可选导入器，原资料的 GBP（英镑）价格数值会被明确投影为 CNY（人民币）演示数值，**不是汇率换算**；支付状态、日期、店铺及退款均注明为模拟假设。原始行、映射和投影只写入被忽略的本机 `local/imports/`，不会自动入库、上传或提交。本仓库 MIT 代码许可不替代这些外部数据的许可。

其他 [`samples/`](samples/) 和 [`knowledge/`](knowledge/) 文件是本工程的合成业务样例与演示政策，不代表电商平台真实订单、真实售后承诺或法律意见。
