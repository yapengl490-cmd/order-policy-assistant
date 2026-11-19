公开仓库中的独立合成样例

本目录名称沿用导入器原型。仓库现有 JSON 完全由程序构造：订单编号、买家、金额、时间、
退款和渠道均为虚构，不包含 UCI 的真实发票行、客户编号或原始到演示订单的映射。
店铺标识 S_UCI_DEMO 是兼容原接口的演示标识，不代表真实机构或真实店铺。

当前四份 JSON：
- uci_projection.json：三笔独立合成订单及两笔合成退款，可用于免费演示导入。
- uci_expected.json：事前固定金额预期，不能发送给被测模型。
- uci_demo_channels.json：独立合成渠道；未提供的渠道保留 unknown（未知）。
- uci_demo_comparison_expected.json：两期比较的固定验收预期，不是原因证据。

退款截止 2026-11-03T00:00:00+08:00（不含截止边界）：
订单                  支付/分    退款/分    净额/分
O_PUBLIC_SYNTH_001       10000       3500       6500
O_PUBLIC_SYNTH_002        8000          0       8000
O_PUBLIC_SYNTH_003       20000          0      20000
合计                    38000       3500      34500

可选的 UCI 格式导入器
prepare_public_sample.py 支持用户自行在本机提供官方压缩包。默认演示、初始化和测试不会
下载源文件，也不会调用模型。导入器只生成本机私有投影；不自动写数据库、上传或建立账号。

来源：Chen, D. (2015). Online Retail [Dataset]. UCI Machine Learning Repository.
DOI：https://doi.org/10.24432/C5BW33
数据页：https://archive.ics.uci.edu/dataset/352/online+retail
官方压缩包：https://archive.ics.uci.edu/static/public/352/online+retail.zip
数据许可：CC BY 4.0，https://creativecommons.org/licenses/by/4.0/
本项目代码许可独立说明；以上是数据来源许可，不能当作项目代码许可。

源金额单位为 GBP（英镑），InvoiceDate 是发票时间，InvoiceNo 以 C 开头表示取消。
可选导入器把价格数值投影为 CNY（人民币）演示分值，不是汇率换算；支付时间、支付状态、
店铺和退款都是明确标注的模拟假设。取消发票不会直接变成成功退款。
程序保留选中发票全部源行，非法/缺失/矛盾行会拒绝整张发票，不悄悄漏行后求和。

在项目目录执行（先自行下载官方源文件；请遵守其许可和隐私要求）：
python prepare_public_sample.py --source-zip /本机源文件/online-retail.zip --output-dir local/imports/uci-run-01

输出必须位于已被 .gitignore 排除的 local/imports/，文件权限为仅本用户读写；遇到同名文件
停止，不能覆盖旧证据。生成的真实来源行和映射只能留在本机，不添加到公开仓库。
旧解析器验证过的压缩包 SHA-256：
f5385cbb54bbebf7196389109c6b0621faab0c304e3702548165e71c84aede8b
内层工作簿 SHA-256：
43465a06f2ccf7c8b5bd2892bc7defb52f97487934fe93b16ae4c3936424676d
官方文件、哈希或列名变动时重新核查，不能复用历史成功结论。

测试使用自行构造的微型 XLSX 工作簿，无需真实数据：
python -m unittest discover -s tests -p 'test_public_sample.py'
python -m unittest discover -s tests -p 'test_public_demo.py'

本目录只验证合成导入、权限分离、金额核对与来源记录机制，不代表真实平台订单或生产退款验证。
