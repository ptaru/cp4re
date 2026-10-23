#!/usr/bin/env python3
"""Compile receiver sources and require exact equality with the packaged code plan."""
from pathlib import Path
import argparse,json,subprocess,os
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--zig',default='zig');p.add_argument('--out',type=Path,default=Path('/tmp/cp4-wake-code'));a=p.parse_args()
here=Path(__file__).resolve().parent;a.out.mkdir(parents=True,exist_ok=True)
flags=[a.zig,'cc','-target','riscv32-freestanding','-mcpu=generic_rv32+m+c','-mabi=ilp32','-Oz','-ffreestanding','-fno-stack-protector','-ffunction-sections','-fdata-sections','-fno-unwind-tables','-fno-asynchronous-unwind-tables']
env=dict(os.environ);env.setdefault('ZIG_GLOBAL_CACHE_DIR','/tmp/cp4-code-zig-cache');env.setdefault('ZIG_LOCAL_CACHE_DIR','/tmp/cp4-code-zig-local')
objects=[]
for name in ['receiver_core.c','target_prototype.c','status_bridge.S']:
 obj=a.out/(Path(name).stem+'.rv32.o');subprocess.run(flags+['-c',str(here/name),'-o',str(obj)],env=env,check=True);objects.append(str(obj))
elf=a.out/'prototype.elf';subprocess.run(flags+['-Wl,--no-gc-sections','-nostdlib','-Wl,-e,replacement_bootstrap','-Wl,-T,'+str(here/'prototype.ld'),*objects,'-o',str(elf)],env=env,check=True)
from elftools.elf.elffile import ELFFile
with elf.open('rb') as f:
 e=ELFFile(f)
 for region in json.loads((here/'patch.json').read_text())['regions']:
  s=e.get_section_by_name(region['section']);data=s.data()+bytes.fromhex('0100')*((region['capacity']-s['sh_size'])//2)
  assert s['sh_addr']-0x40d000==region['offset'] and data.hex()==region['bytes'],region['section']
print('PASS: compiled receiver sections match the packaged instruction bytes')
