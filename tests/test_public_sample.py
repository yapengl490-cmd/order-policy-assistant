"""自建微型工作簿验证发票完整性；不下载、不扫描正式大数据集、不写数据库。"""
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from prepare_public_sample import NS, EXPECTED_HEADERS, collect_invoices, build_projection
from import_data import prepare_payload


def row(**changes):
    return dict(zip(EXPECTED_HEADERS,['100001','SKU1','test item','2','40450.4166667','1.25','10001','UK'])) | changes


def archive(path, rows, headers=EXPECTED_HEADERS):
    shared=[]; table=ET.Element(NS+'worksheet'); data=ET.SubElement(table,NS+'sheetData')
    for index,values in enumerate([headers]+[[item.get(key,'') for key in EXPECTED_HEADERS] for item in rows],1):
        line=ET.SubElement(data,NS+'row',r=str(index))
        for col,value in enumerate(values):
            shared.append(value)
            cell=ET.SubElement(line,NS+'c',r=chr(65+col)+str(index),t='s')
            ET.SubElement(cell,NS+'v').text=str(len(shared)-1)
    strings=ET.Element(NS+'sst')
    for value in shared:ET.SubElement(ET.SubElement(strings,NS+'si'),NS+'t').text=value
    book=io.BytesIO()
    with zipfile.ZipFile(book,'w') as z:
        z.writestr('xl/sharedStrings.xml',ET.tostring(strings))
        z.writestr('xl/worksheets/sheet1.xml',ET.tostring(table))
    with zipfile.ZipFile(path,'w') as z:z.writestr('Online Retail.xlsx',book.getvalue())


class PublicSampleTests(unittest.TestCase):
    def collect(self, rows, count=1, headers=EXPECTED_HEADERS):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fixture.zip';archive(path,rows,headers)
            return collect_invoices(path,count)

    def test_full_invoice_total_and_cancellation_are_separate(self):
        rows=[row(),row(StockCode='SKU2',Quantity='1',UnitPrice='0.75'),row(InvoiceNo='C100001',Quantity='-1')]
        invoices,stats,book_sha=self.collect(rows)
        self.assertEqual(sum(item['line_pence'] for item in invoices['100001']),325)
        self.assertEqual([item['source_row'] for item in invoices['100001']],[2,3])
        self.assertEqual(stats['cancellation_rows_not_used'],1)
        payload,mapping,expected=build_projection(invoices,'test-source',book_sha,stats)
        prepare_payload(payload)
        self.assertEqual(expected['total_paid_fen'],325)
        self.assertEqual(mapping['mappings'][0]['source_gbp_total'],'3.25')
        self.assertEqual(payload['metadata']['currency'],'CNY')
        self.assertIn('非汇率',payload['metadata']['source'])
        self.assertTrue(all(item['refund_id'].startswith('R_UCI_SYNTH_') for item in payload['refunds']))

    def test_invalid_selected_invoice_line_before_or_after_good_line_is_never_dropped(self):
        invalids=[{'CustomerID':''},{'Quantity':'0'},{'Quantity':'-1'},{'Quantity':'1.5'},
                  {'Quantity':'bad'},{'UnitPrice':'0'},{'UnitPrice':'-2'},
                  {'UnitPrice':'NaN'},{'UnitPrice':'Infinity'},{'UnitPrice':'0.001'},
                  {'InvoiceDate':'not-a-date'}]
        for invalid in invalids:
            for reverse in (False,True):
                with self.subTest(invalid=invalid,reverse=reverse):
                    rows=[row(),row(**invalid)]
                    with self.assertRaisesRegex(ValueError,'未漏行求和'):
                        self.collect(rows[::-1] if reverse else rows)

    def test_invalid_unselected_invoice_does_not_corrupt_selected_one(self):
        invoices,_,_=self.collect([row(),row(InvoiceNo='200001',Quantity='bad')])
        self.assertEqual(set(invoices),{'100001'})
        self.assertEqual(len(invoices['100001']),1)

    def test_same_invoice_customer_or_date_conflict_is_rejected(self):
        for change in ({'CustomerID':'10002'},{'InvoiceDate':'40451'}):
            with self.subTest(change=change),self.assertRaises(ValueError):self.collect([row(),row(**change)])

    def test_unknown_header_and_noninteger_count_are_rejected(self):
        headers=list(EXPECTED_HEADERS);headers[5]='PriceUSD'
        with self.assertRaisesRegex(ValueError,'源列名'):self.collect([row()],headers=headers)
        with self.assertRaises(ValueError):self.collect([row()],count=True)

    def test_synthetic_workbook_projection_has_complete_lineage_and_fixed_money(self):
        # 发布测试自己构造输入，不能依赖原真实发票或客户编号。
        invoices,stats,book_sha=self.collect([
            row(InvoiceNo='900001',Quantity='2',UnitPrice='1.25'),
            row(InvoiceNo='900002',Quantity='3',UnitPrice='2.00'),
            row(InvoiceNo='900003',Quantity='1',UnitPrice='9.00'),
            row(InvoiceNo='900001',StockCode='SKU2',Quantity='1',UnitPrice='0.50')],count=3)
        payload,mapping,expected=build_projection(invoices,'synthetic-test-archive',book_sha,stats)
        self.assertEqual(expected['total_paid_fen'],1800)
        self.assertEqual(expected['total_refund_fen'],105)
        self.assertEqual(expected['total_net_fen'],1695)
        self.assertEqual(mapping['mappings'][0]['source_rows'],[2,5])
        self.assertEqual(mapping['mappings'][0]['source_gbp_total'],'3')
        self.assertEqual(mapping['archive_sha256'],'synthetic-test-archive')
        prepare_payload(payload)

    def test_public_demo_has_only_independent_synthetic_identifiers(self):
        base=Path(__file__).resolve().parents[1]/'samples/uci'
        self.assertFalse((base/'uci_source_rows.json').exists())
        self.assertFalse((base/'uci_source_mapping.json').exists())
        payload=json.loads((base/'uci_projection.json').read_text())
        self.assertIn('未取用真实UCI交易行',payload['metadata']['source'])
        self.assertTrue(all(row['order_id'].startswith('O_PUBLIC_SYNTH_') for row in payload['orders']))
        self.assertTrue(all(row['buyer_id'].startswith('B_PUBLIC_SYNTH_') for row in payload['buyers']))
        prepare_payload(payload)

    def test_external_source_output_cannot_target_public_sample_directory(self):
        from prepare_public_sample import main
        destination=Path(__file__).resolve().parents[1]/'samples/uci'
        with patch.object(sys,'argv',['prepare_public_sample.py','--source-zip','not-opened.zip',
                                      '--output-dir',str(destination)]), \
             patch('prepare_public_sample.collect_invoices') as collector:
            with self.assertRaisesRegex(SystemExit,'local/imports/'):
                main()
            collector.assert_not_called()


if __name__=='__main__':unittest.main()
