受控政策库说明（R06，2026-11-18）

范围与证据
本目录是助手编写的 S001 普通鞋类合成演示，既不是法律意见，也不是淘宝或任何真实店铺政策。
2026-10 版本为 168 小时，2026-11 版本为 336 小时。订单支付时刻只用于选择政策版本；
退货窗口从可信收货时刻起算，包含起点、不含截止。未穿着、吊牌完整的用户自述仍未核实。
完整条款保留定制例外和商家责任核验条件。实际资格判断由 buyer_support 的程序完成，不由向量分数或模型决定。

单一原文及人工维护边界
文件 JSON 头只保存范围、版本与有效区间；正文是业务规则的唯一来源。
policy_documents.py 对这份有限模板逐项解析，返回规则及每条规则的原文引用；不接受未知语义。
manifest.json 只保存路径和 SHA-256 指纹，不保存第二套规则。改文字须审查原文、解析器、业务边界测试，更新清单。
本版不是任意商家手册自动理解系统。新增版本用新 ID 和不重叠有效区间导入；不能覆盖已有版本。
每份文件按标题分成范围、退货资格、退货运费 3 个完整条款，共 6 块；过长条款拒绝，不能切掉例外后继续。

存储与权限
sql/009_policy_knowledge.sql 只新增 public.policy_documents 和 public.policy_chunks。
管理员离线导入/索引，应用账号 datus_reader 只有 SELECT。独立演示由demo.py初始化；自管数据库由管理员显式迁移，本说明不证明已迁移。
import_policy_files 在一个事务内校验整批；同 ID 同内容幂等，内容/版本冲突或区间重叠整批拒绝。
PostgreSQL 事务锁按店铺/品类串行化本导入函数；管理员绕开函数直接改库不属于受支持导入流程。
两表需随 PostgreSQL 备份恢复；原文也保留于版本管理。向量可重建，但重建会发送政策正文给外部嵌入服务。
不自动删除旧版本、订单、退款或其他持久数据。回退仅由管理员确认新表数据不再需要后处理。

接口
from policy_documents import load_manifest_files
from policy_kb import import_policy_files, select_policy, index_policy_embeddings, search_policy_chunks
from embedding_client import EmbeddingSettings, EmbeddingClient

import_policy_files(admin_engine, load_manifest_files(project_root / 'knowledge/manifest.json'))
bundle = select_policy(read_engine, store_id='S001', category='ordinary_shoes', paid_at=trusted_order_paid_at)
关键词检索：search_policy_chunks(read_engine, question, store_id=..., category=..., paid_at=..., mode='keyword')
以上调用不发送嵌入请求。store/category/paid_at 必须来自已授权订单，不直接相信模型或聊天输入。

真实嵌入仅在显式调用时发生：
embedder = EmbeddingClient(EmbeddingSettings.load(root=project_root))
index_policy_embeddings(admin_engine, embedder)  # 6 块可在一次请求中生成；写入前再次核对原文
search_policy_chunks(read_engine, question, store_id=..., category=..., paid_at=..., mode='hybrid', embedder=embedder)
配置模板 knowledge.env.example 复制至 local/knowledge.env，在本机填密钥或引用已保存的外部 .env 文件。
API 固定 https://api.siliconflow.cn/v1/embeddings；模型 BAAI/bge-m3，输出 1024 维，不发送 dimensions 参数。
输入数/长度、响应大小、有限数值/维度均校验，默认超时 15 秒，不自动重试。统计不记录密钥与请求正文。
服务异常明确失败；缺少适用政策或向量时不会调用嵌入服务。未计费请求会明确统计为 unmetered_requests。
这些限制不等同于资金硬锁；余额/免费额度由真实执行方另外核查。

检索对照与限制
先按店铺、品类、支付时间过滤到同一版本，再对同样的 3 个候选比较关键词与混合检索。
关键词采用中文双字片段/英文词匹配；混合检索组合关键词排名与余弦相似度排名，使用倒数排名融合。
向量作为 JSON 存于现有 PostgreSQL，小数据在 Python 中逐条精确比较；未安装向量数据库或扩展。
默认没有 Reranker（重排序模型）。本规模无需宣称向量性能优势；更大数据量需另测延迟、内存和索引方案。
retrieval_cases.json 是真实嵌入前冻结的 6 题期望，含原文指纹。K06 店铺不匹配应无候选、零嵌入请求。
同候选对照须保存每题两种结果的候选 ID、内容指纹、排名、耗时与请求统计；不能只报汇总准确率。
此文件不证明真实嵌入已通过，且不预设混合检索一定优于关键词。测试中的替身向量明确标为离线来源。
policy_kb 默认拒绝替身向量，allow_test_vectors=True 仅用于独立测试库，不能据此称实际向量效果通过。

离线验证
在项目目录使用现有虚拟环境执行：python -m unittest discover -s tests -p 'test_policy*.py' -v
新测试使用独立内存 SQLite，变异原文仅写临时目录，并验证受控源文件字节未被测试改写。
它们覆盖导入/引用/版本/完整例外/冲突回滚/过滤/嵌入边界/同候选对照；不能替代正式 PostgreSQL 或真实服务验收。

接口依据
https://api-docs.siliconflow.cn/docs/api/embeddings-post
https://github.com/siliconflow-inc/siliconflow-api/blob/main/openapi.yaml
