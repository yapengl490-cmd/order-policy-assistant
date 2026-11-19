"""把获许可的UCI发票素材投影为明确标注的合成订单，不导入数据库。

原价是GBP、原日期是发票时间；输出CNY、支付状态、日期、店铺、退款均为
教学模拟假设。取消发票绝不直接转为成功退款。只输出少量完整发票及来源。
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta
from decimal import Decimal, DecimalException, ROUND_HALF_UP
import hashlib
import io
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
PAGE = 'https://archive.ics.uci.edu/dataset/352/online+retail'
EXPECTED_HEADERS = ['InvoiceNo','StockCode','Description','Quantity','InvoiceDate','UnitPrice','CustomerID','Country']
NOTICE = 'UCI英国发票素材的合成人民币投影；非汇率换算，支付/退款/时间/店铺均为演示假设'


def collect_invoices(source_zip: Path, count: int = 3):
    if type(count) is not int or not 1 <= count <= 10 or source_zip.stat().st_size > 35*1024*1024:
        raise ValueError('仅接受预期大小的官方源文件和1—10张发票')
    with zipfile.ZipFile(source_zip) as outer:
        entries=[i for i in outer.infolist() if i.filename=='Online Retail.xlsx']
        if len(entries)!=1 or entries[0].file_size>35*1024*1024:
            raise ValueError('源文件不是预期UCI工作簿')
        book=outer.read(entries[0])
    selected={}; invalid_invoices={}
    stats={'data_rows_scanned':0,'cancellation_rows_not_used':0,'nonpositive_rows_not_used':0}
    with zipfile.ZipFile(io.BytesIO(book)) as z:
        if sum(i.file_size for i in z.infolist())>700*1024*1024:
            raise ValueError('工作簿展开大小超过限制')
        with z.open('xl/sharedStrings.xml') as f:
            shared=[''.join(item.itertext()) for item in ET.parse(f).getroot()]
        with z.open('xl/worksheets/sheet1.xml') as f:
            for _,row in ET.iterparse(f,events=['end']):
                if row.tag!=NS+'row':continue
                cells={}
                for cell in row:
                    if cell.tag!=NS+'c':continue
                    col=re.match(r'[A-Z]+',cell.attrib['r']).group()
                    v=cell.find(NS+'v'); value='' if v is None else (v.text or '')
                    if cell.get('t')=='s':value=shared[int(value)]
                    cells[col]=value
                row_number=int(row.attrib['r']); row.clear()
                values=[cells.get(chr(65+i),'') for i in range(8)]
                if row_number==1:
                    if values!=EXPECTED_HEADERS:raise ValueError('源列名已改变，需重新审查映射')
                    continue
                stats['data_rows_scanned']+=1
                raw=dict(zip(EXPECTED_HEADERS,values)); invoice=raw['InvoiceNo']
                if invoice.upper().startswith('C'):
                    stats['cancellation_rows_not_used']+=1;continue
                if not invoice.isdigit():continue
                # 保留整张发票的拒绝依据；非法行即使早于首个有效行，也不能被漏掉后求和。
                try:
                    if not raw['CustomerID'].isdigit():raise ValueError('customer_missing_or_invalid')
                    quantity=Decimal(raw['Quantity']); price=Decimal(raw['UnitPrice'])
                    invoice_date=Decimal(raw['InvoiceDate'])
                    if not all(x.is_finite() for x in (quantity,price,invoice_date)):
                        raise ValueError('nonfinite_number')
                    if quantity<=0 or price<=0:
                        stats['nonpositive_rows_not_used']+=1
                        raise ValueError('nonpositive_quantity_or_price')
                    if quantity!=quantity.to_integral_value():raise ValueError('fractional_quantity')
                    if not 0<invoice_date<2958466:raise ValueError('invalid_invoice_date')
                    rounded=price.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
                    if abs(price-rounded)>Decimal('.000001'):
                        raise ValueError('price_not_representable_in_pence')
                except (ValueError,DecimalException) as exc:
                    invalid_invoices.setdefault(invoice,(row_number,str(exc) if isinstance(exc,ValueError) else 'invalid_decimal'))
                    continue
                if invoice not in selected:
                    if len(selected)>=count:continue
                    selected[invoice]=[]
                raw.update(source_row=row_number,unit_price_raw=raw['UnitPrice'],
                    unit_price_gbp=str(rounded),line_pence=int(rounded*100)*int(quantity))
                selected[invoice].append(raw)
    if len(selected)!=count:raise ValueError('没有足够可用的完整发票')
    for invoice,lines in selected.items():
        if invoice in invalid_invoices:
            row_number,reason=invalid_invoices[invoice]
            raise ValueError(f'所选发票{invoice}含不可用源行{row_number}（{reason}）；整张拒绝，未漏行求和')
        if len({r['CustomerID'] for r in lines})!=1:raise ValueError('同一发票客户不一致')
        if len({r['InvoiceDate'] for r in lines})!=1:raise ValueError('同一发票日期不一致')
    return selected,stats,hashlib.sha256(book).hexdigest()


def build_projection(invoices, source_sha, book_sha, stats):
    payload={'metadata':{'source':NOTICE+'; doi:10.24432/C5BW33',
        'version':'uci-projection-20261118-v1','currency':'CNY','normalized':True,'data_kind':'normalized_sample'},
        'buyers':[],'orders':[],'refunds':[],'shops':[{'shop_id':'S_UCI_DEMO','name':'UCI素材独立模拟店'}],'order_shops':[]}
    mappings=[];customers={}
    for index,(invoice,lines) in enumerate(invoices.items(),1):
        customer=lines[0]['CustomerID']
        if customer not in customers:
            identity=f'B_UCI_{len(customers)+1:03}'
            customers[customer]=identity;payload['buyers'].append({'buyer_id':identity,'display_name':'公开数据投影的合成买家'})
        oid='O_UCI_'+invoice;total=sum(r['line_pence'] for r in lines)
        # 固定演示时间，不声称原2010发票就是2026支付凭证。
        day=14+index;paid=f'2026-10-{day:02}T10:00:00+08:00'
        payload['orders'].append({'order_id':oid,'buyer_id':customers[customer],
            'paid_at':paid,'paid_amount_fen':total,'status':'paid'})
        payload['order_shops'].append({'order_id':oid,'shop_id':'S_UCI_DEMO'})
        mappings.append({'order_id':oid,'source_invoice':invoice,'source_customer':customer,
            'source_rows':[r['source_row'] for r in lines],'source_gbp_total':str(Decimal(total)/100),
            'projected_cny_fen':total,'source_invoice_date_excel':lines[0]['InvoiceDate'],
            'simulation_assumptions':['CNY数值投影而非GBP换汇','订单支付状态为合成','paid_at为固定演示时间','店铺与买家为本地模拟身份']})
    first=payload['orders'][0];refund1=max(1,first['paid_amount_fen']//4);refund2=max(1,first['paid_amount_fen']//10)
    if refund1+refund2>first['paid_amount_fen']:raise ValueError('模拟退款超过支付')
    for i,(amount,date) in enumerate([(refund1,'2026-10-20T09:00:00+08:00'),(refund2,'2026-11-02T09:00:00+08:00')],1):
        payload['refunds'].append({'refund_id':f'R_UCI_SYNTH_{i}','order_id':first['order_id'],
            'refunded_at':date,'refund_amount_fen':amount,'status':'succeeded'})
    expected={'cutoff':'2026-11-03T00:00:00+08:00','orders':[],'total_paid_fen':0,'total_refund_fen':refund1+refund2}
    for row in payload['orders']:
        refund=refund1+refund2 if row['order_id']==first['order_id'] else 0
        expected['orders'].append({'order_id':row['order_id'],'paid_fen':row['paid_amount_fen'],'refund_fen':refund,'net_fen':row['paid_amount_fen']-refund})
        expected['total_paid_fen']+=row['paid_amount_fen']
    expected['total_net_fen']=expected['total_paid_fen']-expected['total_refund_fen']
    lineage={'title':'Online Retail','creator':'Daqing Chen','year':2015,'doi':'10.24432/C5BW33','source_url':PAGE,
        'license':'CC BY 4.0','license_url':'https://creativecommons.org/licenses/by/4.0/',
        'archive_sha256':source_sha,'workbook_sha256':book_sha,'notice':NOTICE,'scanned':stats,
        'refunds':'全为本项目合成；源取消记录未作为成功退款','mappings':mappings}
    return payload,lineage,expected


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-zip',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--count',type=int,default=3)
    args=p.parse_args();out=args.output_dir.resolve()
    # 外部真实交易只能留在已忽略的私有输出区，不能写回公开样例目录。
    private_root=Path(__file__).resolve().parent/'local'/'imports'
    if private_root.is_symlink() or private_root.parent.is_symlink():
        raise SystemExit('私有输出目录不能是符号链接')
    try:out.relative_to(private_root.resolve())
    except ValueError:
        raise SystemExit('真实来源处理结果只能输出到本项目 local/imports/ 下；该目录不提交') from None
    names=['uci_projection.json','uci_source_rows.json','uci_source_mapping.json','uci_expected.json']
    if any((out/n).exists() for n in names):raise SystemExit('输出已存在，未覆盖；请选择新的目录比较版本')
    invoices,stats,book_sha=collect_invoices(args.source_zip,args.count)
    payload,lineage,expected=build_projection(invoices,hashlib.sha256(args.source_zip.read_bytes()).hexdigest(),book_sha,stats)
    from import_data import prepare_payload
    prepare_payload(payload)  # 只做规范化契约校验，不连接数据库。
    out.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(out,0o700)
    for name,value in zip(names,[payload,invoices,lineage,expected]):
        with (out/name).open('x',encoding='utf-8') as stream:
            os.chmod(out/name,0o600)
            stream.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'orders':len(payload['orders']),'source_rows_scanned':stats['data_rows_scanned'],'expected':expected,'database_writes':0},ensure_ascii=False))

if __name__=='__main__':main()
