"""生成独立容器验证凭据；不读取正式local配置、不打印凭据。"""
from pathlib import Path
import os
import secrets
import sys

def prepare(path):
    path=Path(path).expanduser()
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if path.exists():
        print('验证配置已存在，保留原值。');return
    with path.open('x') as stream:
        os.chmod(path,0o600)
        for key in ['VERIFY_ADMIN_PASSWORD','VERIFY_READER_PASSWORD','VERIFY_MEMORY_PASSWORD','VERIFY_ACCESS_TOKEN']:
            stream.write(key+'='+secrets.token_urlsafe(32)+'\n')
    print('已生成独立验证配置；不展示内容，不上传。')
if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('用法：python prepare_verify.py /本机私密目录/verify.env')
    prepare(sys.argv[1])
